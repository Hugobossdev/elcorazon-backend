"""Routes des notifications — montées sous `/api/v1/notifications/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.notifications import backoffice, views

app_name = "notifications"

router = DefaultRouter()
# Enregistrée avant la collection racine, qui capterait `campaigns` comme un
# identifiant de notification.
router.register("campaigns", backoffice.CampaignViewSet, basename="campaign")
router.register("", views.NotificationViewSet, basename="notification")

urlpatterns = router.urls
