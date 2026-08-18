"""Points d'entrée du panier.

Le panier est **par client et par restaurant** : il s'adresse par le slug du
restaurant plutôt que par un identifiant que le client devrait retenir.
`GET /carts/{slug}/` crée le panier vide s'il n'existe pas encore, ce qui évite
au client d'avoir à distinguer « pas encore de panier » de « panier vide » —
deux états qu'aucun écran ne montre différemment.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.carts.models import Cart, CartLine
from apps.carts.serializers import (
    CartLineWriteSerializer,
    CartSerializer,
    QuantitySerializer,
)
from apps.carts.services import CartService, price_cart
from apps.restaurants.models import Restaurant
from common.permissions import IsCustomer, authenticated_user
from common.throttling import CartWriteThrottle

__all__ = ["CartViewSet"]

#: `line_id` n'est pas un champ de `Cart` : le générateur ne peut pas en
#: déduire le type, et le déclarer vaut mieux qu'un `string` par défaut.
LINE_ID = OpenApiParameter(
    name="line_id",
    location=OpenApiParameter.PATH,
    type=OpenApiTypes.UUID,
    description="Identifiant de la ligne de panier.",
)


class CartViewSet(GenericViewSet[Cart]):
    """Panier serveur du client authentifié.

    Réservé aux clients : ni le personnel ni un livreur n'ont de panier, et
    leur en ouvrir un créerait des commandes sans destinataire réel.
    """

    permission_classes = [IsCustomer]
    serializer_class = CartSerializer
    throttle_classes = [CartWriteThrottle]
    # Déclaré pour le générateur de schéma seulement : `get_queryset` exige un
    # utilisateur, que la génération hors requête n'a pas.
    queryset = Cart.objects.none()
    lookup_field = "restaurant__slug"
    lookup_url_kwarg = "slug"

    def get_queryset(self) -> QuerySet[Cart]:
        # L'appartenance est un filtre, pas une permission : le panier d'autrui
        # est introuvable plutôt qu'interdit.
        return Cart.objects.filter(user=authenticated_user(self.request))

    def _restaurant(self, slug: str) -> Restaurant:
        return get_object_or_404(Restaurant, slug=slug, is_active=True)

    def _cart(self, slug: str) -> Cart:
        cart = CartService.cart_for(authenticated_user(self.request), self._restaurant(slug))
        return CartService.load(cart)

    def _rendered(self, slug: str, http_status: int = status.HTTP_200_OK) -> Response:
        """Le panier entier est renvoyé après chaque écriture.

        Une réponse qui ne rendrait que la ligne touchée obligerait le client à
        recalculer le sous-total lui-même — donc à réimplémenter la
        tarification côté mobile, ce que C1 cherche justement à éviter.
        """
        return Response(CartSerializer(price_cart(self._cart(slug))).data, status=http_status)

    @extend_schema(responses={200: CartSerializer}, tags=["carts"])
    def retrieve(self, request: Request, slug: str) -> Response:
        return self._rendered(slug)

    @extend_schema(responses={200: CartSerializer(many=True)}, tags=["carts"])
    def list(self, request: Request) -> Response:
        """Tous les paniers en cours, un par restaurant entamé."""
        carts = [
            price_cart(CartService.load(cart))
            for cart in self.get_queryset().order_by("-updated_at")
        ]
        return Response(CartSerializer(carts, many=True).data)

    @extend_schema(request=CartLineWriteSerializer, responses={201: CartSerializer}, tags=["carts"])
    @action(detail=True, methods=["post"], url_path="lines")
    def add_line(self, request: Request, slug: str) -> Response:
        serializer = CartLineWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        CartService.add_line(cart=self._cart(slug), **serializer.validated_data)
        return self._rendered(slug, status.HTTP_201_CREATED)

    @extend_schema(
        request=QuantitySerializer,
        responses={200: CartSerializer},
        parameters=[LINE_ID],
        tags=["carts"],
    )
    @action(detail=True, methods=["patch"], url_path=r"lines/(?P<line_id>[^/.]+)")
    def set_quantity(self, request: Request, slug: str, line_id: str) -> Response:
        serializer = QuantitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        CartService.set_quantity(self._line(slug, line_id), serializer.validated_data["quantity"])
        return self._rendered(slug)

    @extend_schema(responses={200: CartSerializer}, parameters=[LINE_ID], tags=["carts"])
    @set_quantity.mapping.delete
    def remove_line(self, request: Request, slug: str, line_id: str) -> Response:
        self._line(slug, line_id).delete()
        return self._rendered(slug)

    @extend_schema(operation_id="carts_clear", responses={200: CartSerializer}, tags=["carts"])
    @add_line.mapping.delete
    def clear(self, request: Request, slug: str) -> Response:
        CartService.clear(self._cart(slug))
        return self._rendered(slug)

    def _line(self, slug: str, line_id: str) -> CartLine:
        """Ligne du panier de l'appelant, ou 404.

        Le filtre remonte jusqu'au propriétaire du panier : sans lui, un
        identifiant de ligne deviné permettrait de modifier le panier d'un
        autre client.
        """
        return get_object_or_404(
            CartLine,
            pk=line_id,
            cart__restaurant__slug=slug,
            cart__user=authenticated_user(self.request),
        )
