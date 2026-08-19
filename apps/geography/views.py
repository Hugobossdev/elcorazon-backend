"""Points d'entrée de la géographie.

Ouverts sans authentification : l'application affiche le pays et la ville
**avant** l'écran d'inscription, et exiger un jeton ici forcerait à créer un
compte pour savoir si le service est disponible chez soi.

Aucun service métier — ADR-003 : ces routes vont de la vue à l'ORM. Y
intercaler une couche qui appellerait `.filter()` serait du coût de maintenance
déguisé en rigueur.
"""

from __future__ import annotations

from django.contrib.gis.db.models.functions import Area
from django.contrib.gis.geos import Point
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.geography.models import City, Country, DeliveryZone
from apps.geography.serializers import (
    CitySerializer,
    CountrySerializer,
    DeliveryZoneSerializer,
    ZoneResolutionQuerySerializer,
    ZoneResolutionSerializer,
)
from common.throttling import ResilientAnonRateThrottle, ResilientUserRateThrottle

__all__ = ["CityViewSet", "CountryViewSet", "ZoneResolutionView"]


class CountryViewSet(ReadOnlyModelViewSet[Country]):
    """Pays d'opération, actifs seulement.

    Un pays désactivé disparaît de l'API sans être supprimé : sa devise et son
    fuseau restent nécessaires à la lecture des commandes déjà passées là-bas.
    """

    serializer_class = CountrySerializer
    permission_classes = [AllowAny]
    queryset = Country.objects.filter(is_active=True)
    lookup_field = "iso_code"
    filterset_fields = ["currency"]


class CityViewSet(ReadOnlyModelViewSet[City]):
    """Villes desservies.

    Le filtre passe par le code ISO du pays et non par sa clé primaire : c'est
    ce que le client a en main (`TG`), et cela lui évite de retenir un UUID.
    """

    serializer_class = CitySerializer
    permission_classes = [AllowAny]
    queryset = (
        City.objects.filter(is_active=True, country__is_active=True)
        .select_related("country")
        .order_by("name")
    )
    lookup_field = "slug"
    filterset_fields = {"country__iso_code": ["exact"]}


class ZoneResolutionView(APIView):
    """`GET /geography/zones/resolve/?lat=…&lon=…` — cette adresse est-elle desservie ?

    C'est la question que pose l'application avant d'afficher un panier : elle
    conditionne les frais annoncés, le montant minimum et le délai estimé.

    **Un point hors couverture n'est pas une erreur** et ne renvoie donc pas de
    404. C'est une réponse légitime à une question légitime ; la traiter en
    erreur obligerait chaque client à ranger le cas nominal « je viens
    d'emménager hors zone » dans sa branche d'exception.

    Le tri par surface croissante départage les zones qui se chevauchent : une
    zone « Centre-ville » incluse dans une zone « Grand Lomé » doit l'emporter,
    car c'est la plus spécifique qui porte le bon barème.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]

    @extend_schema(
        parameters=[ZoneResolutionQuerySerializer],
        responses={200: ZoneResolutionSerializer},
        tags=["geography"],
    )
    def get(self, request: Request) -> Response:
        query = ZoneResolutionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        point = Point(query.validated_data["lon"], query.validated_data["lat"], srid=4326)
        zone = (
            DeliveryZone.objects.filter(
                boundary__covers=point,
                is_active=True,
                city__is_active=True,
                city__country__is_active=True,
            )
            .select_related("city__country")
            .annotate(surface=Area("boundary"))
            .order_by("surface")
            .first()
        )

        return Response(
            {
                "is_covered": zone is not None,
                "zone": DeliveryZoneSerializer(zone).data if zone else None,
            }
        )
