"""Routes de la fidélité — montées sous `/api/v1/loyalty/`."""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.loyalty import backoffice, views

app_name = "loyalty"

router = DefaultRouter()
# Enregistrée avant la collection cliente, qui capterait `manage` comme un
# identifiant de récompense.
router.register("manage/rewards", backoffice.ManagedRewardViewSet, basename="managed-reward")
router.register("rewards", views.RewardViewSet, basename="reward")
router.register("entries", views.PointsEntryViewSet, basename="entry")
router.register("redemptions", views.RewardRedemptionViewSet, basename="redemption")
router.register("plans", views.SubscriptionPlanViewSet, basename="subscription-plan")
router.register("subscriptions", views.SubscriptionViewSet, basename="subscription")

# Le solde est une ressource **singleton** — celui du porteur du jeton — donc une
# vue et non un routeur : `/account/` sans identifiant. Un `ViewSet` imposerait
# une clé dans l'URL, et une clé dans l'URL invite à en essayer une autre.
urlpatterns = [
    path("account/", views.PointsAccountView.as_view(), name="account"),
    *router.urls,
]
