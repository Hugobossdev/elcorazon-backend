"""Routes du suivi — montées sous `/api/v1/tracking/`."""

from __future__ import annotations

from django.urls import path

from apps.tracking import views

app_name = "tracking"

urlpatterns = [
    path(
        "assignments/<uuid:assignment_id>/pings/",
        views.PingView.as_view(),
        name="pings",
    ),
    path("orders/<uuid:order_id>/", views.OrderTrackingView.as_view(), name="order"),
]
