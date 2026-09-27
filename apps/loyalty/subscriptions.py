"""Abonnements — invariant P4.

Le prix vient du catalogue, jamais du client : l'implémentation précédente
acceptait `monthly_price` dans la requête d'inscription, ce qui permettait de
s'abonner au tarif de son choix. Ici, souscrire ne prend qu'un identifiant de
plan ; le montant facturé est relu depuis `SubscriptionPlan.price`, comme un
prix de catalogue l'est pour une commande (C1).

Le règlement, initial ou de renouvellement, suit le chemin d'un paiement
ordinaire (`PaymentService.initiate_external`) : une transaction en attente,
puis une notification signée du prestataire — jamais une confirmation côté
client. L'abonnement s'active **parce que** sa transaction s'est soldée, pas
parce que la requête qui l'a demandée le prétend : voir `on_payment_settled`,
abonné à `payment_transaction_settled` depuis `AppConfig.ready()`, comme
`loyalty.receivers` l'est déjà à `order_status_changed`.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.loyalty.models import (
    SUBSCRIPTION_MACHINE,
    Subscription,
    SubscriptionPayment,
    SubscriptionPlan,
    SubscriptionStatus,
)
from apps.payments.gateway import CheckoutInstruction
from apps.payments.models import PaymentProvider, PaymentStatus
from apps.payments.models import Transaction as PaymentTransaction
from apps.payments.services import PaymentService
from common.exceptions import BusinessRuleViolation

__all__ = ["SubscriptionService"]


class SubscriptionService:
    @staticmethod
    @transaction.atomic
    def subscribe(
        *, user: User, plan: SubscriptionPlan
    ) -> tuple[Subscription, CheckoutInstruction]:
        """Ouvre un abonnement et sa première demande de paiement.

        `one_open_subscription_per_user` empêche déjà deux abonnements
        ouverts en base ; le contrôle ici évite le voyage réseau vers le
        prestataire pour une demande que la contrainte refuserait de toute
        façon à l'écriture.
        """
        if not plan.is_active:
            raise BusinessRuleViolation("Ce plan n'est plus proposé.")

        if Subscription.objects.filter(
            user=user, status__in=[SubscriptionStatus.PENDING, SubscriptionStatus.ACTIVE]
        ).exists():
            raise BusinessRuleViolation("Un abonnement est déjà ouvert pour ce client.")

        subscription = Subscription.objects.create(
            user=user, plan=plan, status=SubscriptionStatus.PENDING
        )
        _, instruction = SubscriptionService._charge(subscription)
        return subscription, instruction

    @staticmethod
    @transaction.atomic
    def cancel(*, subscription: Subscription) -> Subscription:
        """Résilie l'abonnement — sans effet sur l'échéance déjà payée.

        `auto_renew` tombe à faux avant même la transition : un renouvellement
        déclenché entre la lecture et l'écriture ne doit pas repartir sur un
        abonnement qu'on est en train de fermer.
        """
        locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
        SUBSCRIPTION_MACHINE.validate(locked.status, SubscriptionStatus.CANCELLED)

        locked.status = SubscriptionStatus.CANCELLED
        locked.auto_renew = False
        locked.cancelled_at = timezone.now()
        locked.save(update_fields=["status", "auto_renew", "cancelled_at", "updated_at"])
        return locked

    @staticmethod
    @transaction.atomic
    def expire(*, subscription: Subscription) -> Subscription:
        """Clôt un abonnement dont le renouvellement n'a pas abouti.

        `can()` et non `validate()` : appelée depuis une tâche planifiée sur
        un lot d'abonnements, elle ne doit pas échouer sur celui qu'un
        paiement concurrent vient d'activer ou de résilier — elle passe, sans
        rien faire, sur ce qui n'est plus dans l'état attendu.
        """
        locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
        if not SUBSCRIPTION_MACHINE.can(locked.status, SubscriptionStatus.EXPIRED):
            return locked

        locked.status = SubscriptionStatus.EXPIRED
        locked.save(update_fields=["status", "updated_at"])
        return locked

    # -------------------------------------------------------- facturation

    @staticmethod
    def _charge(subscription: Subscription) -> tuple[PaymentTransaction, CheckoutInstruction]:
        """Ouvre la demande de paiement d'une échéance, initiale ou de renouvellement.

        Le montant vient de `plan.price` à l'instant de l'appel — pas de celui
        figé à la souscription — pour qu'une évolution tarifaire s'applique
        aux échéances futures sans exiger une nouvelle souscription.
        """
        plan = subscription.plan
        period_start = timezone.now()
        period_end = period_start + dt.timedelta(days=plan.billing_period_days)

        txn, instruction = PaymentService.initiate_external(
            provider=PaymentProvider.PAYDUNYA,
            amount=plan.price,
            payer=subscription.user,
        )
        SubscriptionPayment.objects.create(
            subscription=subscription,
            transaction=txn,
            period_start=period_start,
            period_end=period_end,
        )
        return txn, instruction

    # -------------------------------------------------- réaction au paiement

    @staticmethod
    @transaction.atomic
    def on_payment_settled(txn: PaymentTransaction) -> None:
        """Active l'abonnement dont cette transaction règle une échéance.

        Appelée par le récepteur du signal de paiement, jamais par une vue :
        c'est l'encaissement vérifié qui active l'abonnement ou prolonge sa
        période, pas la requête qui l'a demandé. Sans effet si la transaction
        ne règle aucune échéance — la plupart n'en règlent pas.
        """
        link = (
            SubscriptionPayment.objects.select_related("subscription")
            .filter(transaction=txn)
            .first()
        )
        if link is None:
            return

        subscription = Subscription.objects.select_for_update().get(pk=link.subscription_id)

        if subscription.status == SubscriptionStatus.PENDING:
            SUBSCRIPTION_MACHINE.validate(subscription.status, SubscriptionStatus.ACTIVE)
            subscription.status = SubscriptionStatus.ACTIVE
        elif subscription.status != SubscriptionStatus.ACTIVE:
            # Résilié ou expiré entre-temps : l'encaissement reste acquis
            # (P1 — `completed` ne redescend jamais), mais ne réactive rien.
            # Un remboursement, s'il est décidé, est un geste séparé.
            return

        subscription.current_period_start = link.period_start
        subscription.current_period_end = link.period_end
        subscription.save(
            update_fields=["status", "current_period_start", "current_period_end", "updated_at"]
        )

    # --------------------------------------------------------- renouvellement

    @staticmethod
    def due_for_renewal(*, horizon: dt.datetime) -> list[Subscription]:
        """Abonnements actifs dont la période s'achève, hors ceux déjà en cours de règlement.

        Exclut ceux qui portent une transaction encore `pending`/`processing` :
        sans ce filtre, une tâche qui repasse avant que le webhook précédent
        soit revenu ouvrirait une seconde demande pour la même échéance.
        """
        grace = dt.timedelta(days=settings.SUBSCRIPTION_RENEWAL_GRACE_DAYS)
        return list(
            Subscription.objects.filter(
                status=SubscriptionStatus.ACTIVE,
                auto_renew=True,
                current_period_end__gte=horizon - grace,
                current_period_end__lte=horizon,
            ).exclude(
                payments__transaction__status__in=[
                    PaymentStatus.PENDING,
                    PaymentStatus.PROCESSING,
                ]
            )
        )

    @staticmethod
    def overdue(*, horizon: dt.datetime) -> list[Subscription]:
        """Abonnements actifs dont la période de grâce est dépassée — à expirer."""
        grace = dt.timedelta(days=settings.SUBSCRIPTION_RENEWAL_GRACE_DAYS)
        return list(
            Subscription.objects.filter(
                status=SubscriptionStatus.ACTIVE, current_period_end__lt=horizon - grace
            )
        )

    @staticmethod
    def renew_due(*, horizon: dt.datetime | None = None) -> int:
        """Facture chaque abonnement échu, expire ceux hors délai de grâce.

        Appelée par la tâche planifiée. Renvoie le nombre de demandes de
        paiement ouvertes ; l'activation effective attend, comme toujours, la
        notification du prestataire.
        """
        moment = horizon if horizon is not None else timezone.now()

        for subscription in SubscriptionService.overdue(horizon=moment):
            SubscriptionService.expire(subscription=subscription)

        compte = 0
        for subscription in SubscriptionService.due_for_renewal(horizon=moment):
            SubscriptionService._charge(subscription)
            compte += 1
        return compte
