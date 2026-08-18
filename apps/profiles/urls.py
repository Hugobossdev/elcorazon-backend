"""Routes du profil client — montées sous `/api/v1/profiles/`."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.profiles import views

app_name = "profiles"

router = DefaultRouter()
router.register("addresses", views.AddressViewSet, basename="address")

urlpatterns = [
    path("preferences/", views.PreferenceView.as_view(), name="preferences"),
    path("", include(router.urls)),
]
