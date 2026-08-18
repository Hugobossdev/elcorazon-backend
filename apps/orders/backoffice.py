"""Supervision des commandes — l'API que consomme l'application `admin`.

C'est l'écran devant lequel se passe le service : la liste de ce qui est en
cours, l'avancement d'un statut, l'annulation de ce qui ne partira pas.

Séparé de `views.py` pour la raison qu'énonce `catalog/urls.py` — **un chemin,
un public, une permission**. Les deux publics vivaient jusqu'ici sur les mêmes
routes, distingués par le seul `get_queryset`, et cela avait deux conséquences
qui ne se voyaient pas :

* `orders.read` ne gardait rien. Le registre de l'ADR-005 la déclare, mais
  aucune route ne l'exigeait : tout compte du personnel lisait les commandes de
  son établissement, y compris celui à qui l'on avait précisément refusé ce
  droit. Une permission qui n'est appliquée nulle part est pire qu'absente —
  elle donne le sentiment d'avoir décidé ;
* le verbe d'annulation du client était joignable par le livreur. Son
  `permission_classes` implicite était « authentifié », et le `get_queryset`
  d'alors rendait au livreur les commandes qui lui étaient confiées : il pouvait
  donc annuler la commande qu'il transportait.

Ici, chaque action nomme sa permission, et l'audit statique de
`tests/architecture/test_layers.py` peut la lire sans exécuter la vue.

Le cloisonnement par établissement (ADR-005, troisième étage) reste un filtre de
requête : une commande hors périmètre est **introuvable**, pas interdite. Sur
une ressource dont les identifiants sont des UUID, la nuance est mince ; elle
compte tout de même sur `reference`, qui est séquentielle et se devine.
"""

from __future__ import annotations

from typing import ClassVar

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import GenericViewSet

from apps.orders.models import Order
from apps.orders.serializers import (
    OrderDetailSerializer,
    OrderSerializer,
    StaffCancelSerializer,
    StatusTransitionSerializer,
)
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from common.exceptions import BusinessRuleViolation
from common.permissions import HasPermission, authenticated_user

__all__ = ["ManagedOrderViewSet"]


class ManagedOrderViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet[Order]):
    """Commandes de l'établissement — lecture, avancement, annulation.

    Aucune création ici : une commande naît d'un panier client, jamais d'un
    écran d'exploitation. Aucune suppression non plus — c'est une pièce
    comptable, et ce qui n'a pas eu lieu s'annule au lieu de disparaître.
    """

    # En n-uplet et non en liste, comme dans les autres back-offices :
    # `permission_classes` est une variable d'instance sur `APIView`, et
    # l'annoter `ClassVar` — ce qu'exigerait une liste mutable — est refusé par
    # le vérificateur de types.
    permission_classes = (HasPermission.of("orders.read"),)

    # Déclaré pour le générateur de schéma seulement — voir `get_queryset`.
    queryset = Order.objects.none()

    #: `placed_at` en intervalle parce que l'écran de supervision demande
    #: toujours la même chose — « le service en cours » — et que la seule façon
    #: de l'exprimer sans borne serait de tout charger pour n'en afficher que la
    #: fin.
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact"],
        "restaurant__slug": ["exact"],
        "customer": ["exact"],
        "placed_at": ["gte", "lte"],
    }

    def get_serializer_class(self) -> type[BaseSerializer[Order]]:
        return OrderDetailSerializer if self.action == "retrieve" else OrderSerializer

    def get_queryset(self) -> QuerySet[Order]:
        user = authenticated_user(self.request)
        queryset = Order.objects.select_related("restaurant", "customer").order_by("-placed_at")

        if not is_unscoped(user):
            queryset = queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

        if self.action == "retrieve":
            queryset = queryset.prefetch_related("lines__menu_item", "status_events")
        return queryset

    @extend_schema(
        request=StatusTransitionSerializer,
        responses={200: OrderDetailSerializer},
        tags=["orders"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="status",
        url_name="status",
        permission_classes=[HasPermission.of("orders.update_status")],
    )
    def update_status(self, request: Request, pk: str) -> Response:
        """Avance le statut.

        Aucune vérification de flux ici : la machine décide, et une transition
        refusée sort en 409 avec les cibles autorisées, ce qui permet à
        l'application d'afficher les boutons justes.

        `cancelled` en est **exclu**, et c'est le seul cas particulier du
        module. La machine l'accepte comme cible depuis quatre états, si bien
        que le laisser passer ici ferait de `orders.update_status` un droit
        d'annuler — et viderait `orders.cancel` de son sens. Le registre
        distingue les deux parce que l'exploitation les distingue : faire
        avancer le service est le geste de tous les jours, annuler la commande
        d'un tiers ne l'est pas.
        """
        serializer = StatusTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target = serializer.validated_data["status"]
        if target == OrderStatus.CANCELLED:
            raise BusinessRuleViolation(
                "L'annulation ne passe pas par cette route : appelez `cancel`, "
                "qui exige la permission `orders.cancel` et un motif.",
                current_status=self.get_object().status,
            )

        order = OrderService.transition_to(
            order=self.get_object(),
            target=target,
            actor=authenticated_user(request),
            reason=serializer.validated_data["reason"],
        )
        return Response(OrderDetailSerializer(order).data)

    @extend_schema(
        request=StaffCancelSerializer, responses={200: OrderDetailSerializer}, tags=["orders"]
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[HasPermission.of("orders.cancel")],
    )
    def cancel(self, request: Request, pk: str) -> Response:
        """Annule une commande, motif obligatoire.

        Va plus loin que l'annulation du client — jusqu'à `ready` — parce que
        c'est le cas qu'elle ne couvre pas : la rupture découverte en cuisine,
        l'adresse introuvable, le client injoignable.
        """
        serializer = StaffCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = OrderService.cancel_by_staff(
            order=self.get_object(),
            actor=authenticated_user(request),
            reason=serializer.validated_data["reason"],
        )
        return Response(OrderDetailSerializer(order).data)
