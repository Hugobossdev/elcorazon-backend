"""Routes du panier collaboratif — montées sous `/api/v1/group-carts/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.groupcarts import views

app_name = "groupcarts"

router = DefaultRouter()
router.register("", views.GroupCartViewSet, basename="group-cart")

urlpatterns = router.urls
