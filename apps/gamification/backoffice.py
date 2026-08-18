"""Administration des succès, badges et défis — ADR-005.

Ces trois catalogues sont de la **donnée d'exploitation**, pas du code : créer
« 10 commandes ce mois-ci, 500 points » ne doit pas demander un déploiement.
C'est la même raison qui met les bornes d'un groupe d'options en base plutôt
qu'en dur (ADR-003).

Trois choses distinguent ces routes de celles que lit un client :

* **elles montrent l'inactif.** La forme cliente masque ce qui n'est pas actif —
  proposer un défi terminé n'aide personne — mais l'écran qui sert à réactiver
  un badge doit pouvoir le voir ;
* **elles ne portent aucune progression.** `progress`, `is_unlocked`,
  `is_completed` sont la lecture d'*un* client sur *un* objet ; ici on édite
  l'objet, pas ce que quelqu'un en a fait ;
* **elles ne débloquent rien.** Aucune route pour attribuer un succès ou créditer
  des points : ils s'obtiennent en commandant, par les signaux du domaine
  (`apps.gamification.services`). Une attribution manuelle depuis un écran
  d'administration reviendrait à frapper monnaie — exactement ce que ferment
  F1 et F2 du côté de la fidélité.

**La suppression n'est pas exposée.** Un succès effacé emporterait par cascade
les `UserAchievement` qui le référencent, c'est-à-dire l'historique de ce que
des clients ont réellement débloqué. `is_active` retire de la circulation sans
réécrire le passé.
"""

from __future__ import annotations

from typing import ClassVar

from django.db.models import QuerySet
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.viewsets import GenericViewSet

from apps.gamification.models import Achievement, Badge, Challenge
from apps.gamification.serializers import (
    ManagedAchievementSerializer,
    ManagedBadgeSerializer,
    ManagedChallengeSerializer,
)
from common.permissions import HasReadWritePermission

__all__ = [
    "ManagedAchievementViewSet",
    "ManagedBadgeViewSet",
    "ManagedChallengeViewSet",
]

#: Consulter le catalogue et le composer ne sont pas le même métier.
GAMIFICATION_PERMISSION = HasReadWritePermission.of(
    read="gamification.read", write="gamification.write"
)


class _ManagedCatalogViewSet[Model: (Achievement, Badge, Challenge)](
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Model],
):
    """Facteur commun : mêmes permissions, même absence de suppression.

    En n-uplet et non en liste : `permission_classes` est déclarée comme
    variable d'instance sur `APIView`, et l'annoter `ClassVar` — ce que
    réclamerait une liste mutable — est refusé par le vérificateur de types.
    """

    permission_classes = (GAMIFICATION_PERMISSION,)


class ManagedAchievementViewSet(_ManagedCatalogViewSet[Achievement]):
    """Succès — « 10 commandes », « 5 restaurants différents »."""

    serializer_class = ManagedAchievementSerializer
    queryset = Achievement.objects.order_by("condition_type", "condition_value")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "condition_type": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "description"]

    def get_queryset(self) -> QuerySet[Achievement]:
        return Achievement.objects.order_by("condition_type", "condition_value")


class ManagedBadgeViewSet(_ManagedCatalogViewSet[Badge]):
    """Badges — paliers de points cumulés."""

    serializer_class = ManagedBadgeSerializer
    queryset = Badge.objects.order_by("points_required")
    filterset_fields: ClassVar[dict[str, list[str]]] = {"is_active": ["exact"]}
    search_fields: ClassVar[list[str]] = ["title", "description"]

    def get_queryset(self) -> QuerySet[Badge]:
        return Badge.objects.order_by("points_required")


class ManagedChallengeViewSet(_ManagedCatalogViewSet[Challenge]):
    """Défis — bornés dans le temps, contrairement aux succès et aux badges."""

    serializer_class = ManagedChallengeSerializer
    queryset = Challenge.objects.order_by("-starts_at")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "challenge_type": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["title", "description"]

    def get_queryset(self) -> QuerySet[Challenge]:
        return Challenge.objects.order_by("-starts_at")
