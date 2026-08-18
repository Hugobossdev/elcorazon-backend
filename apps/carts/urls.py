"""Routes du panier — montées sous `/api/v1/carts/`.

Le panier s'adresse par le slug du restaurant : `/carts/el-corazon-lome/`. Un
identifiant de panier obligerait le client à le retenir, ou à le demander avant
chaque ajout.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.carts import views

app_name = "carts"

router = DefaultRouter()
router.register("", views.CartViewSet, basename="cart")

urlpatterns = router.urls
