"""Routes des appels — montées sous `/api/v1/calls/`."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.calls import views

app_name = "calls"

router = DefaultRouter()
router.register("", views.CallViewSet, basename="call")

urlpatterns = [
    # Avant le routeur : son préfixe est vide, et il capterait `orders` comme
    # un identifiant d'appel — même piège que `manage/` côté commandes.
    path("orders/<uuid:order_id>/", views.PlaceCallView.as_view(), name="place"),
    path("", include(router.urls)),
]
