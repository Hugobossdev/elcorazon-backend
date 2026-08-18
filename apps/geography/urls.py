"""Routes de la géographie — montées sous `/api/v1/geography/`."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.geography import backoffice, views

app_name = "geography"

router = DefaultRouter()
router.register("countries", views.CountryViewSet, basename="country")
router.register("cities", views.CityViewSet, basename="city")

# Le préfixe `manage/` sépare ce qu'un visiteur lit — la liste des villes
# desservies — de ce que le siège écrit : contours, barèmes, ouverture d'un
# marché. Un chemin, un public, une permission.
router.register("manage/countries", backoffice.ManagedCountryViewSet, basename="managed-country")
router.register("manage/cities", backoffice.ManagedCityViewSet, basename="managed-city")
router.register("manage/zones", backoffice.ManagedDeliveryZoneViewSet, basename="managed-zone")

urlpatterns = [
    # Déclarée **avant** le routeur : `zones/resolve/` n'est pas un détail de
    # collection, et un routeur qui la capterait la traiterait comme un
    # identifiant de zone.
    path("zones/resolve/", views.ZoneResolutionView.as_view(), name="zone-resolve"),
    path("", include(router.urls)),
]
