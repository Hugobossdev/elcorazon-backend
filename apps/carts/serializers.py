"""Contrats du panier — invariant C1.

Aucun montant n'est accepté en entrée : le client dit *ce qu'il veut*, le
serveur dit *ce que ça coûte*. Les sérialiseurs de sortie n'ont pas de modèle
correspondant en base — ils décrivent un calcul, pas une table.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.catalog.models import MenuItem, Option, Variant
from common.serializers import MoneyField

__all__ = [
    "CartLineUpdateSerializer",
    "CartLineWriteSerializer",
    "CartSerializer",
    "PricedLineSerializer",
    "QuantitySerializer",
]


class SelectedOptionSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    price_delta = MoneyField(read_only=True)
    group = serializers.CharField(source="group.name", read_only=True)


class PricedLineSerializer(serializers.Serializer[Any]):
    """Ligne valorisée.

    `unavailable_reason` accompagne toujours `is_orderable` à faux : « plus au
    menu » et « momentanément indisponible » n'appellent pas le même geste du
    client, et un refus muet finit en appel au support.
    """

    id = serializers.UUIDField(source="line.id", read_only=True)
    menu_item = serializers.UUIDField(source="line.menu_item_id", read_only=True)
    name = serializers.CharField(source="line.menu_item.name", read_only=True)
    image = serializers.ImageField(source="line.menu_item.image", read_only=True)
    quantity = serializers.IntegerField(source="line.quantity", read_only=True)
    notes = serializers.CharField(source="line.notes", read_only=True)
    options = SelectedOptionSerializer(many=True, read_only=True)
    # La taille retenue — nulle pour un article qui n'en a pas.
    variant = serializers.UUIDField(source="line.variant_id", read_only=True, allow_null=True)
    variant_name = serializers.SerializerMethodField()
    unit_price = MoneyField(read_only=True)
    total = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    # Motif stable (`common.availability.UnavailabilityCode`), vide si la ligne
    # est commandable. C'est lui que le client compare, jamais la phrase.
    unavailable_code = serializers.CharField(read_only=True)
    unavailable_reason = serializers.CharField(read_only=True)

    def get_variant_name(self, obj: Any) -> str:
        variant = obj.line.variant
        return variant.name if variant is not None else ""


class CartSerializer(serializers.Serializer[Any]):
    """Panier valorisé.

    `unavailable_code` et `unavailable_reason` disent pourquoi **la cuisine**
    ne prend pas la commande — fermée, suspendue. Les motifs propres à chaque
    article sont sur ses lignes. `is_orderable` compose les deux, pour que le
    bouton de validation n'ait qu'une question à poser.
    """

    id = serializers.UUIDField(source="cart.id", read_only=True)
    restaurant = serializers.CharField(source="cart.restaurant.slug", read_only=True)
    restaurant_name = serializers.CharField(source="cart.restaurant.name", read_only=True)
    currency = serializers.CharField(read_only=True)
    lines = PricedLineSerializer(many=True, read_only=True)
    subtotal = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    unavailable_code = serializers.CharField(read_only=True)
    unavailable_reason = serializers.CharField(read_only=True)
    updated_at = serializers.DateTimeField(source="cart.updated_at", read_only=True)


class CartLineWriteSerializer(serializers.Serializer[Any]):
    """Ajout d'une ligne.

    Ni prix ni libellé : ils sont relus du catalogue. `options` est une liste
    d'identifiants, dont l'appartenance à l'article et le respect des bornes de
    groupe sont vérifiés par le service — la forme ici, la règle là-bas.
    """

    menu_item = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.alive())
    quantity = serializers.IntegerField(min_value=1, max_value=99, default=1)
    options = serializers.PrimaryKeyRelatedField(
        queryset=Option.objects.all(), many=True, required=False, default=list
    )
    # La taille, exigée par le service si l'article en a (lot 2). Son
    # appartenance à l'article se vérifie là-bas, avec le reste de la règle.
    variant = serializers.PrimaryKeyRelatedField(
        queryset=Variant.objects.all(), required=False, allow_null=True, default=None
    )
    notes = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default="", trim_whitespace=True
    )


class QuantitySerializer(serializers.Serializer[Any]):
    quantity = serializers.IntegerField(min_value=1, max_value=99)


class CartLineUpdateSerializer(serializers.Serializer[Any]):
    """Modification d'une ligne déjà au panier.

    Les trois champs sont facultatifs **et distincts d'un champ vide** : ne pas
    envoyer `options` laisse la personnalisation en place — c'est le cas du
    bouton « + » du panier, qui ne connaît que la quantité — quand envoyer une
    liste vide retire tous les choix. Les confondre aurait rendu impossible
    l'un des deux gestes, et `required=False` seul ne les distingue pas : c'est
    `validated_data` qui porte la présence de la clé.

    Ni prix ni libellé ici non plus : la ligne modifiée est revalorisée au
    catalogue comme les autres (C1).
    """

    quantity = serializers.IntegerField(min_value=1, max_value=99, required=False)
    options = serializers.PrimaryKeyRelatedField(
        queryset=Option.objects.all(), many=True, required=False
    )
    variant = serializers.PrimaryKeyRelatedField(
        queryset=Variant.objects.all(), required=False, allow_null=True
    )
    notes = serializers.CharField(
        max_length=500, required=False, allow_blank=True, trim_whitespace=True
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError(
                "Indiquez au moins la quantité, les options ou la note à modifier."
            )
        return attrs
