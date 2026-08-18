"""Champs de sérialisation transverses — ADR-007, ADR-009.

Deux types du domaine n'ont pas de représentation JSON évidente : le montant et
le point géographique. Les exposer au cas par cas dans chaque app produirait
autant de variantes que d'auteurs — c'est ce qui a donné, sur l'implémentation
précédente, des frais de livraison sérialisés `5.00` d'un côté et `500.0` de
l'autre. Ils sont donc définis **une fois**, ici.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point, Polygon
from django.contrib.gis.geos.error import GEOSException
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.money import Money, UnknownCurrency

__all__ = ["BoundaryField", "LocationField", "MoneyField"]


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "amount": {
                "type": "string",
                "description": "Montant en unité mineure. 1250 XOF = 1250 F ; 1250 EUR = 12,50 €.",
                "example": "1250",
            },
            "currency": {"type": "string", "example": "XOF", "minLength": 3, "maxLength": 3},
        },
        "required": ["amount", "currency"],
    }
)
class MoneyField(serializers.Field[Money, Any, dict[str, str], Any]):
    """Montant — `{"amount": "1250", "currency": "XOF"}`.

    La valeur sort en **chaîne** et non en nombre (ADR-007) : `JSON.parse` d'un
    client JavaScript convertit tout nombre en `double`, et l'exactitude qu'on
    a défendue jusqu'ici en base se perdrait au dernier mètre. Une chaîne
    traverse indifféremment tous les clients.

    L'homonymie avec `common.fields.MoneyField` est voulue et suit la
    convention DRF — `serializers.CharField` et `models.CharField` cohabitent
    de la même façon. Le module d'import lève l'ambiguïté.
    """

    default_error_messages: ClassVar[dict[str, Any]] = {
        "not_an_object": 'Un montant s\'écrit {{"amount": "1250", "currency": "XOF"}}.',
        "not_an_integer": "Le montant doit être un entier en unité mineure, sans séparateur.",
        "unknown_currency": "Devise inconnue : {currency}.",
    }

    def to_representation(self, value: Money) -> dict[str, str]:
        return {"amount": str(value.amount_minor), "currency": value.currency}

    def to_internal_value(self, data: Any) -> Money:
        if not isinstance(data, dict) or "amount" not in data or "currency" not in data:
            self.fail("not_an_object")

        try:
            # `int(str)` refuse `"12.50"` — ce qui est le comportement voulu :
            # une unité majeure envoyée là où on attend une unité mineure est
            # une erreur d'intégration, pas une valeur à convertir en silence.
            amount = int(str(data["amount"]))
        except (TypeError, ValueError):
            self.fail("not_an_integer")

        try:
            return Money(amount, str(data["currency"]))
        except UnknownCurrency:
            self.fail("unknown_currency", currency=data["currency"])


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "lat": {"type": "number", "format": "double", "example": 6.1319},
            "lon": {"type": "number", "format": "double", "example": 1.2255},
        },
        "required": ["lat", "lon"],
    }
)
class LocationField(serializers.Field[Point, Any, dict[str, float], Any]):
    """Point géographique — `{"lat": 6.1319, "lon": 1.2255}`.

    Ni GeoJSON ni WKT : les clients Flutter manipulent un couple de doubles, et
    `Order.delivery_location` fige déjà cette forme en base. Une troisième
    écriture des coordonnées obligerait chaque client à convertir.

    L'ordre est le piège classique — PostGIS attend `Point(x=lon, y=lat)`,
    l'inverse de l'ordre de lecture humain. Le nommage explicite le supprime.
    """

    default_error_messages: ClassVar[dict[str, Any]] = {
        "not_an_object": 'Une position s\'écrit {{"lat": 6.13, "lon": 1.22}}.',
        "out_of_range": "Latitude dans [-90, 90] et longitude dans [-180, 180] attendues.",
    }

    def to_representation(self, value: Point) -> dict[str, float]:
        return {"lat": value.y, "lon": value.x}

    def to_internal_value(self, data: Any) -> Point:
        if not isinstance(data, dict) or "lat" not in data or "lon" not in data:
            self.fail("not_an_object")

        try:
            lat, lon = float(data["lat"]), float(data["lon"])
        except (TypeError, ValueError):
            self.fail("not_an_object")

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self.fail("out_of_range")

        return Point(lon, lat, srid=4326)


@extend_schema_field(
    {
        "type": "object",
        "description": (
            "Contour au format GeoJSON — `Polygon` ou `MultiPolygon`. "
            "Un `Polygon` est accepté et converti : une zone d'un seul tenant "
            "est le cas courant."
        ),
        "properties": {
            "type": {"type": "string", "example": "MultiPolygon"},
            "coordinates": {"type": "array", "items": {}},
        },
        "required": ["type", "coordinates"],
    }
)
class BoundaryField(serializers.Field[MultiPolygon, Any, dict[str, Any], Any]):
    """Contour d'une zone de livraison — GeoJSON, à l'écriture comme à la lecture.

    Ici le GeoJSON s'impose, alors que `LocationField` le refuse pour un point.
    La différence n'est pas une inconséquence : un point a une forme évidente et
    universelle — deux nombres nommés — qu'aucun client n'a besoin d'apprendre.
    Un contour, non : il sort d'un outil de dessin cartographique, qui produit
    du GeoJSON, et inventer une forme maison obligerait chaque outil à être
    converti avant l'envoi.

    Ce champ n'est **jamais** exposé au client final : le contour pèse plusieurs
    kilo-octets, aucun écran ne l'affiche, et savoir où passe la frontière n'est
    pas ce que le client demande — il demande si son point est desservi.
    """

    default_error_messages: ClassVar[dict[str, Any]] = {
        "not_geojson": 'Un contour s\'écrit en GeoJSON : {{"type": …, "coordinates": …}}.',
        "wrong_type": "Contour attendu de type Polygon ou MultiPolygon, reçu {kind}.",
        "invalid": "Contour illisible : {reason}",
    }

    def to_representation(self, value: MultiPolygon) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(value.geojson)
        return payload

    def to_internal_value(self, data: Any) -> MultiPolygon:
        if not isinstance(data, dict) or "type" not in data or "coordinates" not in data:
            self.fail("not_geojson")

        try:
            geometry = GEOSGeometry(json.dumps(data), srid=4326)
        except (GEOSException, ValueError, TypeError) as exc:
            self.fail("invalid", reason=str(exc))

        if isinstance(geometry, Polygon):
            # Une zone d'un seul tenant est le cas courant, et exiger d'elle un
            # `MultiPolygon` à un seul élément ferait échouer l'export de tous
            # les outils de dessin sur une subtilité de typage.
            geometry = MultiPolygon(geometry, srid=4326)

        if not isinstance(geometry, MultiPolygon):
            self.fail("wrong_type", kind=geometry.geom_type)

        if not geometry.valid:
            self.fail("invalid", reason=geometry.valid_reason)

        return geometry
