"""Routes des commandes — montées sous `/api/v1/orders/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.orders import backoffice, views

app_name = "orders"

router = DefaultRouter()

# `manage/` d'abord : le routeur essaie les préfixes dans l'ordre, et celui de
# `OrderViewSet` est vide — il capterait donc `manage` comme un identifiant de
# commande et rendrait un 404 au lieu de la liste de supervision.
router.register("manage", backoffice.ManagedOrderViewSet, basename="managed-order")
router.register("", views.OrderViewSet, basename="order")

urlpatterns = router.urls
