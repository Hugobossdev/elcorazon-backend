"""Routes du catalogue — montées sous `/api/v1/catalog/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.catalog import backoffice, views

app_name = "catalog"

router = DefaultRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("items", views.MenuItemViewSet, basename="item")
router.register("reviews", views.ReviewViewSet, basename="review")

# Le préfixe `manage/` sépare la carte que lit un client de celle qu'écrit
# l'exploitation. Les deux vivent dans la même app — c'est le même domaine —
# mais aucune route ne fait les deux : un chemin, un public, une permission.
router.register("manage/categories", backoffice.ManagedCategoryViewSet, basename="managed-category")
router.register("manage/items", backoffice.ManagedMenuItemViewSet, basename="managed-item")
router.register(
    "manage/option-groups", backoffice.ManagedOptionGroupViewSet, basename="managed-option-group"
)
router.register("manage/options", backoffice.ManagedOptionViewSet, basename="managed-option")
router.register(
    "manage/option-templates",
    backoffice.ManagedOptionTemplateViewSet,
    basename="managed-option-template",
)

urlpatterns = router.urls
