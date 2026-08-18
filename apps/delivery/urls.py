"""Routes de la livraison — montées sous `/api/v1/delivery/`."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.delivery import backoffice, views

app_name = "delivery"

router = DefaultRouter()
router.register("assignments", views.AssignmentViewSet, basename="assignment")
router.register("couriers", views.StaffCourierViewSet, basename="courier")
router.register("shifts", backoffice.CourierShiftViewSet, basename="shift")

urlpatterns = [
    # Le dossier du livreur s'adresse par `me/` et non par son identifiant :
    # il n'a qu'un dossier, et le lui faire retenir n'apporte rien.
    path("me/", views.CourierProfileView.as_view(), name="me"),
    path("me/online/", views.CourierOnlineView.as_view(), name="me-online"),
    path(
        "orders/<uuid:order_id>/offer/",
        views.OfferAssignmentView.as_view(),
        name="offer",
    ),
    path(
        "orders/<uuid:order_id>/rating/",
        views.OrderRatingView.as_view(),
        name="order-rating",
    ),
    path(
        "assignments/<uuid:assignment_id>/cancel/",
        views.CancelAssignmentView.as_view(),
        name="assignment-cancel",
    ),
    path("", include(router.urls)),
]
