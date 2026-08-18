"""Encaissement et remboursement — invariants C5, P1, P3.

**Le webhook est la seule source de vérité de l'encaissement.** Le retour du
client sur l'application n'est qu'un indice d'interface : il ne déclenche
aucune écriture d'état de paiement. C'est ce qui rend impossible de se déclarer
payé — la faille la plus grave de l'implémentation précédente, où un
participant d'un paiement partagé basculait la commande entière en `completed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.delivery.models import CourierProfile
from apps.orders.models import Order, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.payments.gateway import CheckoutInstruction, Notification, gateway_for
from apps.payments.models import (
    PAYMENT_MACHINE,
    PaymentProvider,
    PaymentStatus,
    Refund,
    Transaction,
    WebhookEvent,
    Withdrawal,
)
from apps.payments.signals import payment_transaction_settled
from common.exceptions import BusinessRuleViolation, InsufficientBalance
from common.money import Money

__all__ = [
    "PaymentService",
    "RefundService",
    "WebhookOutcome",
    "WithdrawalService",
    "settled_total",
]

#: Moyen de paiement de la commande → prestataire qui l'encaisse.
PROVIDER_FOR_METHOD = {
    PaymentMethod.MOBILE_MONEY: PaymentProvider.PAYDUNYA,
    PaymentMethod.CARD: PaymentProvider.PAYDUNYA,
    PaymentMethod.CASH: PaymentProvider.CASH,
    PaymentMethod.WALLET: PaymentProvider.WALLET,
}


def settled_total(order: Order) -> Money:
    """Somme réellement encaissée sur cette commande.

    Les montants sont additionnés en unité mineure et **par devise**, ce que la
    structure garantit : une commande porte une devise unique, figée à sa
    création. Un `SUM` sur des devises mêlées produirait un nombre qui ne veut
    rien dire.
    """
    total = Money.zero(order.total.currency)
    for amount in order.transactions.filter(status=PaymentStatus.COMPLETED):
        total += amount.amount
    return total


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """Ce qu'a produit une notification, pour le journal et la réponse."""

    accepted: bool
    detail: str
    transaction: Transaction | None = None


class PaymentService:
    @staticmethod
    @transaction.atomic
    def initiate(*, order: Order, payer: User) -> tuple[Transaction, CheckoutInstruction]:
        """Ouvre une demande de paiement pour le solde restant.

        **C5** — les deux gardes qui manquaient : une commande annulée
        n'accepte plus de paiement, et une commande déjà soldée n'en accepte
        pas un second. Sans elles, un client pouvait payer deux fois, ou payer
        une commande qu'on ne préparerait jamais.
        """
        locked = Order.objects.select_for_update().get(pk=order.pk)

        if locked.status == OrderStatus.CANCELLED:
            raise BusinessRuleViolation(
                "Cette commande est annulée ; elle n'accepte plus de paiement.",
                current_status=locked.status,
            )

        if hasattr(locked, "split_payment"):
            # Payer le tout solderait la commande en laissant des parts
            # ouvertes : les autres participants paieraient une commande déjà
            # réglée, ou ne paieraient jamais.
            raise BusinessRuleViolation(
                "Cette commande est partagée : chaque participant règle sa part.",
                split=str(locked.split_payment.pk),
            )

        outstanding = locked.total - settled_total(locked)
        if not outstanding.is_positive:
            raise BusinessRuleViolation(
                "Cette commande est déjà réglée.", current_status=locked.status
            )

        provider = PROVIDER_FOR_METHOD[PaymentMethod(locked.payment_method)]
        # `amount` est un `MoneyField` — voir la note dans `orders.services`.
        pending = Transaction(  # type: ignore[misc]
            order=locked,
            provider=provider,
            provider_reference="",
            amount=outstanding,
            payer=payer,
            status=PaymentStatus.PENDING,
        )
        # La référence vient du prestataire, mais celui-ci a besoin de
        # l'identifiant de la transaction : la clé primaire est un UUIDv7
        # généré côté Python, donc connue avant l'insertion.
        instruction = gateway_for(provider).open_checkout(pending)
        pending.provider_reference = instruction.provider_reference
        pending.save()

        # `pending → processing` dit que la main est passée au prestataire.
        # Confondre les deux états empêcherait de distinguer « le client n'a
        # rien fait » de « le client est sur le portail ».
        PaymentService._move(pending, PaymentStatus.PROCESSING)
        return pending, instruction

    @staticmethod
    @transaction.atomic
    def initiate_external(
        *, provider: str, amount: Money, payer: User, payer_phone: str = ""
    ) -> tuple[Transaction, CheckoutInstruction]:
        """Ouvre une demande de paiement qui ne règle pas de commande.

        Même chemin que `initiate` — une transaction en attente, une demande
        ouverte chez le prestataire, `pending → processing` — sans les gardes
        propres à une commande (annulation, partage, solde restant), qui n'ont
        pas de sens ici. `order` reste vide : c'est à l'appelant de relier sa
        propre entité à la transaction, jamais l'inverse (ADR-002).
        """
        if not amount.is_positive:
            raise BusinessRuleViolation("Le montant à encaisser doit être strictement positif.")

        pending = Transaction(  # type: ignore[misc]
            order=None,
            provider=provider,
            provider_reference="",
            amount=amount,
            payer=payer,
            payer_phone=payer_phone,
            status=PaymentStatus.PENDING,
        )
        instruction = gateway_for(provider).open_checkout(pending)
        pending.provider_reference = instruction.provider_reference
        pending.save()

        PaymentService._move(pending, PaymentStatus.PROCESSING)
        return pending, instruction

    @staticmethod
    def _move(txn: Transaction, target: str) -> None:
        PAYMENT_MACHINE.validate(txn.status, target)
        txn.status = target
        fields = ["status", "updated_at"]
        if target == PaymentStatus.COMPLETED:
            txn.completed_at = timezone.now()
            fields.append("completed_at")
        txn.save(update_fields=fields)

    # ------------------------------------------------------------- webhook

    @staticmethod
    def handle_webhook(
        *, provider: str, notification: Notification, payload: dict[str, Any]
    ) -> WebhookOutcome:
        """Applique une notification de prestataire.

        **P1** — l'idempotence est portée par l'unicité de `(provider,
        event_id)` en base, pas par un `if déjà_traité`. Deux workers qui
        traitent le même rejeu en parallèle passeraient tous deux le test
        applicatif ; la contrainte, elle, n'en laisse passer qu'un.

        Un rejeu répond **succès**. Renvoyer une erreur ferait retenter le
        prestataire indéfiniment sur un événement pourtant bien traité.

        `payload` est conservé brut à côté de la notification normalisée : le
        jour où un champ manque, c'est lui qu'on relira, pas ce qu'on a bien
        voulu en extraire.
        """
        if not notification.event_id.strip(":"):
            raise BusinessRuleViolation("Notification sans identifiant d'événement.")

        # `get_or_create` **lit avant d'écrire**, et c'est là toute la
        # différence avec le `create` rattrapé qui précédait. Celui-ci était
        # correct — son `atomic()` imbriqué posait bien le point de reprise que
        # réclame une violation d'unicité — mais il obtenait l'idempotence en
        # provoquant l'erreur puis en l'avalant. Chaque rejeu laissait donc une
        # ligne `duplicate key value violates unique constraint
        # "webhook_event_unique_per_provider"` dans le journal PostgreSQL, pour
        # un cas parfaitement normal : les prestataires renvoient leurs
        # notifications, c'est le principe. Le journal se remplissait d'erreurs
        # qui n'en sont pas, et les vraies s'y noyaient.
        #
        # Le chemin courant est désormais un simple `SELECT`. La garantie ne
        # faiblit pas pour autant : `get_or_create` conserve l'insertion sous
        # point de reprise pour la course où deux workers ne trouvent rien puis
        # insèrent tous deux — c'est la contrainte qui tranche, comme avant, et
        # `created` dit lequel des deux a gagné.
        event, created = WebhookEvent.objects.get_or_create(
            provider=provider,
            event_id=notification.event_id,
            defaults={"payload": payload, "signature_verified": True},
        )
        if not created:
            # Rejeu : on répond succès sans réappliquer. Une erreur ferait
            # retenter le prestataire indéfiniment sur un événement déjà traité.
            return WebhookOutcome(accepted=True, detail="Événement déjà traité.")

        return PaymentService._apply(event, provider=provider, notification=notification)

    @staticmethod
    @transaction.atomic
    def _apply(event: WebhookEvent, *, provider: str, notification: Notification) -> WebhookOutcome:
        reference = notification.provider_reference
        target = notification.status

        txn = (
            Transaction.objects.select_for_update()
            .filter(provider=provider, provider_reference=reference)
            .first()
        )
        if txn is None:
            # Enregistré mais non appliqué : une notification sans transaction
            # correspondante est soit un test du prestataire, soit une erreur
            # de configuration. La tracer permet de la retrouver ; la refuser
            # ferait retenter à l'infini.
            event.processing_error = f"Aucune transaction pour la référence {reference!r}."
            event.processed_at = timezone.now()
            event.save(update_fields=["processing_error", "processed_at"])
            return WebhookOutcome(accepted=True, detail=event.processing_error)

        if PAYMENT_MACHINE.is_noop(txn.status, target):
            # P1 littéral : un `completed` rejoué ne réécrit rien et
            # n'enclenche aucun effet de bord.
            PaymentService._close(event)
            return WebhookOutcome(True, "Statut déjà atteint.", txn)

        if not PAYMENT_MACHINE.can(txn.status, target):
            # Une notification tardive — « échoué » après un encaissement — ne
            # doit ni passer, ni faire retenter. Un 409 rendu au prestataire le
            # ferait revenir indéfiniment sur une transition qui ne sera jamais
            # légale. L'invariant tient quand même : la transaction ne bouge
            # pas, et l'anomalie est tracée pour être vue.
            event.processing_error = f"Transition refusée : {txn.status} → {target}."
            PaymentService._close(event)
            return WebhookOutcome(True, event.processing_error, txn)

        if target == PaymentStatus.FAILED:
            txn.failure_reason = notification.reason
            txn.save(update_fields=["failure_reason"])

        PaymentService._move(txn, target)

        if target == PaymentStatus.COMPLETED:
            if txn.order is not None:
                # La part se solde **parce que** sa transaction s'est soldée
                # (P2). Importé ici et non en tête : `split` a besoin de ce
                # module pour ouvrir une demande de paiement, et l'import
                # croisé au chargement ferait échouer le démarrage.
                from apps.payments.split import SplitService

                SplitService.on_transaction_settled(txn)
                PaymentService._confirm_order(txn.order)

            # Émis pour toute transaction, y compris celles qui ne règlent pas
            # de commande — un abonnement aujourd'hui. `payments` n'a pas
            # besoin de savoir qui écoute ; voir `apps.payments.signals`.
            payment_transaction_settled.send(sender=Transaction, transaction=txn)

        PaymentService._close(event)
        return WebhookOutcome(True, f"Transaction passée en {target}.", txn)

    @staticmethod
    def _close(event: WebhookEvent) -> None:
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at", "processing_error"])

    @staticmethod
    def _confirm_order(order: Order) -> None:
        """Confirme la commande une fois le solde encaissé.

        Le paiement partiel ne confirme pas : c'est le cas du paiement partagé,
        où la commande n'est engagée que lorsque toutes les parts sont
        réglées. La comparaison porte sur le total encaissé, pas sur le nombre
        de transactions.
        """
        if settled_total(order) < order.total:
            return
        if ORDER_MACHINE.can(order.status, OrderStatus.CONFIRMED):
            OrderService.transition_to(
                order=order, target=OrderStatus.CONFIRMED, reason="Paiement encaissé."
            )


class RefundService:
    @staticmethod
    @transaction.atomic
    def refund(
        *, order: Order, transaction_id: str, amount: Money, reason: str, actor: User
    ) -> Refund:
        """Enregistre un remboursement — **sans le verser**.

        PayDunya n'expose pas d'API de remboursement : le virement se fait
        depuis leur tableau de bord. Cette méthode écrit donc l'**intention**,
        avec son plafond et sa trace, et laisse le mouvement d'argent à un
        geste humain. Le remboursement reste en `pending` jusqu'à ce que
        quelqu'un le confirme.

        C'est à dire à l'exploitation avant la mise en service : sans cela,
        quelqu'un cliquera « rembourser », verra une ligne apparaître, et
        croira que le client a été remboursé.


        **P3** — le plafond est le total réellement encaissé, **moins ce qui a
        déjà été remboursé**. C'est la seconde moitié qui manquait : plafonner
        chaque remboursement pris isolément laisse rembourser trois fois la
        totalité en trois appels.

        Le plafond ne peut pas être une contrainte `CHECK` : il porte sur une
        somme d'autres lignes. Il est donc appliqué ici, sous verrou sur la
        commande — sans quoi deux demandes concurrentes liraient le même
        « déjà remboursé » et passeraient toutes les deux.
        """
        locked = Order.objects.select_for_update().get(pk=order.pk)
        txn = locked.transactions.select_for_update().get(pk=transaction_id)

        if not txn.is_settled:
            raise BusinessRuleViolation(
                "Seule une transaction encaissée peut être remboursée.",
                current_status=txn.status,
            )
        if not amount.is_positive:
            raise BusinessRuleViolation("Le montant remboursé doit être strictement positif.")

        already = Money.zero(locked.total.currency)
        for refund in locked.refunds.filter(
            status__in=[PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.COMPLETED]
        ):
            already += refund.amount

        remaining = settled_total(locked) - already
        if amount > remaining:
            raise BusinessRuleViolation(
                f"Remboursement plafonné à {remaining} — encaissé "
                f"{settled_total(locked)}, déjà remboursé {already}.",
                refundable=str(remaining.amount_minor),
                currency=remaining.currency,
            )

        return Refund.objects.create(  # type: ignore[misc]
            order=locked,
            transaction=txn,
            amount=amount,
            reason=reason,
            requested_by=actor,
            status=PaymentStatus.PENDING,
        )


class WithdrawalService:
    """Retrait des gains d'un livreur.

    Écrit comme `RefundService`, et pour la même raison : PayDunya ne verse pas
    sur commande d'API dans cette intégration, et l'app livreur ne doit de toute
    façon pas déclencher un décaissement. Ce service **débite le solde** — c'est
    la partie qui ne peut pas attendre — et enregistre l'intention de versement,
    que l'exploitation exécute ensuite.

    L'implémentation précédente faisait l'inverse : le téléphone appelait l'API
    de décaissement avec un montant qu'il calculait lui-même, puis écrivait la
    ligne en base. Le bénéficiaire décidait donc de ce qu'il touchait.
    """

    @staticmethod
    @transaction.atomic
    def request(*, courier: CourierProfile, amount: Money) -> Withdrawal:
        """Débite les gains et ouvre une demande de retrait.

        Le verrou est indispensable : deux demandes simultanées passeraient
        toutes deux la vérification de solde et videraient le compteur deux
        fois. C'est le même schéma que le débit de points de fidélité.
        """
        locked = CourierProfile.objects.select_for_update().get(pk=courier.pk)
        earnings = locked.total_earnings

        if earnings is None or earnings.amount_minor <= 0:
            raise InsufficientBalance("Aucun gain disponible au retrait.")
        if amount.currency != earnings.currency:
            raise BusinessRuleViolation(
                "Le retrait doit être demandé dans la devise des gains.",
                earnings_currency=earnings.currency,
            )
        if amount.amount_minor > earnings.amount_minor:
            raise InsufficientBalance(
                "Le montant demandé dépasse les gains disponibles.",
                available=str(earnings),
            )

        locked.total_earnings = earnings - amount
        locked.save(update_fields=["total_earnings_minor", "total_earnings_currency", "updated_at"])

        # `MoneyField` est un type composite ajouté par `contribute_to_class` :
        # django-stubs ne le voit pas comme un attribut de modèle, comme pour
        # `Refund` ci-dessus.
        return Withdrawal.objects.create(  # type: ignore[misc]
            courier=locked, amount=amount, status=PaymentStatus.PENDING
        )

    @staticmethod
    @transaction.atomic
    def settle(*, withdrawal: Withdrawal, provider_reference: str) -> Withdrawal:
        """Le versement a été exécuté — geste de l'exploitation, pas du livreur."""
        PAYMENT_MACHINE.validate(withdrawal.status, PaymentStatus.PROCESSING)
        PAYMENT_MACHINE.validate(PaymentStatus.PROCESSING, PaymentStatus.COMPLETED)

        withdrawal.status = PaymentStatus.COMPLETED
        withdrawal.provider_reference = provider_reference
        withdrawal.completed_at = timezone.now()
        withdrawal.save(
            update_fields=["status", "provider_reference", "completed_at", "updated_at"]
        )
        return withdrawal

    @staticmethod
    @transaction.atomic
    def fail(*, withdrawal: Withdrawal, reason: str) -> Withdrawal:
        """Le versement n'a pas abouti : les gains sont **rendus**.

        Sans ce recrédit, un virement échoué ferait disparaître le solde du
        livreur — l'argent ne serait ni sur son compte, ni dans l'application.
        """
        PAYMENT_MACHINE.validate(withdrawal.status, PaymentStatus.FAILED)

        locked = CourierProfile.objects.select_for_update().get(pk=withdrawal.courier_id)
        current = locked.total_earnings or Money.zero(withdrawal.amount.currency)
        locked.total_earnings = current + withdrawal.amount
        locked.save(update_fields=["total_earnings_minor", "total_earnings_currency", "updated_at"])

        withdrawal.status = PaymentStatus.FAILED
        withdrawal.failure_reason = reason
        withdrawal.save(update_fields=["status", "failure_reason", "updated_at"])
        return withdrawal
