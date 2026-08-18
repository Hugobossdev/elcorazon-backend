"""Points d'entrée du panier collaboratif.

Le panier s'adresse par son identifiant, contrairement au panier personnel qui
s'adresse par le slug du restaurant : il y en a un par groupe, pas un par
établissement, et c'est l'invitation qui en fait connaître l'identifiant.

**L'appartenance est un filtre, pas une permission** : le panier d'un groupe
auquel on n'appartient pas est introuvable plutôt qu'interdit. Un 403 confirmerait
l'existence du panier — et donc celle du code — à qui essaie des identifiants.
Seul `join` échappe au filtre, forcément : on y entre avant d'être membre, et le
code d'invitation y tient le rôle de l'autorisation.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.groupcarts.models import GroupCart, GroupCartLine
from apps.groupcarts.serializers import (
    GroupCartCancelSerializer,
    GroupCartConfirmSerializer,
    GroupCartLineWriteSerializer,
    GroupCartOpenSerializer,
    GroupCartSerializer,
    JoinByCodeSerializer,
    QuantitySerializer,
)
from apps.groupcarts.services import GroupCartService
from apps.orders.serializers import OrderDetailSerializer
from common.permissions import IsCustomer, authenticated_user
from common.throttling import CartWriteThrottle

__all__ = ["GroupCartViewSet"]

LINE_ID = OpenApiParameter(
    name="line_id",
    location=OpenApiParameter.PATH,
    type=OpenApiTypes.UUID,
    description="Identifiant de la ligne de panier collaboratif.",
)


class GroupCartViewSet(GenericViewSet[GroupCart]):
    """Paniers collaboratifs du client authentifié.

    Réservé aux clients, comme le panier personnel : ni le personnel ni un livreur
    ne commandent à déjeuner par ce chemin, et leur en ouvrir la possibilité
    créerait des commandes sans destinataire réel.
    """

    permission_classes = [IsCustomer]
    serializer_class = GroupCartSerializer
    throttle_classes = [CartWriteThrottle]
    queryset = GroupCart.objects.none()

    def get_queryset(self) -> QuerySet[GroupCart]:
        return GroupCart.objects.filter(
            members__user=authenticated_user(self.request)
        ).select_related("restaurant", "host")

    # ------------------------------------------------------------------ rendu

    def _rendered(self, group_cart: GroupCart, http_status: int = status.HTTP_200_OK) -> Response:
        """Le panier entier est renvoyé après chaque écriture.

        Une réponse limitée à la ligne touchée obligerait chaque client à
        recalculer le sous-total et les totaux par participant — donc à
        réimplémenter la tarification côté mobile, ce que C1 cherche à éviter, et
        cette fois en autant d'exemplaires qu'il y a de participants.
        """
        loaded = GroupCartService.load(group_cart)
        selection = GroupCartService.price(loaded)
        payload: dict[str, Any] = {
            "group_cart": loaded,
            "members": list(loaded.members.all()),
            "lines": selection.lines,
            "per_member": [
                {"member": member, "total": total}
                for member, total in GroupCartService.price_per_member(loaded).items()
            ],
            "currency": selection.currency,
            "subtotal": selection.subtotal,
            "is_orderable": selection.is_orderable,
        }
        return Response(GroupCartSerializer(payload).data, status=http_status)

    # ------------------------------------------------------------ consultation

    @extend_schema(responses={200: GroupCartSerializer(many=True)}, tags=["group-carts"])
    def list(self, request: Request) -> Response:
        """Les paniers de groupe auxquels je participe."""
        return Response(
            [
                self._rendered(group_cart).data
                for group_cart in self.get_queryset().order_by("-created_at")
            ]
        )

    @extend_schema(responses={200: GroupCartSerializer}, tags=["group-carts"])
    def retrieve(self, request: Request, pk: str) -> Response:
        return self._rendered(self.get_object())

    # --------------------------------------------------------------- ouverture

    @extend_schema(
        request=GroupCartOpenSerializer,
        responses={201: GroupCartSerializer},
        tags=["group-carts"],
    )
    def create(self, request: Request) -> Response:
        serializer = GroupCartOpenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        group_cart = GroupCartService.open(
            host=authenticated_user(request), **serializer.validated_data
        )
        return self._rendered(group_cart, status.HTTP_201_CREATED)

    @extend_schema(
        request=JoinByCodeSerializer, responses={200: GroupCartSerializer}, tags=["group-carts"]
    )
    @action(detail=False, methods=["post"])
    def join(self, request: Request) -> Response:
        """Rejoindre par code d'invitation.

        Sur `detail=False` : celui qui rejoint ne connaît pas l'identifiant du
        panier, seulement le code qu'on lui a transmis. Exiger l'identifiant dans
        l'URL l'obligerait à le deviner — ou nous à publier une route qui le
        révèle.
        """
        serializer = JoinByCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        group_cart = GroupCartService.by_code(serializer.validated_data["code"])
        GroupCartService.join(group_cart=group_cart, user=authenticated_user(request))
        return self._rendered(group_cart)

    # ------------------------------------------------------------ contributions

    @extend_schema(
        request=GroupCartLineWriteSerializer,
        responses={201: GroupCartSerializer},
        tags=["group-carts"],
    )
    @action(detail=True, methods=["post"], url_path="lines")
    def add_line(self, request: Request, pk: str) -> Response:
        serializer = GroupCartLineWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        group_cart = self.get_object()
        GroupCartService.add_line(
            group_cart=group_cart,
            member=authenticated_user(request),
            **serializer.validated_data,
        )
        return self._rendered(group_cart, status.HTTP_201_CREATED)

    @extend_schema(
        request=QuantitySerializer,
        responses={200: GroupCartSerializer},
        parameters=[LINE_ID],
        tags=["group-carts"],
    )
    @action(detail=True, methods=["patch"], url_path=r"lines/(?P<line_id>[^/.]+)")
    def set_quantity(self, request: Request, pk: str, line_id: str) -> Response:
        serializer = QuantitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        group_cart = self.get_object()
        GroupCartService.set_quantity(
            line=self._line(group_cart, line_id),
            actor=authenticated_user(request),
            quantity=serializer.validated_data["quantity"],
        )
        return self._rendered(group_cart)

    @extend_schema(responses={200: GroupCartSerializer}, parameters=[LINE_ID], tags=["group-carts"])
    @set_quantity.mapping.delete
    def remove_line(self, request: Request, pk: str, line_id: str) -> Response:
        group_cart = self.get_object()
        GroupCartService.remove_line(
            line=self._line(group_cart, line_id), actor=authenticated_user(request)
        )
        return self._rendered(group_cart)

    def _line(self, group_cart: GroupCart, line_id: str) -> GroupCartLine:
        """Ligne **de ce panier**, ou 404.

        Le filtre sur le panier est indispensable : sans lui, un identifiant de
        ligne appartenant à un autre groupe serait accepté, et le contrôle d'auteur
        du service porterait sur le mauvais panier.
        """
        return get_object_or_404(GroupCartLine, pk=line_id, group_cart=group_cart)

    # ------------------------------------------------------------- transitions

    @extend_schema(responses={200: GroupCartSerializer}, tags=["group-carts"])
    @action(detail=True, methods=["post"])
    def lock(self, request: Request, pk: str) -> Response:
        """Clore les ajouts sans commander — réservé à l'hôte."""
        group_cart = GroupCartService.lock(
            group_cart=self.get_object(), actor=authenticated_user(request)
        )
        return self._rendered(group_cart)

    @extend_schema(
        request=GroupCartConfirmSerializer,
        responses={201: OrderDetailSerializer},
        tags=["group-carts"],
    )
    @action(detail=True, methods=["post"])
    def confirm(self, request: Request, pk: str) -> Response:
        """Transformer le panier en une commande — réservé à l'hôte.

        Pas d'en-tête d'idempotence ici, contrairement à `POST /orders/`, et ce
        n'est pas un oubli : le panier collaboratif **est** la clé. Un second appel
        trouve un panier déjà `confirmed`, et la machine à états refuse la
        transition ; deux appels simultanés sont sérialisés par le verrou de ligne.
        Une clé fournie par le client n'ajouterait rien à une garantie que le
        modèle donne déjà, et pourrait être omise le jour où le réseau coupe.
        """
        serializer = GroupCartConfirmSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        order = GroupCartService.confirm(
            group_cart=self.get_object(),
            actor=authenticated_user(request),
            **serializer.validated_data,
        )
        return Response(OrderDetailSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=GroupCartCancelSerializer,
        responses={200: GroupCartSerializer},
        tags=["group-carts"],
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str) -> Response:
        """Renoncer — réservé à l'hôte."""
        serializer = GroupCartCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        group_cart = GroupCartService.cancel(
            group_cart=self.get_object(),
            actor=authenticated_user(request),
            reason=serializer.validated_data["reason"],
        )
        return self._rendered(group_cart)
