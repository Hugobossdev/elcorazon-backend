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
    """La fenêtre d'un rapport — deux dates **murales**, ou rien.

    ## Pourquoi elles sont devenues facultatives

    Elles étaient obligatoires, et l'application les calculait sur
    `DateTime.now()` : l'horloge du **poste** du back-office. Un siège qui
    consulte à minuit et demi demandait donc les chiffres d'une journée que la
    cuisine n'avait pas commencée, et lisait un tableau de bord vide sans que
    rien ne l'explique. La question « quel jour sommes-nous ? » n'a de réponse
    que là où l'activité a lieu, et c'est le serveur qui connaît ce fuseau.

    Omises, elles valent donc **la journée en cours chez l'établissement**. Le
    rapport republie ce qu'il a retenu (voir `OverviewSerializer`).

    Les deux vont ensemble : n'en donner qu'une est refusé plutôt que complété
    d'office — « du 12 à aujourd'hui » et « d'aujourd'hui au 12 » sont deux
    fenêtres différentes, et deviner laquelle est demandée rendrait des chiffres
    pour une question que personne n'a posée.
    """

    start = serializers.DateField(required=False, default=None)
    end = serializers.DateField(required=False, default=None)
    limit = serializers.IntegerField(min_value=1, max_value=100, required=False, default=10)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        debut, fin = attrs["start"], attrs["end"]
        if (debut is None) != (fin is None):
            raise serializers.ValidationError(
                "`start` et `end` vont ensemble : donnez les deux, ou aucune des deux "
                "pour obtenir la journée en cours de l'établissement."
            )
        if debut is not None and fin is not None and fin < debut:
            raise serializers.ValidationError("`end` doit être postérieure ou égale à `start`.")
        return attrs


class RevenueRowSerializer(serializers.Serializer[Any]):
    day = serializers.DateField(read_only=True)
    currency = serializers.CharField(read_only=True)
    orders_count = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class TopProductRowSerializer(serializers.Serializer[Any]):
    menu_item_id = serializers.CharField(read_only=True)
    item_name = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    quantity_sold = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class CourierPerformanceRowSerializer(serializers.Serializer[Any]):
    courier_id = serializers.CharField(read_only=True)
    courier_name = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    deliveries = serializers.IntegerField(read_only=True)
    earnings_minor = serializers.IntegerField(read_only=True)


class NetworkQuerySerializer(serializers.Serializer[Any]):
    """`?level=` et `?zone=` du rapport réseau — la fenêtre vient de `ReportQuerySerializer`."""

    level = serializers.ChoiceField(
        choices=["country", "city", "zone", "kitchen"], required=False, default="kitchen"
    )
    zone = serializers.UUIDField(required=False, allow_null=True, default=None)


class NetworkRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    city = serializers.CharField(read_only=True)
    country = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    orders_count = serializers.IntegerField(read_only=True)
    in_progress_count = serializers.IntegerField(read_only=True)
    delivered_count = serializers.IntegerField(read_only=True)
    cancelled_count = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class StatusRowSerializer(serializers.Serializer[Any]):
    status = serializers.CharField(read_only=True)
    orders_count = serializers.IntegerField(read_only=True)


class CategoryRowSerializer(serializers.Serializer[Any]):
    category_id = serializers.CharField(read_only=True)
    category_name = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    quantity_sold = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)


class CurrencyRevenueSerializer(serializers.Serializer[Any]):
    currency = serializers.CharField(read_only=True)
    orders_delivered = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)
    average_basket_minor = serializers.IntegerField(read_only=True)


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
    revenue_minor = serializers.IntegerField(read_only=True, allow_null=True)
    average_basket_minor = serializers.IntegerField(read_only=True, allow_null=True)
    currency = serializers.CharField(read_only=True, allow_null=True)
    revenues = CurrencyRevenueSerializer(many=True, read_only=True)
    customers_count = serializers.IntegerField(read_only=True)
    couriers_online = serializers.IntegerField(read_only=True)
    menu_items_available = serializers.IntegerField(read_only=True)
    menu_items_total = serializers.IntegerField(read_only=True)

    # La fenêtre telle qu'elle a été agrégée, et le fuseau qui l'a découpée.
    # L'écran n'a plus à la deviner : il la demandait sur l'horloge de son
    # propre poste, ce qui donnait la mauvaise journée dès que le siège et la
    # cuisine ne sont pas dans le même fuseau.
    start = serializers.DateField(read_only=True)
    end = serializers.DateField(read_only=True)
    timezone_name = serializers.CharField(read_only=True)
    timezone_certain = serializers.BooleanField(read_only=True)
