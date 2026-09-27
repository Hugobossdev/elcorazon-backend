"""Analytics : journal d'événements — immuable, comme le journal de fidélité (F5).

Un seul modèle : `AnalyticsEvent`. Les rapports (`apps.analytics.reports`) ne
sont pas des tables séparées à tenir à jour — ils s'agrègent à la demande
depuis les commandes, les lignes de commande et les courses, qui sont déjà la
source de vérité de ce qu'ils racontent. Dupliquer ces chiffres dans des tables
de reporting créerait un second endroit où ils peuvent diverger, pour un gain
de performance qu'aucune charge mesurée ne justifie encore.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from common.models import UUIDModel

__all__ = ["AnalyticsEvent"]


class AnalyticsEvent(UUIDModel):
    """Un événement — jamais modifié ni supprimé après écriture.

    `user` est nullable et `on_delete=SET_NULL` : un visiteur non authentifié
    émet des événements (vue de catalogue, recherche) qui ont leur valeur
    statistique propre, et un compte supprimé ne doit pas emporter l'historique
    agrégé avec lui.
    """

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="analytics_events"
    )
    event_type = models.CharField(max_length=64, db_index=True)
    event_data = models.JSONField(default=dict, blank=True)
    session_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "événement analytics"
        verbose_name_plural = "événements analytics"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event_type", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.event_type} — {self.created_at:%Y-%m-%d %H:%M}"
