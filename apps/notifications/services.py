"""Émission des notifications — ADR-008.

Un même événement métier produit jusqu'à trois choses : une ligne persistante,
un message WebSocket si l'écran est ouvert, un push si l'application est
fermée. Ce module décide **quoi partir où**, et c'est le seul endroit qui le
décide — sans quoi chaque appelant se ferait sa propre idée du transactionnel
et du marketing.

Rien ici n'appelle le réseau : l'envoi part par Celery (`tasks.py`). Un jeton
OAuth suivi d'un POST par appareil ajouterait des centaines de millisecondes à
chaque changement de statut de commande.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from apps.accounts.models import User, UserType
from apps.notifications.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Notification,
    NotificationKind,
)

__all__ = ["MARKETING_KINDS", "notify", "recipients_of", "send_campaign"]

#: Catégories soumises au consentement de l'utilisateur.
#:
#: Tout le reste est transactionnel et part quoi qu'il arrive : « votre livreur
#: arrive » n'est pas une sollicitation commerciale, et le couper au motif que
#: l'utilisateur a refusé le marketing produirait un client planté devant sa
#: porte sans savoir que le repas est là.
MARKETING_KINDS = frozenset({NotificationKind.MARKETING})


def notify(
    *,
    user: User,
    kind: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    push: bool = True,
) -> Notification | None:
    """Enregistre une notification et programme son envoi push.

    Renvoie `None` quand le consentement manque pour une catégorie qui l'exige
    — rien n'est alors écrit non plus : une notification marketing qu'on ne
    peut pas envoyer n'a pas à encombrer l'historique de quelqu'un qui l'a
    refusée.

    L'envoi est programmé **après le commit**. Une tâche postée pendant la
    transaction peut être consommée par un worker avant que celle-ci ne soit
    validée : le worker lit alors une notification qui n'existe pas encore, ou
    envoie un push pour une commande qui sera annulée par un `ROLLBACK`.
    """
    if kind in MARKETING_KINDS and not _accepts_marketing(user):
        return None

    notification = Notification.objects.create(
        user=user, kind=kind, title=title, body=body, data=data or {}
    )

    if push:
        transaction.on_commit(lambda: _dispatch(notification.pk))

    return notification


def _accepts_marketing(user: User) -> bool:
    """Consentement au marketing push.

    Absence de préférences enregistrées vaut acceptation — c'est le défaut du
    modèle, et le refuser ici ferait taire toute communication tant que
    l'utilisateur n'a pas visité un écran de réglages qu'il ne visitera jamais.
    """
    preferences = getattr(user, "preferences", None)
    return True if preferences is None else bool(preferences.marketing_push_enabled)


def _dispatch(notification_id: object) -> None:
    """Poste la tâche d'envoi.

    Importée ici et non en tête de module : `tasks` importe ce module pour
    lire les notifications, et l'import croisé au chargement ferait échouer le
    démarrage du worker.
    """
    from apps.notifications.tasks import send_push

    send_push.delay(str(notification_id))


# ----------------------------------------------------------------- campagnes


def recipients_of(campaign: Campaign) -> models.QuerySet[User]:
    """Population visée par une campagne.

    Les comptes inactifs sont exclus de tous les segments : écrire à un compte
    bloqué serait au mieux inutile, au pire une relance commerciale adressée à
    quelqu'un dont on vient de fermer le compte.
    """
    horizon = timezone.now() - dt.timedelta(days=campaign.segment_days)

    if campaign.audience == Audience.COURIERS:
        return User.objects.filter(user_type=UserType.COURIER, is_active=True)

    clients = User.objects.filter(user_type=UserType.CUSTOMER, is_active=True)

    if campaign.audience == Audience.ACTIVE_CUSTOMERS:
        return clients.filter(orders__placed_at__gte=horizon).distinct()
    if campaign.audience == Audience.LAPSED_CUSTOMERS:
        # `exclude` plutôt que « dernière commande antérieure à l'horizon » : la
        # formulation par exclusion embarque aussi les comptes qui n'ont jamais
        # commandé, et c'est la population qu'une campagne de reconquête vise
        # en premier.
        return clients.exclude(orders__placed_at__gte=horizon)

    return clients


@transaction.atomic
def send_campaign(campaign: Campaign) -> Campaign:
    """Envoie une campagne — **une seule fois**.

    Le verrou et la relecture du statut ne sont pas de la prudence : deux clics
    sur « envoyer » arrivent régulièrement, et sans eux les deux requêtes
    lisent « brouillon » puis écrivent chacune leur lot de notifications. Le
    destinataire, lui, reçoit deux fois le même message et se désabonne.

    Le consentement n'est **pas** revérifié ici : `notify` écarte déjà les
    comptes ayant refusé le marketing. Le redécider à cet endroit produirait
    deux règles de consentement, dont l'une finirait par être la mauvaise.
    `recipient_count` compte donc les envois réels, pas la taille du segment.
    """
    verrouillee = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if verrouillee.status == CampaignStatus.SENT:
        return verrouillee

    envoyees = 0
    for destinataire in recipients_of(verrouillee).iterator(chunk_size=500):
        envoi = notify(
            user=destinataire,
            kind=NotificationKind.MARKETING,
            title=verrouillee.title,
            body=verrouillee.body,
            data={"campaign": str(verrouillee.pk)},
        )
        if envoi is not None:
            envoyees += 1

    verrouillee.status = CampaignStatus.SENT
    verrouillee.sent_at = timezone.now()
    verrouillee.recipient_count = envoyees
    verrouillee.save(update_fields=["status", "sent_at", "recipient_count", "updated_at"])
    return verrouillee
