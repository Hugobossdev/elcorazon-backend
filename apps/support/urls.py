"""Routes du support — montées sous `/api/v1/support/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.support import backoffice, views

app_name = "support"

router = DefaultRouter()
# Le côté personnel. `manage/` et non les mêmes chemins que le client : la
# réponse ne dépend pas du type de compte, elle dépend de la route.
router.register("manage/tickets", backoffice.ManagedTicketViewSet, basename="managed-ticket")
router.register(
    "manage/complaints", backoffice.ManagedComplaintViewSet, basename="managed-complaint"
)
router.register("manage/returns", backoffice.ManagedReturnViewSet, basename="managed-return")
router.register("tickets", views.SupportTicketViewSet, basename="ticket")
router.register("complaints", views.ComplaintViewSet, basename="complaint")
router.register("returns", views.ReturnRequestViewSet, basename="return")

urlpatterns = router.urls
