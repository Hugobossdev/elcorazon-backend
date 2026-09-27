"""Contrats du back-office des recettes — ADR-009.

Une recette vise **un plat ou une option**, jamais les deux, et cette cible ne
change plus après la création : réattribuer une recette d'un burger à une
salade ferait porter à la salade l'histoire de ce que le burger a consommé.
Pour une autre cible, on crée une autre recette.

Les lignes ne s'écrivent pas par ce sérialiseur mais par leur propre route, qui
passe par `ProductionService.set_ingredient` : c'est là que vit la règle qu'aucune
contrainte de table ne peut porter — la dimension de la ligne est celle de
l'ingrédient.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.catalog.models import MenuItem, Option
from apps.inventory.models import Ingredient
from apps.production.models import Recipe, RecipeIngredient
from common.serializers import QuantityField

__all__ = [
    "CoverageSerializer",
    "RecipeLineSerializer",
    "RecipeLineWriteSerializer",
    "RecipeSerializer",
]


class RecipeLineSerializer(serializers.ModelSerializer[RecipeIngredient]):
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    ingredient_slug = serializers.CharField(source="ingredient.slug", read_only=True)
    quantity = QuantityField(read_only=True)

    class Meta:
        model = RecipeIngredient
        fields = ["id", "ingredient", "ingredient_name", "ingredient_slug", "quantity"]
        read_only_fields = fields


class RecipeSerializer(serializers.ModelSerializer[Recipe]):
    menu_item = serializers.PrimaryKeyRelatedField[MenuItem](
        queryset=MenuItem.objects.all(), required=False, allow_null=True
    )
    option = serializers.PrimaryKeyRelatedField[Option](
        queryset=Option.objects.select_related("group__menu_item"),
        required=False,
        allow_null=True,
    )
    target_name = serializers.SerializerMethodField()
    restaurant = serializers.SerializerMethodField()
    lines = RecipeLineSerializer(many=True, read_only=True)

    class Meta:
        model = Recipe
        fields = [
            "id",
            "menu_item",
            "option",
            "target_name",
            "restaurant",
            "notes",
            "lines",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_target_name(self, obj: Recipe) -> str:
        """« Burger Corazón », ou « Suppléments › Fromage » pour une option."""
        if obj.menu_item is not None:
            return obj.menu_item.name
        if obj.option is not None:
            return f"{obj.option.group.name} › {obj.option.name}"
        return ""  # pragma: no cover - `recipe_targets_exactly_one` l'interdit

    def get_restaurant(self, obj: Recipe) -> str:
        """Le slug de la cuisine dont la carte porte la cible."""
        if obj.menu_item is not None:
            return obj.menu_item.restaurant.slug
        if obj.option is not None:
            return obj.option.group.menu_item.restaurant.slug
        return ""  # pragma: no cover - idem

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance is not None:
            for champ in ("menu_item", "option"):
                if champ in attrs and attrs[champ] != getattr(self.instance, champ):
                    raise serializers.ValidationError(
                        {
                            champ: "La cible d'une recette ne change pas : créer une "
                            "recette pour la nouvelle cible."
                        }
                    )
            return attrs

        plat, option = attrs.get("menu_item"), attrs.get("option")
        if (plat is None) == (option is None):
            raise serializers.ValidationError(
                "Une recette vise exactement un plat (`menu_item`) ou une option (`option`)."
            )
        if plat is not None and Recipe.objects.filter(menu_item=plat).exists():
            raise serializers.ValidationError({"menu_item": "Ce plat a déjà sa recette."})
        if option is not None and Recipe.objects.filter(option=option).exists():
            raise serializers.ValidationError({"option": "Cette option a déjà sa recette."})
        return attrs


class RecipeLineWriteSerializer(serializers.Serializer[Any]):
    """Corps de `POST …/recipes/{id}/lines/` — pose ou remplace une ligne."""

    ingredient = serializers.PrimaryKeyRelatedField[Ingredient](queryset=Ingredient.objects.alive())
    quantity = QuantityField()


class _MissingItemSerializer(serializers.ModelSerializer[MenuItem]):
    category = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = MenuItem
        fields = ["id", "name", "slug", "category"]
        read_only_fields = fields


class CoverageSerializer(serializers.Serializer[Any]):
    """Ce qui reste à saisir avant de croire le coût matière d'une cuisine."""

    restaurant = serializers.CharField(read_only=True)
    items_total = serializers.IntegerField(read_only=True)
    items_with_recipe = serializers.IntegerField(read_only=True)
    missing = _MissingItemSerializer(many=True, read_only=True)
