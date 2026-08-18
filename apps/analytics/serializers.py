"""Contrats de l'analytics.

`EventWriteSerializer` est la seule entrée : n'importe quel type d'événement,
n'importe quelle charge JSON — c'est le client qui sait ce qu'il observe, le
serveur ne fait que l'horodater et l'attribuer. Les rapports, eux, n'ont pas
de sérialiseur d'entrée : leurs paramètres sont des dates, validées par
`ReportQuerySerializer`.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

__all__ = [
    "CategoryRowSerializer",
    "CourierPerformanceRowSerializer",
    "EventWriteSerializer",
    "OverviewSerializer",
    "ReportQuerySerializer",
    "RevenueRowSerializer",
    "StatusRowSerializer",
    "TopProductRowSerializer",
]


class EventWriteSerializer(serializers.Serializer[Any]):
    """`data` serait le nom naturel, mais il masquerait la propriété `.data`
    que DRF pose déjà sur tout `Serializer` — d'où `event_data`, aligné sur le
    nom de la colonne du modèle."""

    event_type = serializers.CharField(max_length=64)
    event_data = serializers.JSONField(required=False, default=dict)
    session_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class ReportQuerySerializer(serializers.Serializer[Any]):
    start = serializers.DateField()
    end = serializers.DateField()
    limit = serializers.IntegerField(min_value=1, max_value=100, required=False, default=10)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError("`end` doit être postérieure ou égale à `start`.")
        return attrs


class RevenueRowSerializer(serializers.Serializer[Any]):
    day = serializers.DateField(read_only=True)
    orders_count = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class TopProductRowSerializer(serializers.Serializer[Any]):
    menu_item_id = serializers.CharField(read_only=True)
    item_name = serializers.CharField(read_only=True)
    quantity_sold = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class CourierPerformanceRowSerializer(serializers.Serializer[Any]):
    courier_id = serializers.CharField(read_only=True)
    courier_name = serializers.CharField(read_only=True)
    deliveries = serializers.IntegerField(read_only=True)
    earnings_minor = serializers.IntegerField(read_only=True)


class StatusRowSerializer(serializers.Serializer[Any]):
    status = serializers.CharField(read_only=True)
    orders_count = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class CategoryRowSerializer(serializers.Serializer[Any]):
    category_id = serializers.CharField(read_only=True)
    category_name = serializers.CharField(read_only=True)
    quantity_sold = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class OverviewSerializer(serializers.Serializer[Any]):
    """Chiffres de tête du tableau de bord.

    Les montants sortent en unité mineure, comme les autres rapports, et non en
    objet `Money` : une ligne de rapport est un nombre à tracer sur un
    graphique, pas une somme à facturer. La devise est celle du marché et
    n'appartient pas à la ligne.
    """

    orders_count = serializers.IntegerField(read_only=True)
    orders_delivered = serializers.IntegerField(read_only=True)
    orders_cancelled = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)
    average_basket_minor = serializers.IntegerField(read_only=True)
    customers_count = serializers.IntegerField(read_only=True)
    couriers_online = serializers.IntegerField(read_only=True)
    menu_items_available = serializers.IntegerField(read_only=True)
    menu_items_total = serializers.IntegerField(read_only=True)
