"""Routes de l'inventaire — montées sous `/api/v1/inventory/`.

Rien d'autre que `manage/` : l'inventaire n'a pas de lecteur public. Ce qu'un
client en voit passe par le juge de disponibilité, sur la carte.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.inventory import backoffice

app_name = "inventory"

router = DefaultRouter()
router.register(
    "manage/ingredients", backoffice.ManagedIngredientViewSet, basename="managed-ingredient"
)
router.register("manage/stock", backoffice.ManagedStockItemViewSet, basename="managed-stock-item")
router.register(
    "manage/movements", backoffice.ManagedStockMovementViewSet, basename="managed-stock-movement"
)
router.register(
    "manage/adjustment-requests",
    backoffice.ManagedAdjustmentRequestViewSet,
    basename="managed-adjustment-request",
)

urlpatterns = router.urls
