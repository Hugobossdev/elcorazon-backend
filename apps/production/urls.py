"""Routes de la production — montées sous `/api/v1/production/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.production import backoffice

app_name = "production"

router = DefaultRouter()
router.register("manage/recipes", backoffice.ManagedRecipeViewSet, basename="managed-recipe")

urlpatterns = router.urls
