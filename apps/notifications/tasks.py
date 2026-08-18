"""Tâches d'envoi — ADR-008.

**Tout appel réseau sortant quitte le cycle de requête.** Un envoi FCM demande
un jeton OAuth puis un POST par appareil : le faire dans la vue ajouterait des
centaines de millisecondes à chaque changement de statut de commande, et ferait
échouer la transaction métier quand le service push est indisponible.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import Task, shared_task
from celery.exceptions import MaxRetriesExceededError
from django.utils import timezone

from apps.accounts.models import Device
from apps.notifications.models import Notification
from apps.notifications.push import (
    PushDeliveryIncomplete,
    PushMessage,
    backend,
    payload_for,
)

__all__ = ["purge_unregistered_devices", "send_push"]

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    # Report exponentiel avec dispersion : sans le `jitter`, les milliers de
    # tâches mises en échec par une même panne de service reviennent toutes à
    # la même seconde et la prolongent.
    retry_backoff=True,
    retry_jitter=True,
)
def send_push(self: Task, notification_id: str, tokens: list[str] | None = None) -> dict[str, int]:
    """Envoie une notification aux appareils de son destinataire.

    Deux issues d'échec, traitées différemment — c'est tout l'enjeu :

    * **définitif** — le service déclare l'appareil injoignable : le jeton est
      supprimé. C'est ce qui manquait à l'implémentation précédente, qui
      retentait trois fois un appareil désinstallé, à chaque notification,
      indéfiniment ;
    * **passager** — quota, délai dépassé, panne : une reprise est demandée,
      **portant sur les seuls jetons concernés**. Reprendre toute la liste
      ferait vibrer deux fois le téléphone de ceux qui avaient reçu.

    `tokens` n'est renseigné qu'à la reprise : au premier appel, la tâche
    s'adresse à tous les appareils du destinataire.
    """
    notification = Notification.objects.select_related("user").filter(pk=notification_id).first()
    if notification is None:
        # La notification a été supprimée entre la programmation et l'exécution
        # — un compte effacé, par exemple. Ce n'est pas une erreur à retenter.
        logger.info("push.notification_absente", extra={"notification": notification_id})
        return _rien()

    joignables = Device.objects.filter(user=notification.user)
    if tokens is not None:
        # Les jetons d'une reprise sont refiltrés : l'un d'eux a pu être purgé
        # entre-temps, et réessayer sur un appareil supprimé rejouerait la
        # boucle qu'on cherche justement à casser.
        joignables = joignables.filter(token__in=tokens)

    cibles = list(joignables.values_list("token", flat=True))
    if not cibles:
        return _rien()

    result = backend().send(
        cibles,
        PushMessage(
            title=notification.title,
            body=notification.body,
            data=payload_for(notification.kind, notification.data),
        ),
    )

    purged = 0
    if result.unregistered:
        purged, _ = Device.objects.filter(token__in=result.unregistered).delete()

    if result.failed:
        _demander_reprise(self, notification_id, result.failed)

    return {
        "delivered": len(result.delivered),
        "purged": purged,
        "failed": len(result.failed),
    }


def _rien() -> dict[str, int]:
    return {"delivered": 0, "purged": 0, "failed": 0}


def _demander_reprise(task: Task, notification_id: str, tokens: tuple[str, ...]) -> None:
    """Reprogramme la tâche sur les seuls jetons en échec passager.

    `retry` lève : c'est son mécanisme, et l'exception ne doit surtout pas être
    interceptée ici. Seul l'épuisement des tentatives l'est — abandonner une
    notification après quatre essais est un incident à tracer, pas une tâche à
    laisser échouer bruyamment toutes les heures.
    """
    try:
        task.retry(
            args=(notification_id, list(tokens)),
            exc=PushDeliveryIncomplete(tokens),
        )
    except MaxRetriesExceededError:
        logger.warning(
            "push.abandon",
            extra={"notification": notification_id, "tokens": len(tokens)},
        )


@shared_task
def purge_unregistered_devices(days: int = 180) -> int:
    """Supprime les appareils muets depuis longtemps.

    Complément de la purge à l'envoi : un appareil peut cesser de répondre sans
    que le service push le déclare jamais injoignable — téléphone perdu,
    application jamais rouverte. Six mois sans un seul rafraîchissement de
    jeton suffisent à conclure.
    """
    horizon = timezone.now() - dt.timedelta(days=days)
    deleted, _ = Device.objects.filter(last_used_at__lt=horizon).delete()
    return deleted
