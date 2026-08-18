"""Administration de la hiérarchie géographique — ADR-006.

Pays, villes et zones sont des objets **d'enseigne** : aucun n'appartient à un
établissement. Le cloisonnement du personnel ne peut donc rien en dire, et le
défaut sûr est le refus — l'écriture est réservée aux comptes non cloisonnés
(`assert_unscoped`), la lecture ouverte à `restaurants.read`.

C'est ici que vit le barème de frais de livraison. L'implémentation précédente
n'en avait pas : une constante, contradictoire d'un fichier à l'autre (`5.00`
contre `500.0`), ce qui trahissait l'absence de toute règle. Un frais se décide
désormais par zone, en donnée, depuis cet écran.

La suppression n'est exposée nulle part. Un pays, une ville et une zone sont
référencés par des commandes, des adresses et des établissements — les clés
étrangères sont d'ailleurs en `PROTECT`, si bien qu'un `DELETE` échouerait en
violation d'intégrité plutôt que par une règle lisible. `is_active` est le
geste qui correspond à l'intention : on ferme un marché, on ne l'efface pas.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.viewsets import GenericViewSet

from apps.geography.models import City, Country, DeliveryZone
from apps.geography.serializers import (
    ManagedCitySerializer,
    ManagedCountrySerializer,
    ManagedDeliveryZoneSerializer,
)
from common.permissions import HasReadWritePermission, assert_unscoped, authenticated_user

__all__ = ["ManagedCityViewSet", "ManagedCountryViewSet", "ManagedDeliveryZoneViewSet"]

GEOGRAPHY_PERMISSION = HasReadWritePermission.of(read="restaurants.read", write="restaurants.write")


class _SiegeViewSet[Model: (Country, City, DeliveryZone)](
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Model],
):
    """Ressource d'enseigne : lisible par le personnel, écrite par le siège."""

    permission_classes = (GEOGRAPHY_PERMISSION,)

    #: Ce qu'on refuse d'écrire, nommé pour le message d'erreur.
    quoi: str = ""

    def perform_create(self, serializer: Any) -> None:
        assert_unscoped(authenticated_user(self.request), self.quoi)
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        assert_unscoped(authenticated_user(self.request), self.quoi)
        serializer.save()


class ManagedCountryViewSet(_SiegeViewSet[Country]):
    quoi = "L'ouverture d'un pays"
    serializer_class = ManagedCountrySerializer
    queryset = Country.objects.order_by("name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {"is_active": ["exact"]}
    search_fields: ClassVar[list[str]] = ["name", "iso_code"]


class ManagedCityViewSet(_SiegeViewSet[City]):
    quoi = "L'ouverture d'une ville"
    serializer_class = ManagedCitySerializer
    queryset = City.objects.select_related("country").order_by("name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "country__iso_code": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name"]


class ManagedDeliveryZoneViewSet(_SiegeViewSet[DeliveryZone]):
    quoi = "Le barème d'une zone"
    serializer_class = ManagedDeliveryZoneSerializer
    queryset = DeliveryZone.objects.select_related("city__country").order_by("city__name", "name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "city": ["exact"],
        "city__slug": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name"]
