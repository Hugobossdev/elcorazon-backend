"""Tâches planifiées du suivi.

Le suivi n'a de valeur qu'en direct. Sans purge, la table des positions croît
d'environ 1,7 million de lignes par jour à deux cents livreurs actifs, pour une
valeur analytique qui ne justifie pas ce volume.
"""

from __future__ import annotations

from celery import shared_task
from django.conf import settings

from apps.tracking.services import TrackingService

__all__ = ["purge_stale_locations"]


@shared_task
def purge_stale_locations(days: int | None = None) -> int:
    """Supprime les relevés au-delà de la fenêtre de conservation.

    La durée est un réglage : elle relève de la politique de rétention, qui se
    négocie avec le juridique et peut changer sans qu'on redéploie.
    """
    return TrackingService.purge_older_than(
        days=days if days is not None else settings.TRACKING_RETENTION_DAYS
    )
