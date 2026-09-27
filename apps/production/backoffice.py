"""Administration des recettes — ce qu'un plat sort de la chambre froide.

`recipes.read` pour consulter, `recipes.write` pour modifier : une permission
distincte du catalogue, parce qu'une recette ne change pas ce que voit le
client — elle change le coût matière et ce que la réservation immobilise.

Cloisonnées par cuisine à travers leur cible : le plat, ou le plat de l'option.
Une recette hors périmètre est introuvable ; une création hors périmètre est
refusée.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.production.models import Recipe
from apps.production.serializers import (
    CoverageSerializer,
    RecipeLineWriteSerializer,
    RecipeSerializer,
)
from apps.production.services import ProductionService
from apps.restaurants.models import Restaurant
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.permissions import HasReadWritePermission, authenticated_user

__all__ = ["ManagedRecipeViewSet"]

RECIPES_PERMISSION = HasReadWritePermission.of(read="recipes.read", write="recipes.write")


@extend_schema(tags=["production"])
class ManagedRecipeViewSet(ModelViewSet[Recipe]):
    """Recettes des plats et des options d'une carte."""

    permission_classes = (RECIPES_PERMISSION,)
    serializer_class = RecipeSerializer
    queryset = Recipe.objects.none()
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "menu_item": ["exact"],
        "option": ["exact"],
    }

    def get_queryset(self) -> QuerySet[Recipe]:
        base = Recipe.objects.select_related(
            "menu_item__restaurant", "option__group__menu_item__restaurant"
        ).prefetch_related("lines__ingredient")

        slug = self.request.query_params.get("restaurant")
        if slug:
            base = base.filter(
                Q(menu_item__restaurant__slug=slug)
                | Q(option__group__menu_item__restaurant__slug=slug)
            )

        user = authenticated_user(self.request)
        if is_unscoped(user):
            return base.order_by("created_at")
        cuisines = staff_restaurant_ids(user)
        return base.filter(
            Q(menu_item__restaurant_id__in=cuisines)
            | Q(option__group__menu_item__restaurant_id__in=cuisines)
        ).order_by("created_at")

    def perform_create(self, serializer: Any) -> None:
        donnees = serializer.validated_data
        plat = donnees.get("menu_item")
        cuisine = (
            plat.restaurant_id
            if plat is not None
            else donnees["option"].group.menu_item.restaurant_id
        )
        assert_in_scope(authenticated_user(self.request), cuisine)
        serializer.save()

    @extend_schema(
        request=RecipeLineWriteSerializer,
        responses={200: RecipeSerializer},
        tags=["production"],
    )
    @action(detail=True, methods=["post"], permission_classes=[RECIPES_PERMISSION])
    def lines(self, request: Request, pk: str) -> Response:
        """Pose — ou remplace — la quantité d'un ingrédient dans cette recette."""
        corps = RecipeLineWriteSerializer(data=request.data)
        corps.is_valid(raise_exception=True)
        recette = self.get_object()

        ProductionService.set_ingredient(
            recipe=recette,
            ingredient_id=corps.validated_data["ingredient"].pk,
            quantity=corps.validated_data["quantity"],
        )
        return Response(RecipeSerializer(self._recharger(recette)).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="ingredient_id",
                location=OpenApiParameter.PATH,
                type=uuid.UUID,
                description="Ingrédient à retirer de la recette.",
            )
        ],
        responses={200: RecipeSerializer},
        tags=["production"],
    )
    @action(
        detail=True,
        methods=["delete"],
        url_path=r"lines/(?P<ingredient_id>[^/.]+)",
        permission_classes=[RECIPES_PERMISSION],
    )
    def remove_line(self, request: Request, pk: str, ingredient_id: str) -> Response:
        """Retire un ingrédient. Retirer ce qui n'y est pas n'est pas une erreur."""
        recette = self.get_object()
        try:
            identifiant = uuid.UUID(ingredient_id)
        except ValueError:
            return Response(RecipeSerializer(self._recharger(recette)).data)
        ProductionService.remove_ingredient(recipe=recette, ingredient_id=identifiant)
        return Response(RecipeSerializer(self._recharger(recette)).data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="restaurant",
                type=str,
                required=True,
                description="Slug de la cuisine dont on mesure la carte.",
            )
        ],
        responses={200: CoverageSerializer},
        tags=["production"],
    )
    @action(detail=False, methods=["get"], permission_classes=[RECIPES_PERMISSION])
    def coverage(self, request: Request) -> Response:
        """Les plats de la carte qui n'ont pas encore de recette.

        Tant que la liste n'est pas vide, un plat de la cuisine ne consomme rien
        au stock, et son coût matière est inconnu. C'est ce qu'il faut lire
        avant de croire une marge.

        Une cuisine hors périmètre est **introuvable**, comme pour toute lecture.
        """
        slug = request.query_params.get("restaurant", "")
        user = authenticated_user(request)
        cuisines = Restaurant.objects.all()
        if not is_unscoped(user):
            cuisines = cuisines.filter(pk__in=staff_restaurant_ids(user))
        cuisine = get_object_or_404(cuisines, slug=slug)

        total, sans_recette = ProductionService.coverage(cuisine.pk)
        return Response(
            CoverageSerializer(
                {
                    "restaurant": cuisine.slug,
                    "items_total": total,
                    "items_with_recipe": total - len(sans_recette),
                    "missing": sans_recette,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _recharger(recette: Recipe) -> Recipe:
        return (
            Recipe.objects.select_related(
                "menu_item__restaurant", "option__group__menu_item__restaurant"
            )
            .prefetch_related("lines__ingredient")
            .get(pk=recette.pk)
        )
