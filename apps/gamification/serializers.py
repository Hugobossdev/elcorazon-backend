"""Contrats de la gamification.

Tout est en lecture seule : le progrès se calcule côté serveur depuis les
commandes livrées (voir `apps.gamification.services`), rien ne s'y déclare
depuis le client — même logique que la fidélité.

`progress`, `is_unlocked` et `unlocked_at` ne sont pas des colonnes du
catalogue : ils viennent de la ligne de progression **du client courant**,
injectée par la vue dans le contexte du sérialiseur (`context["progress"]`)
pour éviter une requête par ligne de catalogue.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.gamification.models import Achievement, Badge, Challenge

__all__ = [
    "AchievementSerializer",
    "BadgeSerializer",
    "ChallengeSerializer",
    "ManagedAchievementSerializer",
    "ManagedBadgeSerializer",
    "ManagedChallengeSerializer",
]


def _progress_entry(serializer: serializers.BaseSerializer[Any], obj: Any) -> Any:
    """La ligne de progression du client courant, si la vue en a injecté une."""
    return serializer.context.get("progress", {}).get(obj.pk)


class AchievementSerializer(serializers.ModelSerializer[Achievement]):
    progress = serializers.SerializerMethodField()
    is_unlocked = serializers.SerializerMethodField()
    unlocked_at = serializers.SerializerMethodField()

    class Meta:
        model = Achievement
        fields = [
            "id",
            "name",
            "description",
            "icon",
            "condition_type",
            "condition_value",
            "points_reward",
            "progress",
            "is_unlocked",
            "unlocked_at",
        ]
        read_only_fields = fields

    def get_progress(self, obj: Achievement) -> int:
        entry = _progress_entry(self, obj)
        return int(entry.progress) if entry else 0

    def get_is_unlocked(self, obj: Achievement) -> bool:
        entry = _progress_entry(self, obj)
        return bool(entry and entry.is_unlocked)

    def get_unlocked_at(self, obj: Achievement) -> Any:
        entry = _progress_entry(self, obj)
        return entry.unlocked_at if entry else None


class BadgeSerializer(serializers.ModelSerializer[Badge]):
    is_unlocked = serializers.SerializerMethodField()
    unlocked_at = serializers.SerializerMethodField()

    class Meta:
        model = Badge
        fields = [
            "id",
            "title",
            "description",
            "icon",
            "points_required",
            "is_unlocked",
            "unlocked_at",
        ]
        read_only_fields = fields

    def get_is_unlocked(self, obj: Badge) -> bool:
        entry = _progress_entry(self, obj)
        return bool(entry and entry.is_unlocked)

    def get_unlocked_at(self, obj: Badge) -> Any:
        entry = _progress_entry(self, obj)
        return entry.unlocked_at if entry else None


class ChallengeSerializer(serializers.ModelSerializer[Challenge]):
    progress = serializers.SerializerMethodField()
    is_completed = serializers.SerializerMethodField()

    class Meta:
        model = Challenge
        fields = [
            "id",
            "title",
            "description",
            "challenge_type",
            "target_value",
            "reward_points",
            "starts_at",
            "ends_at",
            "progress",
            "is_completed",
        ]
        read_only_fields = fields

    def get_progress(self, obj: Challenge) -> int:
        entry = _progress_entry(self, obj)
        return int(entry.progress) if entry else 0

    def get_is_completed(self, obj: Challenge) -> bool:
        entry = _progress_entry(self, obj)
        return bool(entry and entry.is_completed)


class ManagedAchievementSerializer(serializers.ModelSerializer[Achievement]):
    """Succès vu du back-office — l'objet, pas ce qu'un client en a fait.

    Ni `progress` ni `is_unlocked` : ce sont la lecture d'*un* client sur *un*
    succès, et ils n'ont pas de valeur hors de ce contexte. `is_active` s'y
    ajoute, que la forme cliente masque — l'écran qui sert à réactiver un succès
    doit pouvoir le voir.
    """

    class Meta:
        model = Achievement
        fields = [
            "id",
            "name",
            "description",
            "icon",
            "condition_type",
            "condition_value",
            "points_reward",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedBadgeSerializer(serializers.ModelSerializer[Badge]):
    class Meta:
        model = Badge
        fields = [
            "id",
            "title",
            "description",
            "icon",
            "points_required",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedChallengeSerializer(serializers.ModelSerializer[Challenge]):
    """Défi vu du back-office.

    `condition_type` y figure alors que la forme cliente l'omet : le client voit
    une cible et une progression, l'exploitation choisit **ce qui est compté**.
    """

    class Meta:
        model = Challenge
        fields = [
            "id",
            "title",
            "description",
            "challenge_type",
            "condition_type",
            "target_value",
            "reward_points",
            "starts_at",
            "ends_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Traduit en 400 ce que la contrainte `CHECK` refuserait en 500.

        La contrainte de base reste la dernière ligne de défense — elle vaut
        pour un script ou un import — mais un défi dont la fin précède le début
        mérite un refus lisible, pas une violation d'intégrité que
        l'exploitation signalerait comme une panne.
        """
        instance = self.instance
        debut = attrs.get("starts_at") or (instance.starts_at if instance else None)
        fin = attrs.get("ends_at") or (instance.ends_at if instance else None)

        if debut is not None and fin is not None and fin <= debut:
            raise serializers.ValidationError(
                {"ends_at": "La fin du défi doit être postérieure à son début."}
            )

        cible = attrs.get("target_value")
        if cible is not None and cible < 1:
            raise serializers.ValidationError(
                {"target_value": "Un défi sans cible atteignable ne se termine jamais."}
            )

        return attrs
