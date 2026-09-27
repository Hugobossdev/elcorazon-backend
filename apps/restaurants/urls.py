"""Routes des établissements — montées sous `/api/v1/restaurants/`."""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.restaurants import backoffice, views

app_name = "restaurants"

router = DefaultRouter()
# **L'ordre compte, deux fois.** Une fiche d'établissement se lit par son slug
# — `/restaurants/{slug}/` en lecture publique, `/restaurants/manage/{slug}/`
# au back-office — et un motif de slug capte n'importe quel segment. Enregistré
# en premier, il ferait résoudre `/restaurants/staff/` comme « l'établissement
# dont le slug est *staff* », et `/restaurants/manage/hours/` comme
# « l'établissement à administrer dont le slug est *hours* ». Les ressources
# nommées passent donc avant celle qui les capterait.
router.register("staff", backoffice.StaffViewSet, basename="staff")
router.register("audit", backoffice.AuditEntryViewSet, basename="audit")
router.register(
    "manage/hours", backoffice.ManagedOpeningHoursViewSet, basename="managed-opening-hours"
)
router.register(
    "manage/closures", backoffice.ManagedKitchenClosureViewSet, basename="managed-closure"
)
router.register(
    "manage/zones", backoffice.ManagedRestaurantZoneViewSet, basename="managed-restaurant-zone"
)
router.register("manage", backoffice.ManagedRestaurantViewSet, basename="managed-restaurant")
router.register("", views.RestaurantViewSet, basename="restaurant")

urlpatterns = [
    # Déclarée **avant** le routeur : un motif de slug capterait
    # `delivery-check` et le traiterait comme l'identifiant d'un établissement,
    # exactement comme il capterait `staff` ou `manage`.
    path("delivery-check/", views.DeliveryCheckView.as_view(), name="delivery-check"),
    *router.urls,
]
