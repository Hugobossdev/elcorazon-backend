"""Routes de la gamification — montées sous `/api/v1/gamification/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.gamification import backoffice, views

app_name = "gamification"

router = DefaultRouter()
# Enregistrées avant les collections clientes : `manage/achievements` serait
# sinon capté comme l'identifiant d'un succès.
router.register(
    "manage/achievements", backoffice.ManagedAchievementViewSet, basename="managed-achievement"
)
router.register("manage/badges", backoffice.ManagedBadgeViewSet, basename="managed-badge")
router.register(
    "manage/challenges", backoffice.ManagedChallengeViewSet, basename="managed-challenge"
)
router.register("achievements", views.AchievementViewSet, basename="achievement")
router.register("badges", views.BadgeViewSet, basename="badge")
router.register("challenges", views.ChallengeViewSet, basename="challenge")

urlpatterns = router.urls
