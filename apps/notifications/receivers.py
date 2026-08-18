"""Abonnements aux événements de domaine — ADR-002, ADR-008.

C'est ici que `notifications` réagit à ce que font les autres apps. La flèche va
dans ce sens et pas dans l'autre : `orders` et `delivery` annoncent sans savoir
qui écoute, ce module écoute sans qu'ils le sachent. Ajouter une notification
sur un événement existant ne touche aucune autre app.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.delivery.models import Assignment
from apps.delivery.signals import assignment_offered
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.orders.models import Order
from apps.orders.signals import order_status_changed
from apps.orders.states import OrderStatus

__all__ = ["on_assignment_offered", "on_order_status_changed"]

#: Étapes annoncées au client, et ce qu'on lui dit.
#:
#: La liste est délibérément courte. `preparing` et `ready` sont des étapes de
#: cuisine : les annoncer ferait vibrer le téléphone sans rien apprendre
#: d'actionnable. Notifier chaque transition est le meilleur moyen de se faire
#: couper les notifications — et de perdre du même coup celles qui comptent.
CUSTOMER_ANNOUNCEMENTS: dict[str, tuple[str, str]] = {
    OrderStatus.CONFIRMED: ("Commande confirmée", "Votre commande {reference} est confirmée."),
    OrderStatus.ON_THE_WAY: ("En route", "Votre commande {reference} arrive."),
    OrderStatus.DELIVERED: ("Livrée", "Votre commande {reference} a été livrée. Bon appétit !"),
    OrderStatus.CANCELLED: ("Commande annulée", "Votre commande {reference} a été annulée."),
}


@receiver(order_status_changed, sender=Order, dispatch_uid="notifications.order_status")
def on_order_status_changed(
    sender: type[Order], *, order: Order, target: str, **kwargs: Any
) -> None:
    """Prévient le client des étapes qui le concernent.

    `dispatch_uid` protège du double abonnement : sans lui, un module importé
    deux fois — ce qui arrive au rechargement automatique en développement —
    enverrait deux notifications par transition, et le défaut ne se verrait
    qu'à l'usage.
    """
    message = CUSTOMER_ANNOUNCEMENTS.get(target)
    if message is None:
        return

    title, body = message
    notify(
        user=order.customer,
        kind=NotificationKind.ORDER_STATUS,
        title=title,
        body=body.format(reference=order.reference),
        data={"order": str(order.pk), "status": target},
    )


@receiver(assignment_offered, sender=Assignment, dispatch_uid="notifications.delivery_offer")
def on_assignment_offered(
    sender: type[Assignment], *, assignment: Assignment, **kwargs: Any
) -> None:
    """Prévient le livreur qu'une course l'attend.

    C'est le seul flux où rater un événement a un coût métier direct (ADR-008) :
    le livreur n'a pas son application au premier plan en roulant, et une
    course non vue est un repas qui refroidit. Le WebSocket ne suffit donc pas
    — la notification le double.
    """
    order = assignment.order
    notify(
        user=assignment.courier.user,
        kind=NotificationKind.DELIVERY_OFFER,
        title="Nouvelle course",
        body=f"{order.restaurant.name} — {order.delivery_address_line}",
        data={"assignment": str(assignment.pk), "order": str(order.pk)},
    )
