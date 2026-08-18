"""Routes du support — montées sous `/api/v1/support/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.support import views

app_name = "support"

router = DefaultRouter()
router.register("tickets", views.SupportTicketViewSet, basename="ticket")
router.register("complaints", views.ComplaintViewSet, basename="complaint")
router.register("returns", views.ReturnRequestViewSet, basename="return")

urlpatterns = router.urls
