"""Écriture des événements.

Une seule opération, sans branche : consigner n'est jamais refusé et ne
déclenche jamais d'effet de bord ailleurs — un événement analytics qui ferait
échouer la requête qui l'a produit serait pire que ne pas le consigner du tout.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import User
from apps.analytics.models import AnalyticsEvent

__all__ = ["AnalyticsService"]


class AnalyticsService:
    @staticmethod
    def record(
        *,
        user: User | None,
        event_type: str,
        data: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> AnalyticsEvent:
        return AnalyticsEvent.objects.create(
            user=user, event_type=event_type, event_data=data or {}, session_id=session_id
        )
