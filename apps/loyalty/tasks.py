"""Tâches planifiées de la fidélité."""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.loyalty.models import PointsAccount
from apps.loyalty.services import LoyaltyService
from apps.loyalty.subscriptions import SubscriptionService

__all__ = ["expire_points", "renew_subscriptions"]


@shared_task
def expire_points(months: int | None = None) -> int:
    """Éteint les soldes restés sans mouvement.

    La politique est **l'inactivité**, pas une date d'acquisition : « les
    points s'éteignent après douze mois sans activité » se dit en une phrase,
    donc se comprend, donc se conteste. Une expiration par lot demanderait de
    savoir quel crédit a payé quel débit — un second journal, pour une règle
    que personne ne saurait expliquer au téléphone.

    Un compte jamais actif n'est pas touché : il n'a rien à perdre, et le
    parcourir chaque jour pour rien coûterait à mesure que la base grossit.
    """
    fenetre = months if months is not None else settings.LOYALTY_EXPIRY_MONTHS
    horizon = timezone.now() - dt.timedelta(days=30 * fenetre)

    eteints = 0
    comptes = PointsAccount.objects.filter(
        balance__gt=0, last_activity_at__isnull=False, last_activity_at__lt=horizon
    )
    for compte in comptes.iterator():
        if LoyaltyService.expire_inactive(account=compte, moment=horizon) is not None:
            eteints += 1

    return eteints


@shared_task
def renew_subscriptions() -> int:
    """Facture les abonnements échus, expire ceux hors délai de grâce.

    Horaire plutôt qu'ordonnancé par échéance (comme `expire_points`) : le
    volume ne justifie pas encore un ordonnanceur dédié, et une passe horaire
    suffit largement face à des périodes comptées en jours. Ouvre la demande
    de paiement ; l'activation attend, comme toujours, la notification du
    prestataire — jamais cette tâche elle-même.
    """
    return SubscriptionService.renew_due()
