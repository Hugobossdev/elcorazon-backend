"""Back-office de la gamification.

Les trois catalogues (succès, badges, défis) sont de la politique commerciale
— ils s'éditent, comme le catalogue de récompenses de la fidélité. Les lignes
de progression, elles, sont **produites par le service** à chaque livraison :
les éditer à la main déclarerait un succès débloqué sans qu'aucune commande ne
l'explique, ce qu'un client qui contesterait son historique ne pourrait pas
vérifier.
"""

from __future__ import annotations

from django.contrib import admin

from apps.gamification.models import (
    Achievement,
    Badge,
    Challenge,
    UserAchievement,
    UserBadge,
    UserChallenge,
)
from common.admin import ReadOnlyAdmin

__all__ = [
    "AchievementAdmin",
    "BadgeAdmin",
    "ChallengeAdmin",
    "UserAchievementAdmin",
    "UserBadgeAdmin",
    "UserChallengeAdmin",
]


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    list_display = ("name", "condition_type", "condition_value", "points_reward", "is_active")
    list_filter = ("condition_type", "is_active")
    search_fields = ("name",)
    list_editable = ("is_active",)


@admin.register(UserAchievement)
class UserAchievementAdmin(ReadOnlyAdmin):
    list_display = ("user", "achievement", "progress", "is_unlocked", "unlocked_at")
    list_filter = ("is_unlocked", "achievement")
    search_fields = ("user__email", "achievement__name")
    list_select_related = ("user", "achievement")


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display = ("title", "points_required", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title",)
    list_editable = ("is_active",)


@admin.register(UserBadge)
class UserBadgeAdmin(ReadOnlyAdmin):
    list_display = ("user", "badge", "is_unlocked", "unlocked_at")
    list_filter = ("is_unlocked", "badge")
    search_fields = ("user__email", "badge__title")
    list_select_related = ("user", "badge")


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "challenge_type",
        "target_value",
        "reward_points",
        "starts_at",
        "ends_at",
        "is_active",
    )
    list_filter = ("challenge_type", "is_active")
    search_fields = ("title",)
    date_hierarchy = "starts_at"


@admin.register(UserChallenge)
class UserChallengeAdmin(ReadOnlyAdmin):
    list_display = ("user", "challenge", "progress", "is_completed", "completed_at")
    list_filter = ("is_completed", "challenge")
    search_fields = ("user__email", "challenge__title")
    list_select_related = ("user", "challenge")
