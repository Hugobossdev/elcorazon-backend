"""Routes de l'analytics — montées sous `/api/v1/analytics/`."""

from __future__ import annotations

from django.urls import path

from apps.analytics import views

app_name = "analytics"

urlpatterns = [
    path("events/", views.EventIngestView.as_view(), name="event"),
    path("reports/revenue/", views.RevenueReportView.as_view(), name="report-revenue"),
    path(
        "reports/top-products/",
        views.TopProductsReportView.as_view(),
        name="report-top-products",
    ),
    path("reports/couriers/", views.CourierPerformanceReportView.as_view(), name="report-couriers"),
    path("reports/orders/", views.OrderStatusReportView.as_view(), name="report-orders"),
    path("reports/categories/", views.CategoryReportView.as_view(), name="report-categories"),
    path("reports/overview/", views.OverviewView.as_view(), name="report-overview"),
    path(
        "reports/customers/<uuid:pk>/",
        views.CustomerStatsView.as_view(),
        name="report-customer",
    ),
]
