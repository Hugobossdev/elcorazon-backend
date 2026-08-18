"""Points d'entrée du suivi.

Deux routes, deux sens :

* le **livreur** dépose ses positions sur une course qui est la sienne (L3) ;
* le **client** lit le suivi d'une commande qui est la sienne.

Ces deux routes sont HTTP. Le temps réel — WebSocket, diffusion à chaque
relevé — est la phase 5 ; ce qui est ici en est la couche de persistance et de
secours, celle qu'un client utilise quand le socket est tombé.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.delivery.models import Assignment
from apps.delivery.serializers import CourierPublicSerializer
from apps.delivery.states import DeliveryStatus
from apps.delivery.views import courier_of
from apps.orders.models import Order
from apps.tracking.serializers import (
    LocationPingSerializer,
    PingWriteSerializer,
    TrackingSerializer,
)
from apps.tracking.services import TrackingService
from common.permissions import IsCourier, authenticated_user
from common.throttling import TrackingPingThrottle

__all__ = ["OrderTrackingView", "PingView"]


class PingView(APIView):
    """`POST /tracking/assignments/{id}/pings/` — dépôt d'une position."""

    permission_classes = [IsCourier]
    throttle_classes = [TrackingPingThrottle]

    @extend_schema(
        request=PingWriteSerializer,
        responses={201: LocationPingSerializer, 202: None},
        tags=["tracking"],
    )
    def post(self, request: Request, assignment_id: str) -> Response:
        """L3 — la course doit être celle de l'appelant.

        Le filtre est dans la requête, pas dans une permission d'objet : la
        course d'un collègue est introuvable, ce qui n'apprend rien à qui
        essaierait des identifiants.

        Deux issues de succès : `201` quand le relevé a été persisté, `202`
        quand l'échantillonnage l'a écarté. Le second n'est pas un échec — la
        position a été reçue et la position du dossier rafraîchie — mais le
        distinguer permet au client de savoir ce qui a été gardé.
        """
        courier = courier_of(request)
        assignment = get_object_or_404(Assignment, pk=assignment_id, courier=courier)

        serializer = PingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ping = TrackingService.record(
            assignment=assignment, courier=courier, **serializer.validated_data
        )
        if ping is None:
            return Response(status=status.HTTP_202_ACCEPTED)

        return Response(LocationPingSerializer(ping).data, status=status.HTTP_201_CREATED)


class OrderTrackingView(APIView):
    """`GET /tracking/orders/{id}/` — où en est mon repas ?"""

    @extend_schema(responses={200: TrackingSerializer}, tags=["tracking"])
    def get(self, request: Request, order_id: str) -> Response:
        """Suivi d'une commande, réservé à son client.

        Une commande sans course active rend un suivi **vide plutôt qu'un
        404** : « pas encore de livreur » est l'état normal des premières
        minutes, et le client doit pouvoir afficher son écran de suivi sans
        traiter ce cas comme une erreur.
        """
        order = get_object_or_404(Order, pk=order_id, customer=authenticated_user(request))
        assignment = (
            order.assignments.select_related("courier__user")
            .exclude(status__in=[DeliveryStatus.DECLINED, DeliveryStatus.CANCELLED])
            .order_by("-offered_at")
            .first()
        )

        payload: dict[str, object] = {
            "order": order.pk,
            "assignment_status": assignment.status if assignment else "",
            "courier": CourierPublicSerializer(assignment.courier).data if assignment else {},
            "last_position": None,
            "estimated_delivery_at": order.estimated_delivery_at,
        }
        if assignment is not None:
            latest = TrackingService.latest_for(assignment)
            payload["last_position"] = (
                LocationPingSerializer(latest).data if latest is not None else None
            )

        return Response(payload)
