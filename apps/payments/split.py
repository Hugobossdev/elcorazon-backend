"""Paiement partagé — invariant P2.

C'est la faille la plus grave de l'implémentation précédente, et la seule qui
donnait un repas gratuit : **n'importe quel participant pouvait se déclarer
payé**, ce qui basculait la commande entière en `completed`. Le correctif
d'alors avait restreint l'action aux administrateurs — c'est-à-dire déplacé le
problème sans le résoudre — et l'ADR notait que « le vrai flux par part reste à
construire ». C'est ce module.

La réponse n'est pas une vérification supplémentaire mais une **structure** :
une part n'est réputée réglée que si elle porte une transaction encaissée, et
la contrainte est en base (`settled_share_requires_transaction`). Il n'existe
donc aucun chemin — API, back-office, script d'exploitation — pour marquer une
part payée sans encaissement réel. Ce module ne *garde* pas cette règle : il
travaille dans un espace où l'enfreindre est impossible.

Le règlement d'une part suit exactement le chemin d'un paiement ordinaire :
demande au prestataire, puis notification signée. La part se solde **parce que**
sa transaction s'est soldée, jamais parce que quelqu'un l'a dit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction

from apps.accounts.models import User
from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.payments.gateway import CheckoutInstruction, gateway_for
from apps.payments.models import (
    PAYMENT_MACHINE,
    PaymentStatus,
    SplitPayment,
    SplitShare,
    Transaction,
)
from apps.payments.services import PROVIDER_FOR_METHOD, PaymentService, settled_total
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["ParticipantInput", "SplitService"]


@dataclass(frozen=True, slots=True)
class ParticipantInput:
    """Un convive.

    `user` est facultatif : la moitié des participants d'un repas partagé n'ont
    pas de compte, et exiger une inscription pour payer sa part ferait échouer
    la fonctionnalité sur son cas le plus courant. Ils reçoivent un lien.
    """

    display_name: str
    user: User | None = None
    phone: str = ""
    amount: Money | None = None


class SplitService:
    @staticmethod
    @transaction.atomic
    def create(
        *, order: Order, initiator: User, participants: Sequence[ParticipantInput]
    ) -> SplitPayment:
        """Ouvre un partage et répartit le total.

        Sans montants explicites, `Money.allocate` divise **sans perdre une
        unité mineure** : 1 000 F en trois donne 333, 333 et 334. Une division
        naïve en perdrait un à chaque commande, et l'écart ne se verrait qu'au
        rapprochement comptable.

        Avec des montants explicites — quelqu'un a pris le dessert — leur somme
        doit tomber juste. Accepter un écart reviendrait à décider en silence
        qui paie la différence.
        """
        locked = Order.objects.select_for_update().get(pk=order.pk)

        if locked.customer_id != initiator.pk:
            raise BusinessRuleViolation("Seul le client de la commande peut ouvrir un partage.")
        if locked.status == OrderStatus.CANCELLED:
            raise BusinessRuleViolation("Cette commande est annulée.", current_status=locked.status)
        if hasattr(locked, "split_payment"):
            raise BusinessRuleViolation("Cette commande a déjà un partage en cours.")
        if settled_total(locked).is_positive:
            # C5 — un partage sur une commande déjà entamée produirait des parts
            # dont la somme dépasse ce qui reste dû.
            raise BusinessRuleViolation("Cette commande a déjà reçu un paiement.")
        if len(participants) < 2:
            raise BusinessRuleViolation("Un partage demande au moins deux participants.")

        montants = SplitService._allocate(locked.total, participants)

        split = SplitPayment.objects.create(  # type: ignore[misc]
            order=locked, initiated_by=initiator, total_amount=locked.total
        )
        SplitShare.objects.bulk_create(
            SplitShare(  # type: ignore[misc]
                split=split,
                participant=participant.user,
                display_name=participant.display_name,
                phone=participant.phone,
                amount=montant,
            )
            for participant, montant in zip(participants, montants, strict=True)
        )
        return split

    @staticmethod
    def _allocate(total: Money, participants: Sequence[ParticipantInput]) -> list[Money]:
        """Répartit le total, en garantissant que **chaque part est positive**.

        `share_amount_positive` interdit en base une part nulle ou négative, et
        deux chemins y menaient sans que rien ne les arrête avant l'insertion :

        * **la répartition égale d'un total plus petit que le nombre de
          convives.** `Money.allocate` ne perd aucune unité mineure, mais il ne
          peut pas en inventer : 2 F entre trois personnes donnent 1, 1 et 0.
          La contrainte rejetait alors la commande entière par une violation
          d'intégrité, au lieu d'un refus lisible ;
        * **les montants explicites**, dont seule la *somme* était vérifiée.
          `[1000, 0]` sur un total de 1 000 F tombait juste et passait la
          garde ; `[1500, -500]` aussi. Le premier crée un convive qui ne paie
          rien — donc une part que personne ne réglera jamais et qui laisse le
          partage éternellement ouvert —, le second un montant négatif.

        Les deux sont désormais refusés avant toute écriture, avec un message
        qui dit quoi corriger.
        """
        explicites = [p.amount for p in participants if p.amount is not None]
        if not explicites:
            if total.amount_minor < len(participants):
                # Refusé ici plutôt que laissé produire une part nulle : diviser
                # équitablement suppose qu'il y ait de quoi donner au moins une
                # unité mineure à chacun.
                raise BusinessRuleViolation(
                    f"Le total ({total}) est trop faible pour être partagé entre "
                    f"{len(participants)} participants.",
                    total=str(total.amount_minor),
                    participants=len(participants),
                )
            return total.allocate([1] * len(participants))

        if len(explicites) != len(participants):
            raise BusinessRuleViolation(
                "Indiquez un montant pour chaque participant, ou pour aucun."
            )

        somme = Money.zero(total.currency)
        for participant, montant in zip(participants, explicites, strict=True):
            if montant.currency != total.currency:
                raise BusinessRuleViolation(f"Les parts doivent être en {total.currency}.")
            if not montant.is_positive:
                raise BusinessRuleViolation(
                    f"La part de {participant.display_name} doit être strictement "
                    f"positive (reçu {montant}).",
                    participant=participant.display_name,
                    received=str(montant.amount_minor),
                )
            somme += montant

        if somme != total:
            raise BusinessRuleViolation(
                f"La somme des parts ({somme}) ne fait pas le total ({total}).",
                expected=str(total.amount_minor),
                received=str(somme.amount_minor),
            )
        return list(explicites)

    # ------------------------------------------------------------ règlement

    @staticmethod
    @transaction.atomic
    def pay_share(
        *, share: SplitShare, payer: User | None = None
    ) -> tuple[Transaction, CheckoutInstruction]:
        """Ouvre une demande de paiement pour **une** part.

        Le chemin est celui d'un paiement ordinaire : le prestataire est
        interrogé, la transaction naît en attente, et seule sa notification
        signée la soldera. C'est ce qui fait que la part ne peut pas se déclarer
        payée — elle ne décide de rien, elle suit sa transaction.
        """
        locked = (
            SplitShare.objects.select_for_update().select_related("split__order").get(pk=share.pk)
        )
        order = locked.split.order

        if locked.status == PaymentStatus.COMPLETED:
            raise BusinessRuleViolation("Cette part est déjà réglée.")
        if order.status == OrderStatus.CANCELLED:
            raise BusinessRuleViolation("Cette commande est annulée.")

        en_cours = locked.transaction
        if en_cours is not None and en_cours.status in {
            PaymentStatus.PENDING,
            PaymentStatus.PROCESSING,
        }:
            # Deux transactions ouvertes sur une même part feraient payer deux
            # fois le même convive.
            raise BusinessRuleViolation(
                "Un règlement est déjà en cours pour cette part.",
                transaction=str(en_cours.pk),
            )

        provider = PROVIDER_FOR_METHOD[PaymentMethod(order.payment_method)]
        pending = Transaction(  # type: ignore[misc]
            order=order,
            provider=provider,
            provider_reference="",
            amount=locked.amount,
            payer=payer or locked.participant,
            payer_phone=locked.phone,
            status=PaymentStatus.PENDING,
        )
        instruction = gateway_for(provider).open_checkout(pending)
        pending.provider_reference = instruction.provider_reference
        pending.save()

        # La part pointe vers sa transaction **avant** l'encaissement : c'est ce
        # lien qui permettra à la contrainte de base d'accepter le passage en
        # `completed` le moment venu, et à personne de le faire sans lui.
        locked.transaction = pending
        locked.status = PaymentStatus.PROCESSING
        locked.save(update_fields=["transaction", "status", "updated_at"])

        PaymentService._move(pending, PaymentStatus.PROCESSING)

        # Le partage passe « en cours » dès la première part réglée. Sans cette
        # étape il resterait `pending`, et la machine à états refuserait plus
        # tard `pending → completed` : le partage soldé n'aurait aucun moyen de
        # le dire.
        SplitService._start(locked.split)

        return pending, instruction

    @staticmethod
    def _start(split: SplitPayment) -> None:
        if PAYMENT_MACHINE.can(split.status, PaymentStatus.PROCESSING):
            split.status = PaymentStatus.PROCESSING
            split.save(update_fields=["status", "updated_at"])

    @staticmethod
    def on_transaction_settled(txn: Transaction) -> None:
        """Solde la part adossée à cette transaction, s'il y en a une.

        Appelée par le traitement des notifications, jamais par une vue : c'est
        l'encaissement qui solde la part, et il n'arrive que par là.
        """
        share = SplitShare.objects.select_for_update().filter(transaction=txn).first()
        if share is None:
            return

        if not PAYMENT_MACHINE.can(share.status, PaymentStatus.COMPLETED):
            return

        share.status = PaymentStatus.COMPLETED
        share.save(update_fields=["status", "updated_at"])

        SplitService._close_if_complete(share.split)

    @staticmethod
    def _close_if_complete(split: SplitPayment) -> None:
        """Marque le partage soldé quand toutes ses parts le sont.

        Le statut du partage est **dérivé** et non piloté : il ne peut pas
        avancer sans que les parts aient avancé, donc il ne peut pas mentir.
        """
        restantes = split.shares.exclude(status=PaymentStatus.COMPLETED).exists()
        if restantes:
            return

        if PAYMENT_MACHINE.can(split.status, PaymentStatus.COMPLETED):
            split.status = PaymentStatus.COMPLETED
            split.save(update_fields=["status", "updated_at"])
