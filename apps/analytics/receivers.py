"""Abonnement aux commandes — ADR-002.

Même mécanisme que `loyalty` et `gamification` : `orders` annonce, `analytics`
écoute. Chaque transition consigne un événement — c'est ce qui rend un
tableau de bord « commandes par statut, par heure » possible sans que
`orders` sache que `analytics` existe.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.analytics.services import AnalyticsService
from apps.orders.models import Order
from apps.orders.signals import order_status_changed

__all__ = ["on_order_status_changed"]


@receiver(order_status_changed, sender=Order, dispatch_uid="analytics.order_status_changed")
def on_order_status_changed(
    sender: type[Order], *, order: Order, target: str, **kwargs: Any
) -> None:
    AnalyticsService.record(
        user=order.customer,
        event_type=f"order.{target}",
        data={"order_id": str(order.pk), "restaurant_id": str(order.restaurant_id)},
    )
