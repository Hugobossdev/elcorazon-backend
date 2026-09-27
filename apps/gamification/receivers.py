"""Abonnement aux commandes livrées — ADR-002.

Même mécanisme que `loyalty` : `orders` annonce, `gamification` écoute, et
aucun des deux ne connaît l'autre dans le mauvais sens.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.gamification.services import GamificationService
from apps.orders.models import Order
from apps.orders.signals import order_status_changed
from apps.orders.states import OrderStatus

__all__ = ["on_order_delivered"]


@receiver(order_status_changed, sender=Order, dispatch_uid="gamification.order_delivered")
def on_order_delivered(sender: type[Order], *, order: Order, target: str, **kwargs: Any) -> None:
    if target != OrderStatus.DELIVERED:
        return

    GamificationService.on_order_delivered(user=order.customer, order=order)
