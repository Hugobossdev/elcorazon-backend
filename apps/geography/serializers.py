"""Contrats de la géographie — ADR-006, ADR-009.

Lecture seule côté client : la hiérarchie est administrée par le back-office.
Le contour des zones (`boundary`) n'est **jamais** exposé — c'est un
`MultiPolygon` de plusieurs kilo-octets qu'aucun écran n'affiche, et le client
n'a pas à savoir *où* passe la frontière : il demande si son point est
desservi, la base répond.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.geography.models import City, Country, DeliveryZone
from common.serializers import BoundaryField, LocationField, MoneyField

__all__ = [
    "CitySerializer",
    "CountrySerializer",
    "DeliveryZoneSerializer",
    "ManagedCitySerializer",
    "ManagedCountrySerializer",
    "ManagedDeliveryZoneSerializer",
    "ZoneResolutionQuerySerializer",
    "ZoneResolutionSerializer",
]


class CountrySerializer(serializers.ModelSerializer[Country]):
    class Meta:
        model = Country
        fields = [
            "id",
            "iso_code",
            "name",
            "currency",
            "phone_prefix",
            "timezone",
            "default_language",
        ]
        read_only_fields = fields


class CitySerializer(serializers.ModelSerializer[City]):
    # Le pays est imbriqué plutôt que référencé : sans lui le client ne connaît
    # pas la devise, et il ne peut donc pas formater un seul prix sans un
    # second appel.
    country = CountrySerializer(read_only=True)
    centroid = LocationField(read_only=True)

    class Meta:
        model = City
        fields = ["id", "name", "slug", "country", "centroid"]
        read_only_fields = fields


class DeliveryZoneSerializer(serializers.ModelSerializer[DeliveryZone]):
    city = CitySerializer(read_only=True)
    base_fee = MoneyField(read_only=True)
    fee_per_km = MoneyField(read_only=True)
    free_delivery_threshold = MoneyField(read_only=True)
    min_order_amount = MoneyField(read_only=True)

    class Meta:
        model = DeliveryZone
        fields = [
            "id",
            "name",
            "city",
            "base_fee",
            "fee_per_km",
            "free_delivery_threshold",
            "min_order_amount",
            "max_distance_km",
            "estimated_delivery_minutes",
        ]
        read_only_fields = fields


# --------------------------------------------------------------- back-office


class ManagedCountrySerializer(serializers.ModelSerializer[Country]):
    """Pays vu de l'exploitation.

    `currency` reste modifiable, et c'est un piège qu'il faut connaître : les
    montants déjà écrits portent leur propre devise (ADR-007), donc rien ne se
    convertit rétroactivement. Changer la devise d'un pays en activité
    produirait un catalogue et un historique dans deux unités. La colonne est
    donc laissée ouverte pour l'ouverture d'un marché, pas pour sa correction.
    """

    class Meta:
        model = Country
        fields = [
            "id",
            "iso_code",
            "name",
            "currency",
            "phone_prefix",
            "timezone",
            "default_language",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedCitySerializer(serializers.ModelSerializer[City]):
    country = serializers.SlugRelatedField[Country](
        slug_field="iso_code", queryset=Country.objects.all()
    )
    centroid = LocationField()

    class Meta:
        model = City
        fields = [
            "id",
            "country",
            "name",
            "slug",
            "centroid",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedDeliveryZoneSerializer(serializers.ModelSerializer[DeliveryZone]):
    """Zone et son barème — le seul endroit où se décide un frais de livraison.

    Il remplace la constante contradictoire de l'implémentation précédente
    (`5.00` d'un côté, `500.0` de l'autre), et le barème vit **en donnée** :
    ouvrir un quartier, relever le forfait d'une zone excentrée ou offrir la
    livraison au-dessus d'un seuil se font depuis cet écran, sans déploiement.
    """

    city = serializers.PrimaryKeyRelatedField[City](queryset=City.objects.all())
    boundary = BoundaryField()
    base_fee = MoneyField()
    fee_per_km = MoneyField()
    free_delivery_threshold = MoneyField(required=False, allow_null=True)
    min_order_amount = MoneyField(required=False, allow_null=True)

    class Meta:
        model = DeliveryZone
        fields = [
            "id",
            "city",
            "name",
            "boundary",
            "base_fee",
            "fee_per_km",
            "free_delivery_threshold",
            "min_order_amount",
            "max_distance_km",
            "estimated_delivery_minutes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Le barème est libellé dans la devise du pays.

        La devise n'est pas choisie au niveau de la zone : elle est héritée
        (ADR-006). Un forfait en euros sur une zone d'un pays en francs CFA ne
        se verrait qu'au calcul des frais, c'est-à-dire au passage de commande
        d'un client — en 500, et sur le chemin du chiffre d'affaires.
        """
        instance = self.instance
        city = attrs.get("city") or (instance.city if instance else None)
        if city is None:  # pragma: no cover - `city` est obligatoire à la création
            return attrs

        devise = city.country.currency
        for champ in ("base_fee", "fee_per_km", "free_delivery_threshold", "min_order_amount"):
            montant = attrs.get(champ)
            if montant is not None and montant.currency != devise:
                raise serializers.ValidationError(
                    {champ: f"Ce pays facture en {devise} ; montant reçu en {montant.currency}."}
                )

        return attrs


class ZoneResolutionQuerySerializer(serializers.Serializer[Any]):
    """Paramètres de `GET /geography/zones/resolve/`.

    Déclarés en sérialiseur plutôt que lus à la main : la validation des bornes
    est faite une fois, et `drf-spectacular` documente les paramètres depuis
    cette classe au lieu d'une annotation manuelle qui se périme.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class ZoneResolutionSerializer(serializers.Serializer[Any]):
    """Réponse de la résolution.

    `is_covered` est redondant avec `zone is not null` — délibérément. Le
    booléen est ce que le client teste, et il reste juste si la réponse gagne
    un jour un cas « couvert mais momentanément suspendu ».
    """

    is_covered = serializers.BooleanField(read_only=True)
    zone = DeliveryZoneSerializer(read_only=True, allow_null=True)
