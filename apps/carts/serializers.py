"""Contrats du panier — invariant C1.

Aucun montant n'est accepté en entrée : le client dit *ce qu'il veut*, le
serveur dit *ce que ça coûte*. Les sérialiseurs de sortie n'ont pas de modèle
correspondant en base — ils décrivent un calcul, pas une table.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.catalog.models import MenuItem, Option
from common.serializers import MoneyField

__all__ = [
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
    unit_price = MoneyField(read_only=True)
    total = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    unavailable_reason = serializers.CharField(read_only=True)


class CartSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="cart.id", read_only=True)
    restaurant = serializers.CharField(source="cart.restaurant.slug", read_only=True)
    restaurant_name = serializers.CharField(source="cart.restaurant.name", read_only=True)
    currency = serializers.CharField(read_only=True)
    lines = PricedLineSerializer(many=True, read_only=True)
    subtotal = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
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
    notes = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default="", trim_whitespace=True
    )


class QuantitySerializer(serializers.Serializer[Any]):
    quantity = serializers.IntegerField(min_value=1, max_value=99)
