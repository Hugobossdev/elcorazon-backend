"""Gamification : succès, badges, défis — invariant G1.

Trois catalogues, un seul mécanisme de déblocage. Un succès se débloque au
franchissement d'un seuil calculé sur les commandes livrées ; un badge, sur les
points gagnés à vie ; un défi, sur ce même calcul borné à une fenêtre de temps.
Dans les trois cas, le progrès est **recalculé depuis les commandes**, jamais
accumulé à la main : un compteur maintenu à côté dérive tôt ou tard de la
réalité qu'il prétend résumer, comme le montre `PointsAccount.balance` — sauf
que là, rien ne le rend dérivable pour le constater.

**G1 — le déblocage crédite sa récompense une seule fois**, même si l'événement
qui l'a déclenché (livraison d'une commande) est rejoué. La correction n'est
pas une vérification supplémentaire — elle subirait la même course qu'a subie
F1 sur la fidélité — mais un `UPDATE ... WHERE is_unlocked = false` conditionnel
(voir `apps.gamification.services`) : la ligne ne peut passer de faux à vrai
qu'une fois, quel que soit le nombre d'appels concurrents.
"""

from __future__ import annotations

import datetime as dt

from django.db import models

from apps.accounts.models import User
from common.models import TimeStampedModel, UUIDModel

__all__ = [
    "Achievement",
    "AchievementCondition",
    "Badge",
    "Challenge",
    "ChallengeKind",
    "UserAchievement",
    "UserBadge",
    "UserChallenge",
]


class AchievementCondition(models.TextChoices):
    ORDERS_COUNT = "orders_count", "Nombre de commandes livrées"
    TOTAL_SPENT_MINOR = "total_spent_minor", "Total dépensé (unité mineure)"


class Achievement(UUIDModel, TimeStampedModel):
    """Succès à débloquer — condition unique, évaluée sur tout l'historique."""

    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=32, default="🏆")

    condition_type = models.CharField(max_length=32, choices=AchievementCondition.choices)
    condition_value = models.PositiveIntegerField(
        help_text="Seuil à atteindre, dans l'unité de `condition_type`."
    )
    points_reward = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "succès"
        verbose_name_plural = "succès"
        ordering = ["condition_value"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(condition_value__gt=0), name="achievement_condition_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class UserAchievement(UUIDModel):
    """Progression d'un client vers un succès (G1)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="achievements")
    achievement = models.ForeignKey(Achievement, on_delete=models.CASCADE, related_name="unlocks")
    progress = models.PositiveIntegerField(default=0)
    is_unlocked = models.BooleanField(default=False)
    unlocked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "progression de succès"
        verbose_name_plural = "progressions de succès"
        constraints = [
            models.UniqueConstraint(fields=["user", "achievement"], name="one_progress_per_user")
        ]
        indexes = [models.Index(fields=["user", "is_unlocked"])]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.achievement.name} ({self.progress})"


class Badge(UUIDModel, TimeStampedModel):
    """Badge de fidélité — seuil sur les points gagnés à vie.

    Adossé à `PointsAccount.lifetime_earned` et non au solde courant : un badge
    récompense ce qui a été **gagné**, et ne doit pas se retirer parce que le
    client a dépensé ses points entre-temps.
    """

    title = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=32, default="🏅")
    points_required = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "badge"
        ordering = ["points_required"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(points_required__gt=0), name="badge_threshold_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.title


class UserBadge(UUIDModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="badges")
    badge = models.ForeignKey(Badge, on_delete=models.CASCADE, related_name="unlocks")
    is_unlocked = models.BooleanField(default=False)
    unlocked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "badge obtenu"
        verbose_name_plural = "badges obtenus"
        constraints = [
            models.UniqueConstraint(fields=["user", "badge"], name="one_badge_state_per_user")
        ]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.badge.title}"


class ChallengeKind(models.TextChoices):
    DAILY = "daily", "Quotidien"
    WEEKLY = "weekly", "Hebdomadaire"
    MONTHLY = "monthly", "Mensuel"
    SPECIAL = "special", "Spécial"


class Challenge(UUIDModel, TimeStampedModel):
    """Défi borné dans le temps — même mécanique qu'un succès, sur une fenêtre.

    La fenêtre est portée par le défi et non déduite de `challenge_type` : un
    défi « spécial » n'a pas de durée canonique, et figer daily/weekly/monthly
    sur une horloge serveur empêcherait de faire démarrer une campagne un jour
    autre que minuit.
    """

    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    challenge_type = models.CharField(max_length=16, choices=ChallengeKind.choices)
    condition_type = models.CharField(
        max_length=32,
        choices=AchievementCondition.choices,
        default=AchievementCondition.ORDERS_COUNT,
    )
    target_value = models.PositiveIntegerField()
    reward_points = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "défi"
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(target_value__gt=0), name="challenge_target_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="challenge_window_ordered",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def is_open_at(self, moment: dt.datetime) -> bool:
        return self.is_active and self.starts_at <= moment <= self.ends_at


class UserChallenge(UUIDModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="challenges")
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="participants")
    progress = models.PositiveIntegerField(default=0)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "participation à un défi"
        verbose_name_plural = "participations aux défis"
        constraints = [
            models.UniqueConstraint(fields=["user", "challenge"], name="one_participation_per_user")
        ]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.challenge.title} ({self.progress})"
