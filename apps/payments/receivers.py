"""Ce que `payments` fait des événements des autres modules.

`orders` ne connaît pas `payments` (ADR-002) : c'est donc ici qu'on écoute la
commande, jamais l'inverse.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.orders.models import Order
from apps.orders.signals import order_status_changed
from apps.orders.states import OrderStatus
from apps.payments.cash import record_cash_collected


@receiver(order_status_changed, sender=Order, dispatch_uid="payments.cash_on_delivery")
def on_order_delivered(sender: type[Order], *, order: Order, target: str, **kwargs: Any) -> None:
    """Une commande livrée en espèces a été payée à la porte.

    Synchrone, dans la transaction du passage en `delivered` : une livraison
    enregistrée sans son encaissement laisserait la commande « impayée ».
    """
    if target == OrderStatus.DELIVERED:
        record_cash_collected(order)
