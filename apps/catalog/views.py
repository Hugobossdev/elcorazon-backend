"""Points d'entrée du catalogue.

La lecture est publique : parcourir un menu ne demande pas de compte, et
l'exiger ferait perdre le visiteur avant qu'il ait vu un prix.

L'écriture d'un avis, elle, est réservée aux clients authentifiés — c'est le
seul verbe non sûr de ce module, et la seule route qui passe par un service.
"""

from __future__ import annotations

from django.db.models import Prefetch, QuerySet
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.mixins import CreateModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.throttling import BaseThrottle
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from apps.catalog.filters import MenuItemFilter
from apps.catalog.models import Category, MenuItem, Option, OptionGroup, Review
from apps.catalog.serializers import (
    CategorySerializer,
    MenuItemDetailSerializer,
    MenuItemSerializer,
    ReviewSerializer,
    ReviewWriteSerializer,
)
from apps.catalog.services import ReviewService
from common.permissions import IsCustomer, authenticated_user
from common.throttling import (
    ResilientAnonRateThrottle,
    ResilientUserRateThrottle,
    ReviewWriteThrottle,
)

__all__ = ["CategoryViewSet", "MenuItemViewSet", "ReviewViewSet"]


class CategoryViewSet(ReadOnlyModelViewSet[Category]):
    """Catégories actives d'un restaurant."""

    serializer_class = CategorySerializer
    permission_classes = [AllowAny]
    queryset = (
        Category.objects.filter(is_active=True, restaurant__is_active=True)
        .select_related("restaurant")
        .order_by("sort_order", "name")
    )
    filterset_fields = {"restaurant__slug": ["exact"]}
    pagination_class = None  # Une carte compte une dizaine de catégories : les
    # paginer obligerait chaque client à boucler pour afficher un menu complet.


class MenuItemViewSet(ReadOnlyModelViewSet[MenuItem]):
    """Articles du catalogue.

    Les articles logiquement supprimés sont exclus (`alive()`) : ils n'existent
    plus pour le client, tout en restant lisibles depuis les commandes passées
    qui en conservent une copie figée.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = MenuItemFilter
    search_fields = ["name", "description"]
    ordering_fields = [
        "sort_order",
        "name",
        "price_minor",
        "rating_average",
        "preparation_minutes",
        "calories",
    ]
    ordering = ["sort_order", "name"]

    def get_serializer_class(self) -> type[BaseSerializer[MenuItem]]:
        return MenuItemDetailSerializer if self.action == "retrieve" else MenuItemSerializer

    def get_queryset(self) -> QuerySet[MenuItem]:
        queryset = (
            MenuItem.objects.alive()
            .filter(restaurant__is_active=True)
            .select_related("category", "restaurant")
        )

        if self.action == "retrieve":
            # Trois niveaux — article, groupes, options — chargés en deux
            # requêtes au lieu d'une par groupe. Les options indisponibles
            # restent visibles mais marquées : les masquer ferait croire à un
            # menu qui change de forme d'une minute à l'autre.
            queryset = queryset.prefetch_related(
                Prefetch(
                    "option_groups",
                    queryset=OptionGroup.objects.order_by("sort_order", "name").prefetch_related(
                        Prefetch("options", queryset=Option.objects.order_by("sort_order", "name"))
                    ),
                )
            )

        return queryset


class ReviewViewSet(ListModelMixin, CreateModelMixin, GenericViewSet[Review]):
    """Avis sur les articles.

    Ni modification ni suppression pour l'instant : ce sont des gestes de
    modération, qui appellent une trace d'audit et une permission dédiée. Les
    ouvrir sans cela laisserait un client réécrire un avis après coup sans
    qu'aucun historique ne le montre.
    """

    queryset = (
        Review.objects.select_related("user", "menu_item")
        .filter(menu_item__deleted_at__isnull=True)
        .order_by("-created_at")
    )
    filterset_fields = {"menu_item": ["exact"], "rating": ["exact", "gte"]}

    def get_permissions(self) -> list[BasePermission]:
        # Lecture publique, écriture réservée aux clients : ni le personnel ni
        # un livreur ne notent un plat au nom de la clientèle.
        return [AllowAny()] if self.request.method in ("GET", "HEAD", "OPTIONS") else [IsCustomer()]

    def get_serializer_class(self) -> type[BaseSerializer[Review]]:
        return ReviewWriteSerializer if self.action == "create" else ReviewSerializer

    def get_throttles(self) -> list[BaseThrottle]:
        """Les avis se lisent librement, s'écrivent avec parcimonie.

        Un avis par article et par utilisateur est déjà la règle (S5) : le
        quota ne gêne personne et arrête le remplissage automatisé.
        """
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return super().get_throttles()
        return [ReviewWriteThrottle()]

    @extend_schema(
        request=ReviewWriteSerializer, responses={201: ReviewSerializer}, tags=["catalog"]
    )
    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = ReviewWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        review = ReviewService.submit(user=authenticated_user(request), **serializer.validated_data)

        # La réponse est rendue par le sérialiseur de lecture : le client reçoit
        # l'avis tel qu'il apparaîtra dans la liste, `is_verified_purchase`
        # compris — ce qu'il n'a pas envoyé et ne peut pas deviner.
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)
