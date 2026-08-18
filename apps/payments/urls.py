"""Routes du paiement — montées sous `/api/v1/payments/`."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.payments import views

app_name = "payments"

router = DefaultRouter()
router.register("transactions", views.TransactionViewSet, basename="transaction")

urlpatterns = [
    # Déclarées avant le routeur : `webhook/` et les actions par commande ne
    # sont pas des détails de la collection des transactions.
    path("webhook/<str:provider>/", views.WebhookView.as_view(), name="webhook"),
    path("withdrawals/", views.WithdrawalView.as_view(), name="withdrawals"),
    path("<uuid:order_id>/initiate/", views.InitiatePaymentView.as_view(), name="initiate"),
    path("<uuid:order_id>/split/", views.SplitPaymentView.as_view(), name="split"),
    # Le jeton d'une part, pour un convive sans compte. Pas d'identifiant de
    # commande dans l'URL : le lien ne donne accès qu'à la part.
    path("shares/<str:token>/", views.ShareView.as_view(), name="share"),
    path("<uuid:order_id>/refund/", views.RefundView.as_view(), name="refund"),
    path("", include(router.urls)),
]
