"""Abonnement aux commandes livrées — ADR-002.

`orders` annonce, `loyalty` écoute. La flèche va dans ce sens et pas dans
l'autre : le graphe de dépendances la déclare en pointillés — événementielle —
précisément pour que fidélité, gamification et analytics puissent s'y brancher
sans que `orders` les connaisse.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.loyalty.services import LoyaltyService
from apps.loyalty.subscriptions import SubscriptionService
from apps.orders.models import Order
from apps.orders.signals import order_status_changed
from apps.orders.states import OrderStatus
from apps.payments.models import Transaction
from apps.payments.signals import payment_transaction_settled

__all__ = ["on_order_delivered", "on_payment_settled"]


@receiver(order_status_changed, sender=Order, dispatch_uid="loyalty.order_delivered")
def on_order_delivered(sender: type[Order], *, order: Order, target: str, **kwargs: Any) -> None:
    """Crédite les points d'une commande livrée.

    **À la livraison et pas au paiement.** Une commande payée puis annulée
    rapporterait des points pour un repas jamais reçu, et les reprendre ensuite
    demanderait un débit que le client ne comprendrait pas.

    Le crédit est idempotent par contrainte de base : un événement rejoué ne
    crédite pas deux fois.
    """
    if target != OrderStatus.DELIVERED:
        return

    LoyaltyService.earn(user=order.customer, order=order)


@receiver(payment_transaction_settled, sender=Transaction, dispatch_uid="loyalty.payment_settled")
def on_payment_settled(
    sender: type[Transaction], *, transaction: Transaction, **kwargs: Any
) -> None:
    """Active ou prolonge l'abonnement réglé par cette transaction.

    Sans effet si la transaction ne règle pas d'abonnement — c'est le cas de
    la plupart, et `SubscriptionService.on_payment_settled` le vérifie avant
    d'écrire quoi que ce soit.
    """
    SubscriptionService.on_payment_settled(transaction)
