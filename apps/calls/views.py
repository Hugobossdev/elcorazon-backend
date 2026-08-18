"""Points d'entrée des appels.

Toutes les routes sont réservées aux deux parties de l'appel : le client de la
commande et le livreur de sa course. Le personnel n'y a pas accès — écouter la
conversation d'un client avec son livreur n'est pas une fonction
d'exploitation.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.calls.models import Call
from apps.calls.serializers import (
    CallPlaceSerializer,
    CallSerializer,
    RtcCredentialsSerializer,
)
from apps.calls.services import CallService
from apps.orders.models import Order
from common.permissions import authenticated_user

__all__ = ["CallViewSet", "PlaceCallView"]


class CallViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet[Call]):
    """`/calls/` — les appels auxquels l'appelant a pris part.

    Le filtre de requête porte l'autorisation (ADR-005) : il n'y a pas de
    permission d'objet à écrire ensuite, donc pas de permission à oublier.
    """

    serializer_class = CallSerializer
    queryset = Call.objects.none()  # pour le générateur de schéma

    def get_queryset(self) -> QuerySet[Call]:
        user = authenticated_user(self.request)
        return (
            Call.objects.filter(Q(caller=user) | Q(callee=user))
            .select_related("caller", "callee")
            .order_by("-created_at")
        )

    @extend_schema(request=None, responses={200: CallSerializer}, tags=["calls"])
    @action(detail=True, methods=["post"])
    def accept(self, request: Request, pk: str) -> Response:
        call = CallService.accept(call=self.get_object(), actor=authenticated_user(request))
        return Response(CallSerializer(call).data)

    @extend_schema(request=None, responses={200: CallSerializer}, tags=["calls"])
    @action(detail=True, methods=["post"])
    def decline(self, request: Request, pk: str) -> Response:
        call = CallService.decline(call=self.get_object(), actor=authenticated_user(request))
        return Response(CallSerializer(call).data)

    @extend_schema(request=None, responses={200: CallSerializer}, tags=["calls"])
    @action(detail=True, methods=["post"])
    def end(self, request: Request, pk: str) -> Response:
        call = CallService.end(call=self.get_object(), actor=authenticated_user(request))
        return Response(CallSerializer(call).data)

    @extend_schema(responses={200: RtcCredentialsSerializer}, tags=["calls"])
    @action(detail=True, methods=["get"], url_path="rtc-token")
    def rtc_token(self, request: Request, pk: str) -> Response:
        """Jeton RTC de l'appelant pour cet appel.

        Demandé au moment de rejoindre le canal, et non délivré à la création :
        le destinataire n'a pas de jeton tant qu'il n'a pas décroché, et un
        jeton expiré se redemande sans rouvrir un appel.
        """
        credentials = CallService.credentials_for(
            call=self.get_object(), user=authenticated_user(request)
        )
        return Response(RtcCredentialsSerializer(credentials).data)


class PlaceCallView(APIView):
    """`POST /calls/orders/{order}/` — appeler l'autre partie de cette commande.

    La commande est cherchée **parmi celles que l'appelant peut voir** : la
    sienne s'il est client, celle de sa course s'il est livreur. Le service
    tranche ensuite qui est le destinataire — le client ne le désigne pas.
    """

    @extend_schema(request=CallPlaceSerializer, responses={201: CallSerializer}, tags=["calls"])
    def post(self, request: Request, order_id: str) -> Response:
        user = authenticated_user(request)
        order = get_object_or_404(
            Order.objects.filter(Q(customer=user) | Q(assignments__courier__user=user)).distinct(),
            pk=order_id,
        )

        payload = CallPlaceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        call = CallService.place(order=order, caller=user, kind=payload.validated_data["kind"])
        return Response(CallSerializer(call).data, status=status.HTTP_201_CREATED)
