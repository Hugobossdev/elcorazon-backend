"""Points d'entrée des établissements.

Lecture publique : le client parcourt les restaurants avant d'avoir un compte.
L'administration des établissements passera par le back-office et ses
permissions `restaurants.write` — elle n'est pas ouverte ici.
"""

from __future__ import annotations

from typing import Any

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.serializers import BaseSerializer
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.restaurants.models import Restaurant
from apps.restaurants.serializers import (
    NearbyQuerySerializer,
    RestaurantDetailSerializer,
    RestaurantSerializer,
)

__all__ = ["RestaurantViewSet"]


@extend_schema(parameters=[NearbyQuerySerializer], tags=["restaurants"])
class RestaurantViewSet(ReadOnlyModelViewSet[Restaurant]):
    """Établissements actifs, avec tri par proximité facultatif.

    Le tri par distance n'est pas fait en Python : PostGIS trie sur
    `geography`, donc en mètres sur l'ellipsoïde, et l'index GiST le sert. La
    variante Python — charger tout, calculer, trier — donnerait le même
    résultat sur dix restaurants et deviendrait impraticable à mille.

    Il n'existe **pas** de filtre `open_now` : l'ouverture se calcule dans le
    fuseau de chaque pays à partir de plages dont certaines franchissent
    minuit, ce que SQL ne sait pas exprimer sans dénormaliser. Un filtre
    calculé en Python trierait après la pagination et rendrait des pages de
    tailles arbitraires — un défaut bien pire que l'absence du filtre.
    `is_open` est donc rendu sur chaque élément, et le client filtre l'écran
    qu'il affiche.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle, UserRateThrottle]
    lookup_field = "slug"
    filterset_fields = {
        "zone__city__slug": ["exact"],
        "zone__city__country__iso_code": ["exact"],
        "accepts_orders": ["exact"],
    }

    def get_serializer_class(self) -> type[BaseSerializer[Restaurant]]:
        # Les horaires complets ne sont utiles qu'en fiche : les charger pour
        # chaque élément d'une liste multiplierait la réponse par sept.
        return RestaurantDetailSerializer if self.action == "retrieve" else RestaurantSerializer

    def get_queryset(self) -> QuerySet[Restaurant]:
        queryset = (
            Restaurant.objects.filter(is_active=True)
            .select_related("zone__city__country")
            # `is_open` interroge les plages de chaque établissement : sans ce
            # préchargement, une page de vingt restaurants ferait vingt
            # requêtes de plus.
            .prefetch_related("opening_hours")
        )

        query = NearbyQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        origin = self._origin(query.validated_data)

        if origin is None:
            return queryset.order_by("name")
        return queryset.annotate(distance=Distance("location", origin)).order_by("distance")

    @staticmethod
    def _origin(params: dict[str, Any]) -> Point | None:
        if "lat" not in params:
            return None
        return Point(params["lon"], params["lat"], srid=4326)
