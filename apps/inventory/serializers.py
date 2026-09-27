"""Contrats du back-office de l'inventaire — ADR-009.

## Ce qu'aucun sérialiseur d'entrée ne porte

**Le stock.** `on_hand`, `reserved` et le coût moyen ne s'écrivent jamais par
un champ : ils ne bougent qu'à travers un mouvement, écrit par
`InventoryService`, qui tient le journal et la colonne dans la même transaction.
Un `PATCH {"on_hand": …}` serait exactement l'écriture sans trace que ce module
existe pour interdire — la correction s'appelle un ajustement, et elle porte un
motif.

**Le coût unitaire.** On reçoit le **prix du lot**, tel qu'il figure sur la
facture ; le serveur en déduit le coût par kilogramme. Demander à un magasinier
le prix du gramme d'oignon, c'est lui demander un calcul qu'il fera faux.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import User
from apps.inventory.models import (
    AdjustmentRequest,
    Dimensions,
    Ingredient,
    StockItem,
    StockMovement,
)
from apps.restaurants.models import Restaurant
from common.money import Money
from common.quantities import COST_UNIT, value_minor
from common.serializers import MoneyField, QuantityField

__all__ = [
    "AdjustmentDeclarationSerializer",
    "AdjustmentRequestSerializer",
    "ApprovalSerializer",
    "DeclarationSerializer",
    "IngredientSerializer",
    "ReceiptSerializer",
    "RejectionSerializer",
    "StockItemSerializer",
    "StockMovementSerializer",
    "WasteSerializer",
]


class _ActorSerializer(serializers.ModelSerializer[User]):
    """Qui a écrit ou décidé — le nom, jamais le contact."""

    class Meta:
        model = User
        fields = ["id", "full_name"]
        read_only_fields = fields


# ---------------------------------------------------------------- référentiel


class IngredientSerializer(serializers.ModelSerializer[Ingredient]):
    """Une référence d'achat, au niveau de l'enseigne.

    `dimension` s'écrit à la création et **plus jamais** : la changer
    réinterpréterait d'un coup tous les mouvements passés — « 500 » deviendrait
    500 ml là où il valait 500 mg. Une erreur de dimension se corrige en créant
    la bonne référence et en retirant la mauvaise.
    """

    class Meta:
        model = Ingredient
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "dimension",
            "allergens",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_dimension(self, value: str) -> str:
        if self.instance is not None and value != self.instance.dimension:
            raise serializers.ValidationError(
                "La dimension d'un ingrédient ne change pas après sa création : "
                "elle donne leur sens à tous ses mouvements passés."
            )
        return value

    def validate_allergens(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
            raise serializers.ValidationError("Une liste de codes d'allergènes est attendue.")
        return sorted({code.strip().lower() for code in value if code.strip()})


# ---------------------------------------------------------------------- stock


class StockItemSerializer(serializers.ModelSerializer[StockItem]):
    """Ce qu'une cuisine détient d'un ingrédient.

    À l'écriture, trois champs seulement : la cuisine et l'ingrédient à
    l'ouverture de la ligne, le seuil d'alerte ensuite. Le reste se lit.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.all()
    )
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True)
    ingredient = serializers.PrimaryKeyRelatedField[Ingredient](queryset=Ingredient.objects.alive())
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    ingredient_slug = serializers.CharField(source="ingredient.slug", read_only=True)
    dimension = serializers.ChoiceField(
        source="ingredient.dimension", choices=Dimensions.choices, read_only=True
    )

    on_hand = QuantityField(read_only=True)
    reserved = QuantityField(read_only=True)
    available = QuantityField(read_only=True)
    low_stock_threshold = QuantityField(required=False, allow_null=True)
    is_low = serializers.BooleanField(read_only=True)

    unit_cost = MoneyField(read_only=True, allow_null=True)
    cost_unit = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()

    class Meta:
        model = StockItem
        fields = [
            "id",
            "restaurant",
            "restaurant_name",
            "ingredient",
            "ingredient_name",
            "ingredient_slug",
            "dimension",
            "on_hand",
            "reserved",
            "available",
            "low_stock_threshold",
            "is_low",
            "unit_cost",
            "cost_unit",
            "stock_value",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        # Pas de validateur d'unicité implicite sur (cuisine, ingrédient) : DRF
        # l'ajouterait de lui-même depuis la contrainte, et refuserait en 400 la
        # seconde ouverture — que le service rend **exprès** idempotente, pour
        # qu'un double clic rende la ligne au lieu d'échouer. L'unicité reste
        # tenue par la base.
        validators: ClassVar[list[Any]] = []

    def get_cost_unit(self, obj: StockItem) -> str:
        """L'unité dans laquelle se lit `unit_cost` — `kg`, `l` ou `unit`."""
        return COST_UNIT[obj.ingredient.dimension]

    @extend_schema_field(MoneyField(allow_null=True))
    def get_stock_value(self, obj: StockItem) -> dict[str, str] | None:
        """Ce que vaut le stock détenu, au coût moyen — nul si le coût est inconnu."""
        cout = obj.unit_cost
        if cout is None:
            return None
        return MoneyField().to_representation(
            Money(value_minor(obj.on_hand, cout.amount_minor), cout.currency)
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance is not None:
            # Une ligne ouverte ne change ni de cuisine ni d'ingrédient : son
            # journal les désigne, et les déplacer réattribuerait l'histoire.
            for champ in ("restaurant", "ingredient"):
                if champ in attrs and attrs[champ] != getattr(self.instance, champ):
                    raise serializers.ValidationError(
                        {champ: "Ne se modifie pas sur une ligne de stock ouverte."}
                    )

        ingredient = attrs.get("ingredient") or (
            self.instance.ingredient if self.instance is not None else None
        )
        seuil = attrs.get("low_stock_threshold")
        if ingredient is not None and seuil is not None:
            if seuil.dimension != ingredient.dimension:
                raise serializers.ValidationError(
                    {
                        "low_stock_threshold": (
                            f"« {ingredient.name} » se mesure en "
                            f"{ingredient.get_dimension_display().lower()} : "
                            "le seuil doit l'être aussi."
                        )
                    }
                )
            if seuil.is_negative:
                raise serializers.ValidationError(
                    {"low_stock_threshold": "Un seuil d'alerte ne peut pas être négatif."}
                )
        return attrs


class ReceiptSerializer(serializers.Serializer[Any]):
    """Corps de `POST …/stock/{id}/receive/` — une livraison.

    `total_cost` est le prix **du lot**, facultatif : un don, un transfert
    interne, une livraison dont la facture arrivera plus tard. Sans lui, le coût
    moyen de la ligne ne bouge pas.
    """

    quantity = QuantityField()
    total_cost = MoneyField(required=False, allow_null=True)
    reference = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
        default="",
        help_text="Numéro du bon de livraison ou de la facture.",
    )

    def validate_quantity(self, value: Any) -> Any:
        if not value.is_positive:
            raise serializers.ValidationError(
                "Une livraison fait entrer de la matière : quantité positive attendue."
            )
        return value

    def validate_total_cost(self, value: Money | None) -> Money | None:
        if value is not None and value.amount_minor < 0:
            raise serializers.ValidationError("Un prix d'achat ne peut pas être négatif.")
        return value


class WasteSerializer(serializers.Serializer[Any]):
    """Corps de `POST …/stock/{id}/waste/` — ce qui a été jeté, et pourquoi."""

    quantity = QuantityField()
    reason = serializers.CharField(max_length=500, allow_blank=False)

    def validate_quantity(self, value: Any) -> Any:
        if not value.is_positive:
            raise serializers.ValidationError(
                "Indiquer la quantité perdue, positive : le serveur la sort du stock."
            )
        return value


class AdjustmentDeclarationSerializer(serializers.Serializer[Any]):
    """Corps de `POST …/stock/{id}/adjust/` — un écart constaté.

    **Exactement un** des deux :

    * `counted` — ce que l'inventaire physique a trouvé sur l'étagère. C'est le
      geste réel d'un comptage, et l'écart est calculé par le serveur, sous
      verrou, contre le stock du moment ;
    * `delta` — la correction elle-même, signée. Pour réparer une saisie fausse
      dont on connaît l'écart : « la livraison de mardi faisait 2 kg de moins ».
    """

    counted = QuantityField(required=False)
    delta = QuantityField(required=False)
    reason = serializers.CharField(max_length=500, allow_blank=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if ("counted" in attrs) == ("delta" in attrs):
            raise serializers.ValidationError(
                "Indiquer soit la quantité comptée (`counted`), soit l'écart (`delta`)."
            )
        if "counted" in attrs and attrs["counted"].is_negative:
            raise serializers.ValidationError(
                {"counted": "Une étagère ne contient pas une quantité négative."}
            )
        return attrs


# ------------------------------------------------------------------- journal


class StockMovementSerializer(serializers.ModelSerializer[StockMovement]):
    """Une ligne du journal, en lecture seule — il ne se modifie pas."""

    ingredient_name = serializers.CharField(source="stock_item.ingredient.name", read_only=True)
    restaurant = serializers.CharField(source="stock_item.restaurant.slug", read_only=True)
    quantity = QuantityField(read_only=True)
    unit_cost = MoneyField(read_only=True, allow_null=True)
    # `method_name` explicite : la méthode par défaut, `get_value`, écraserait
    # `Field.get_value`, que DRF appelle pour lire les données d'entrée.
    value = serializers.SerializerMethodField(method_name="valeur_du_mouvement")
    actor = _ActorSerializer(read_only=True, allow_null=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "stock_item",
            "restaurant",
            "ingredient_name",
            "kind",
            "quantity",
            "unit_cost",
            "value",
            "actor",
            "reason",
            "reference",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(MoneyField(allow_null=True))
    def valeur_du_mouvement(self, obj: StockMovement) -> dict[str, str] | None:
        """Valeur du mouvement au coût **figé sur la ligne** — signée comme la quantité."""
        cout = obj.unit_cost
        if cout is None:
            return None
        return MoneyField().to_representation(
            Money(value_minor(obj.quantity, cout.amount_minor), cout.currency)
        )


class AdjustmentRequestSerializer(serializers.ModelSerializer[AdjustmentRequest]):
    """Une perte ou une correction en attente, validée ou refusée."""

    restaurant = serializers.CharField(source="stock_item.restaurant.slug", read_only=True)
    ingredient_name = serializers.CharField(source="stock_item.ingredient.name", read_only=True)
    quantity = QuantityField(read_only=True)
    estimated_value = MoneyField(read_only=True, allow_null=True)
    requested_by = _ActorSerializer(read_only=True, allow_null=True)
    decided_by = _ActorSerializer(read_only=True, allow_null=True)

    class Meta:
        model = AdjustmentRequest
        fields = [
            "id",
            "stock_item",
            "restaurant",
            "ingredient_name",
            "kind",
            "quantity",
            "reason",
            "estimated_value",
            "status",
            "requested_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "movement",
            "created_at",
        ]
        read_only_fields = fields


class DeclarationSerializer(serializers.Serializer[Any]):
    """Ce qu'une déclaration a produit : un mouvement écrit, ou une demande."""

    outcome = serializers.ChoiceField(choices=["applied", "pending_approval"], read_only=True)
    movement = StockMovementSerializer(read_only=True, allow_null=True)
    request = AdjustmentRequestSerializer(read_only=True, allow_null=True)


class ApprovalSerializer(serializers.Serializer[Any]):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class RejectionSerializer(serializers.Serializer[Any]):
    """Le refus dit pourquoi : la personne qui a déclaré doit savoir quoi recompter."""

    note = serializers.CharField(max_length=500, allow_blank=False)
