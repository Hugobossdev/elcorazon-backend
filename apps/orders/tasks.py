"""Tâches planifiées des commandes."""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.orders.models import IdempotencyKey

__all__ = ["purge_idempotency_keys"]


@shared_task
def purge_idempotency_keys(hours: int | None = None) -> int:
    """Supprime les clés d'idempotence consommées et périmées.

    ADR-009 : une clé ne sert que le temps où un client mobile peut retenter.
    Passé ce délai, elle n'empêche plus rien et ne fait que grossir une table
    écrite à chaque commande.

    La fenêtre est large exprès. La réduire économiserait des lignes et
    rouvrirait la porte au doublon qu'on cherche à empêcher : un téléphone
    éteint dans une zone sans réseau peut retenter longtemps après.
    """
    window = hours if hours is not None else settings.IDEMPOTENCY_RETENTION_HOURS
    horizon = timezone.now() - dt.timedelta(hours=window)

    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=horizon).delete()
    return deleted
