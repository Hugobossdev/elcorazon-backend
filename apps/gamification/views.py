"""Points d'entrée de la gamification.

Trois catalogues, tous en lecture seule : rien ne se débloque depuis l'API,
le déblocage est un effet de bord de la livraison d'une commande (voir
`apps.gamification.receivers`). Le client ne fait que consulter où il en est.

Chaque vue charge la progression du client courant en un aller — un
dictionnaire `{id_du_catalogue: ligne_de_progression}` — plutôt qu'une requête
par élément du catalogue affiché.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.gamification.models import (
    Achievement,
    Badge,
    Challenge,
    UserAchievement,
    UserBadge,
    UserChallenge,
)
from apps.gamification.serializers import (
    AchievementSerializer,
    BadgeSerializer,
    ChallengeSerializer,
)
from common.permissions import IsCustomer, authenticated_user

__all__ = ["AchievementViewSet", "BadgeViewSet", "ChallengeViewSet"]


class AchievementViewSet(ReadOnlyModelViewSet[Achievement]):
    serializer_class = AchievementSerializer
    queryset = Achievement.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[Achievement]:
        return Achievement.objects.filter(is_active=True)

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        user = authenticated_user(self.request)
        context["progress"] = {
            entry.achievement_id: entry for entry in UserAchievement.objects.filter(user=user)
        }
        return context


class BadgeViewSet(ReadOnlyModelViewSet[Badge]):
    serializer_class = BadgeSerializer
    queryset = Badge.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[Badge]:
        return Badge.objects.filter(is_active=True)

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        user = authenticated_user(self.request)
        context["progress"] = {
            entry.badge_id: entry for entry in UserBadge.objects.filter(user=user)
        }
        return context


class ChallengeViewSet(ReadOnlyModelViewSet[Challenge]):
    """Uniquement les défis **en cours** — un défi passé ou à venir n'est rien
    à consulter pour le client tant qu'il ne peut pas y participer."""

    serializer_class = ChallengeSerializer
    queryset = Challenge.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[Challenge]:
        now = timezone.now()
        return Challenge.objects.filter(is_active=True, starts_at__lte=now, ends_at__gte=now)

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        user = authenticated_user(self.request)
        context["progress"] = {
            entry.challenge_id: entry for entry in UserChallenge.objects.filter(user=user)
        }
        return context
