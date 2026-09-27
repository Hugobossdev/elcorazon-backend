"""Suivi de commande en temps réel — ADR-008, invariant L3.

Un seul socket sert les deux sens : le livreur y publie sa position, le client
et le personnel la reçoivent. Le droit de **lire** et le droit d'**écrire** n'y
sont pas les mêmes, et c'est tout l'enjeu — l'implémentation précédente
accordait les deux à quiconque connaissait un identifiant de commande.
"""

from __future__ import annotations

from typing import Any

from channels.db import database_sync_to_async
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import UserType
from apps.delivery.models import Assignment
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from apps.tracking.serializers import ChatMessageSerializer, PingWriteSerializer
from apps.tracking.services import TRACKABLE_STATUSES, TrackingService
from common.consumers import CLOSE_STALE, AuthorizedConsumer
from common.exceptions import BusinessRuleViolation
from common.realtime import order_chat_group, order_group, publish

__all__ = ["OrderChatConsumer", "OrderTrackingConsumer"]

#: Statuts de commande pendant lesquels un suivi a du sens.
#:
#: Une commande livrée ou annulée n'a plus rien à diffuser ; laisser le socket
#: s'ouvrir dessus entretiendrait des connexions que rien n'alimente.
TRACKABLE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.PICKED_UP,
        OrderStatus.ON_THE_WAY,
    }
)


class OrderTrackingConsumer(AuthorizedConsumer):
    """`ws/orders/{order_id}/tracking/`"""

    order_id: str
    may_publish: bool = False

    async def authorized_group(self) -> str | None:
        self.order_id = self.scope["url_route"]["kwargs"]["order_id"]
        allowed = await self._check_access()
        return order_group(self.order_id) if allowed else None

    @database_sync_to_async
    def _check_access(self) -> bool:
        """Trois publics, trois raisons d'être là — et une seule qui autorise
        à écrire.

        Le client parce que c'est sa commande, le personnel parce que c'est son
        établissement, le livreur parce que la course lui est assignée. Seul le
        troisième publiera : les deux autres reçoivent.
        """
        order = Order.objects.filter(pk=self.order_id).first()
        if order is None or order.status not in TRACKABLE_ORDER_STATUSES:
            return False

        if order.customer_id == self.user.pk:
            return True

        if self.user.user_type == UserType.COURIER:
            assigned = Assignment.objects.filter(
                order=order,
                courier__user=self.user,
                status__in=TRACKABLE_STATUSES,
            ).exists()
            self.may_publish = assigned
            return assigned

        if self.user.user_type == UserType.STAFF and self.user.has_permission("orders.read"):
            return is_unscoped(self.user) or order.restaurant_id in staff_restaurant_ids(self.user)

        return False

    async def receive_json(self, content: dict[str, Any], **kwargs: object) -> None:
        """Position émise par le livreur.

        Le droit d'écrire a été établi à la connexion, pas ici : un client ou
        un membre du personnel qui enverrait ce message est fermé sur-le-champ.
        Répondre par une simple erreur laisserait le socket ouvert à qui vient
        de montrer qu'il essaie autre chose que ce pour quoi il est là.
        """
        if not self.may_publish:
            await self.close(code=CLOSE_STALE)
            return

        try:
            recorded = await self._record(content)
        except (BusinessRuleViolation, KeyError, TypeError, ValueError) as exc:
            await self.send_json({"type": "error", "detail": str(exc)})
            return

        # La diffusion est **intégrale**, l'écriture ne l'est pas : le relevé
        # qui n'a pas mérité une ligne en base est quand même envoyé au client.
        # C'est la diffusion qui fait l'expérience de suivi ; la persistance ne
        # sert qu'au litige et à l'analyse.
        await database_sync_to_async(publish)(
            self.group,
            "tracking.position",
            {
                "lat": recorded["lat"],
                "lon": recorded["lon"],
                "recorded_at": recorded["recorded_at"],
                "persisted": recorded["persisted"],
            },
        )

    @database_sync_to_async
    def _record(self, content: dict[str, Any]) -> dict[str, Any]:
        serializer = PingWriteSerializer(data=content)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        assignment = Assignment.objects.select_related("courier").get(
            order_id=self.order_id,
            courier__user=self.user,
            status__in=TRACKABLE_STATUSES,
        )
        ping = TrackingService.record(assignment=assignment, courier=assignment.courier, **data)

        point: Point = data["point"]
        return {
            "lat": point.y,
            "lon": point.x,
            "recorded_at": data["recorded_at"].isoformat(),
            "persisted": ping is not None,
        }


class OrderChatConsumer(AuthorizedConsumer):
    """`ws/orders/{order_id}/chat/` — relais client ↔ livreur, ADR-008.

    Contrairement au suivi, les deux parties publient : c'est une
    conversation, pas un flux à sens unique. Le personnel n'y a pas accès —
    ADR-008 ne cite que le client et le livreur pour ce groupe. Le message
    n'est ni persisté ni journalisé (Phase 1 §5) : ce consommateur ne fait que
    le relayer, il ne l'écrit nulle part.
    """

    order_id: str
    sender_type: str = ""

    async def authorized_group(self) -> str | None:
        self.order_id = self.scope["url_route"]["kwargs"]["order_id"]
        allowed = await self._check_access()
        return order_chat_group(self.order_id) if allowed else None

    @database_sync_to_async
    def _check_access(self) -> bool:
        """Même périmètre que le suivi, moins le personnel.

        Une commande livrée ou annulée n'a plus de conversation à tenir —
        ouvrir le socket dessus entretiendrait un canal que plus rien
        n'alimente, comme pour `OrderTrackingConsumer`.
        """
        order = Order.objects.filter(pk=self.order_id).first()
        if order is None or order.status not in TRACKABLE_ORDER_STATUSES:
            return False

        if order.customer_id == self.user.pk:
            self.sender_type = "customer"
            return True

        if self.user.user_type == UserType.COURIER:
            assigned = Assignment.objects.filter(
                order=order,
                courier__user=self.user,
                status__in=TRACKABLE_STATUSES,
            ).exists()
            if assigned:
                self.sender_type = "courier"
            return assigned

        return False

    async def receive_json(self, content: dict[str, Any], **kwargs: object) -> None:
        try:
            text = await self._validated_text(content)
        except (ValidationError, KeyError, TypeError):
            await self.send_json({"type": "error", "detail": "Message invalide."})
            return

        # L'émetteur vient de la connexion authentifiée à l'ouverture du
        # socket, jamais d'un champ du message : c'est la même garde que pour
        # la position du livreur, appliquée ici à l'identité plutôt qu'au
        # droit d'écrire.
        await database_sync_to_async(publish)(
            self.group,
            "chat.message",
            {
                "sender": self.sender_type,
                "text": text,
                "sent_at": timezone.now().isoformat(),
            },
        )

    @database_sync_to_async
    def _validated_text(self, content: dict[str, Any]) -> str:
        serializer = ChatMessageSerializer(data=content)
        serializer.is_valid(raise_exception=True)
        text: str = serializer.validated_data["text"]
        return text
