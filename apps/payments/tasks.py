"""Tâches planifiées des paiements."""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.orders.models import Order, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.payments.models import Transaction
from common.exceptions import BusinessRuleViolation
from common.state_machine import IllegalTransition

__all__ = ["expire_unpaid_orders"]

logger = logging.getLogger(__name__)

#: Moyens qui s'encaissent **avant** la préparation. Les espèces s'encaissent à
#: la remise : leur commande reste `pending` jusqu'à ce que la cuisine la
#: confirme, et c'est normal.
EN_LIGNE = (PaymentMethod.MOBILE_MONEY, PaymentMethod.CARD)

MOTIF = "Paiement non reçu dans le délai imparti."


@shared_task
def expire_unpaid_orders(minutes: int | None = None) -> int:
    """Annule les commandes payables en ligne restées impayées.

    Une telle commande naît `pending` et n'est confirmée que par
    l'encaissement. Si le client abandonne, rien ne l'en faisait sortir : elle
    gardait son stock réservé et son code promotionnel consommé, et le suivi du
    client annonçait une commande qui ne serait jamais préparée.

    **Le délai court depuis la dernière tentative de paiement**, pas depuis la
    commande : un client qui a ouvert son paiement à la vingt-neuvième minute
    n'est pas interrompu à la trentième.

    Le paiement partagé est laissé de côté : chaque convive règle sa part à
    son heure, et c'est le partage qui porte son échéance.

    L'annulation passe par `OrderService.transition_to`, comme toutes les
    autres : remise en stock, code libéré, journal, diffusion et notification
    du client s'y font une seule fois. Une transaction encore ouverte chez le
    prestataire peut aboutir après coup ; l'encaissement est alors enregistré
    et l'exploitation prévenue qu'il est à rembourser
    (`notifications.receivers.on_payment_to_refund`).
    """
    delai = minutes if minutes is not None else settings.PAYMENT_UNPAID_ORDER_TTL_MINUTES
    horizon = timezone.now() - dt.timedelta(minutes=delai)

    tentative_recente = Transaction.objects.filter(order=OuterRef("pk"), created_at__gte=horizon)
    impayees = (
        Order.objects.filter(
            status=OrderStatus.PENDING,
            payment_method__in=EN_LIGNE,
            placed_at__lt=horizon,
            split_payment__isnull=True,
        )
        .exclude(Exists(tentative_recente))
        .order_by("placed_at")
    )

    annulees = 0
    for order in impayees.iterator():
        try:
            OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason=MOTIF)
        except (BusinessRuleViolation, IllegalTransition):
            # Confirmée entre la lecture et le verrou — l'encaissement est
            # arrivé : c'est exactement ce qu'on attendait, rien à faire.
            logger.info("paiement.expiration_evitee", extra={"order": str(order.pk)})
            continue
        annulees += 1
    return annulees
