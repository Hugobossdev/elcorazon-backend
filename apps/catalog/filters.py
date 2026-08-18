"""Filtres de recherche du catalogue.

Ces filtres existent pour que la recherche avancée du client soit **faite par
la base**. L'app Supabase composait la même requête depuis le téléphone, puis
finissait le tri en mémoire sur la page reçue : un article écarté par la
pagination n'apparaissait jamais, quel que soit son intérêt. Filtrer ici rend à
la recherche son sens — elle porte sur le catalogue, pas sur une page de
catalogue.

Le prix est exprimé en **unité mineure** (ADR-007), comme partout ailleurs dans
le contrat : `price_min=1000` vaut 1000 F CFA, jamais 10,00.
"""

from __future__ import annotations

from typing import ClassVar

from django.db.models import QuerySet
from django_filters import rest_framework as filters

from apps.catalog.models import MenuItem

__all__ = ["MenuItemFilter"]


class MenuItemFilter(filters.FilterSet):
    """Critères de la recherche avancée."""

    price_min = filters.NumberFilter(field_name="price_minor", lookup_expr="gte")
    price_max = filters.NumberFilter(field_name="price_minor", lookup_expr="lte")

    calories_min = filters.NumberFilter(field_name="calories", lookup_expr="gte")
    calories_max = filters.NumberFilter(field_name="calories", lookup_expr="lte")

    preparation_max = filters.NumberFilter(field_name="preparation_minutes", lookup_expr="lte")
    rating_min = filters.NumberFilter(field_name="rating_average", lookup_expr="gte")

    #: `vegetarian,halal` → les articles portant **tous** ces régimes.
    #: Le `contains` de PostgreSQL sur un tableau est un « inclut tout », et
    #: c'est le sens attendu : cumuler deux régimes restreint le résultat.
    dietary_tags = filters.CharFilter(method="filter_dietary_tags")

    #: `arachide,gluten` → les articles n'en contenant **aucun**. Un filtre
    #: d'allergène qui se contenterait d'écarter ceux qui les contiennent tous
    #: laisserait passer un plat aux arachides dès qu'il est sans gluten.
    exclude_allergens = filters.CharFilter(method="filter_exclude_allergens")

    #: `tomate,basilic` → les articles contenant tous ces ingrédients.
    ingredients = filters.CharFilter(method="filter_ingredients")

    class Meta:
        model = MenuItem
        fields: ClassVar[dict[str, list[str]]] = {
            "restaurant__slug": ["exact"],
            "category__slug": ["exact"],
            "is_available": ["exact"],
            "is_popular": ["exact"],
            "vip_exclusive": ["exact"],
        }

    @staticmethod
    def _values(raw: str) -> list[str]:
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    def filter_dietary_tags(
        self, queryset: QuerySet[MenuItem], name: str, value: str
    ) -> QuerySet[MenuItem]:
        tags = self._values(value)
        return queryset.filter(dietary_tags__contains=tags) if tags else queryset

    def filter_exclude_allergens(
        self, queryset: QuerySet[MenuItem], name: str, value: str
    ) -> QuerySet[MenuItem]:
        allergens = self._values(value)
        return queryset.exclude(allergens__overlap=allergens) if allergens else queryset

    def filter_ingredients(
        self, queryset: QuerySet[MenuItem], name: str, value: str
    ) -> QuerySet[MenuItem]:
        ingredients = self._values(value)
        return queryset.filter(ingredients__contains=ingredients) if ingredients else queryset
