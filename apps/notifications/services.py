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
import logging
from typing import Any
from uuid import UUID

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
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids, staff_user_ids_for
from common.exceptions import BusinessRuleViolation

__all__ = [
    "MARKETING_KINDS",
    "notify",
    "recipients_of",
    "schedule_campaign",
    "send_campaign",
    "send_due_campaigns",
    "staff_to_alert",
    "unschedule_campaign",
]

logger = logging.getLogger(__name__)

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


def staff_to_alert(*, restaurant_id: UUID, permission: str) -> models.QuerySet[User]:
    """Le personnel d'un établissement habilité à voir ce dont on l'alerte.

    ## Les deux filtres, et pourquoi aucun ne suffit seul

    * **le rattachement** (`StaffMembership`) dit *sur quoi* : sans lui, un
      opérateur de Kara serait réveillé par les commandes de Lomé, qu'il ne
      peut ni voir ni traiter ;
    * **la permission** dit *ce qu'on a le droit de faire* (ADR-005) : alerter
      d'une commande quelqu'un à qui l'API la refusera ensuite en 403 produit
      une notification qui ne mène nulle part.

    C'est la même paire que les vues du back-office opposent — `assert_in_scope`
    et `HasPermission` — appliquée ici pour décider **qui prévenir** plutôt que
    qui laisser entrer. Les recomposer autrement ferait diverger les deux
    lectures, et la notification finirait par désigner une population que
    l'écran n'accepte pas.

    Les superutilisateurs sont joints quel que soit leur rattachement : ils
    n'en ont pas, et ils sont précisément ceux qu'on veut prévenir d'un
    incident.

    ## Pourquoi le périmètre est importé plutôt que traversé

    Le même filtre s'écrit sans import, par la relation inverse
    (`staff_memberships__restaurant_id`) — le nom vient du `related_name` que
    `restaurants` déclare. C'est écarté délibérément : ce serait un couplage
    **réel** que le test d'architecture ne verrait pas, et qui casserait en
    silence, à l'exécution, le jour où ce `related_name` change. L'arête est
    donc déclarée (`notifications → restaurants`, voir `ALLOWED`) et la fonction
    de périmètre appelée, pour que la dépendance soit vérifiée au lieu d'être
    devinée.

    Appeler `staff_user_ids_for` plutôt que de refaire la requête a une seconde
    vertu : le jour où un troisième palier de cloisonnement apparaîtra, les
    alertes le suivront sans qu'on y pense.
    """
    # Passe par le périmètre calculé, et non par la seule table de
    # rattachement : depuis que le cloisonnement a un palier pays/ville
    # (`AreaMembership`), un directeur de marché voit les commandes de son
    # marché au back-office. Lire ici la seule `StaffMembership` le laisserait
    # sans aucune alerte sur ce qu'il est chargé de superviser — les deux
    # lectures doivent désigner la même population.
    rattaches = staff_user_ids_for(restaurant_id=restaurant_id)

    habilites = User.objects.filter(user_type=UserType.STAFF, is_active=True).filter(
        models.Q(is_superuser=True)
        | models.Q(roles__permissions__contains=[permission], pk__in=rattaches)
    )
    # `distinct` : un membre du personnel portant deux rôles qui accordent tous
    # deux la permission sortirait deux fois de la jointure, et recevrait deux
    # notifications identiques pour un seul événement.
    return habilites.distinct()


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
def schedule_campaign(campaign: Campaign, *, quand: dt.datetime) -> Campaign:
    """Date l'envoi d'une campagne — elle partira seule, à l'heure dite.

    Une campagne **envoyée** ne se reprogramme pas : son texte est parti. Une
    campagne déjà programmée, si — c'est ainsi qu'on décale d'une heure.

    L'heure doit être à venir. Une date passée ferait partir la campagne au
    tour suivant du battement, ce qui est peut-être ce que l'on voulait, mais
    se serait décidé sans le dire : mieux vaut refuser et laisser « Envoyer
    maintenant » dire ce qu'il fait.

    ## Le statut se lit sous verrou

    Lu sur l'instance de la vue, il datait d'avant l'envoi que le battement
    était peut-être en train de faire. L'écriture attendait alors le verrou de
    `send_campaign`, puis reposait « programmée » sur une campagne **déjà
    partie** — qui repartait à la nouvelle heure, vers toute la clientèle.
    """
    verrouillee = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if verrouillee.status == CampaignStatus.SENT:
        raise BusinessRuleViolation(
            "Cette campagne est déjà partie : elle ne se reprogramme pas.",
            current_status=verrouillee.status,
        )
    if quand <= timezone.now():
        raise BusinessRuleViolation(
            "L'heure d'envoi doit être à venir — pour partir maintenant, utilisez « Envoyer ».",
            scheduled_at=quand.isoformat(),
        )

    verrouillee.scheduled_at = quand
    verrouillee.status = CampaignStatus.SCHEDULED
    verrouillee.save(update_fields=["scheduled_at", "status", "updated_at"])
    return verrouillee


@transaction.atomic
def unschedule_campaign(campaign: Campaign) -> Campaign:
    """Ramène une campagne programmée à l'état de brouillon, pour la reprendre.

    Sous verrou, pour la même raison que `schedule_campaign` : sans lui, une
    campagne partie pendant le clic redevenait un brouillon — renvoyable.
    """
    verrouillee = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if verrouillee.status != CampaignStatus.SCHEDULED:
        raise BusinessRuleViolation(
            "Seule une campagne programmée s'annule.", current_status=verrouillee.status
        )
    verrouillee.scheduled_at = None
    verrouillee.status = CampaignStatus.DRAFT
    verrouillee.save(update_fields=["scheduled_at", "status", "updated_at"])
    return verrouillee


def send_due_campaigns() -> dict[str, int]:
    """Envoie les campagnes dont l'heure est venue — appelée par le battement.

    Une campagne dont l'heure est passée pendant un arrêt part au premier tour
    suivant, avec du retard plutôt que jamais — le contraire serait une
    campagne silencieusement perdue.

    Chaque campagne est **isolée** : une erreur sur l'une est journalisée et
    n'empêche pas les suivantes de partir. Sans cela, la même campagne en échec
    passait en tête de chaque tour, et bloquait toutes les autres pour toujours.
    """
    echues = list(
        Campaign.objects.filter(status=CampaignStatus.SCHEDULED, scheduled_at__lte=timezone.now())
        .order_by("scheduled_at")
        .values_list("pk", flat=True)
    )

    envoyees = 0
    destinataires = 0
    echecs = 0
    for pk in echues:
        try:
            partie = _send_if_still_due(pk)
        except Exception:
            echecs += 1
            logger.exception(
                "Campagne programmée %s : envoi en échec, retenté au tour suivant.", pk
            )
            continue
        if partie is not None:
            envoyees += 1
            destinataires += partie.recipient_count
    return {"campaigns": envoyees, "recipients": destinataires, "failures": echecs}


@transaction.atomic
def _send_if_still_due(pk: UUID) -> Campaign | None:
    """Envoie la campagne si, **relue sous verrou**, elle est toujours due.

    La liste des campagnes échues est lue sans verrou ; entre cette lecture et
    l'envoi, quelqu'un a pu annuler la programmation ou la décaler. `send_campaign`
    ne refuse qu'une campagne déjà partie : un brouillon qu'on venait de
    déprogrammer partait donc quand même.
    """
    verrouillee = Campaign.objects.select_for_update().get(pk=pk)
    maintenant = timezone.now()
    if (
        verrouillee.status != CampaignStatus.SCHEDULED
        or verrouillee.scheduled_at is None
        or verrouillee.scheduled_at > maintenant
    ):
        return None
    return send_campaign(verrouillee)


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


#: Fenêtre d'attribution d'une commande à une campagne, en jours.
#:
#: Une semaine : assez pour qu'un « −20 % ce week-end » envoyé le lundi compte,
#: assez court pour ne pas attribuer à une campagne les habitudes d'un client
#: qui commande chaque vendredi. Le chiffre est une **corrélation**, pas une
#: causalité, et l'écran le dit.
FENETRE_DE_CONVERSION = dt.timedelta(days=7)


def campaign_stats(campaign: Campaign, *, viewer: User) -> dict[str, Any]:
    """Ce qu'une campagne envoyée a produit — ouvertures, commandes, chiffre.

    Le cahier des charges demande taux d'ouverture, de conversion et ROI
    (§4.2.7). Rien ne les calculait, alors que chaque notification porte déjà
    l'identifiant de sa campagne (`data.campaign`) et son heure de lecture.

    * **Ouverture** : notifications lues sur notifications écrites — donc hors
      comptes ayant refusé le marketing, que `recipient_count` exclut déjà.
    * **Conversion** : destinataires ayant commandé dans la fenêtre qui suit
      l'envoi, commandes annulées exclues.
    * **Chiffre** : total de ces commandes, **par devise** — jamais additionné
      d'une devise à l'autre.

    Les commandes sont cloisonnées au périmètre de qui regarde, comme tout
    rapport : une campagne est un objet d'enseigne, mais le chiffre d'une
    cuisine de Lomé n'a pas à se lire depuis un compte d'Abidjan.

    **Le taux de conversion n'est alors pas calculé**, et c'est la seule
    réponse honnête : son numérateur serait cloisonné et son dénominateur —
    les destinataires de la campagne — ne l'est pas. Un gérant de Lomé lisait
    « 2 % » là où la campagne avait converti 20 % de ses destinataires, les
    autres ayant commandé ailleurs. Un taux faux est pire qu'un taux absent :
    il se compare, se rapporte et se décide. Le nombre de clients et le chiffre
    restent, eux, parfaitement lisibles — ce sont les siens.
    """
    envoyees = Notification.objects.filter(
        kind=NotificationKind.MARKETING, data__campaign=str(campaign.pk)
    )
    lues = envoyees.filter(read_at__isnull=False).count()
    destinataires = campaign.recipient_count

    resultat: dict[str, Any] = {
        "recipients": destinataires,
        "read": lues,
        "open_rate": (lues / destinataires) if destinataires else None,
        "window_days": FENETRE_DE_CONVERSION.days,
        "customers_who_ordered": 0,
        "conversion_rate": None,
        "revenue": [],
    }
    if campaign.sent_at is None or not destinataires:
        return resultat

    commandes = Order.objects.filter(
        customer_id__in=envoyees.values("user_id"),
        placed_at__gte=campaign.sent_at,
        placed_at__lt=campaign.sent_at + FENETRE_DE_CONVERSION,
    ).exclude(status=OrderStatus.CANCELLED)
    cloisonne = not is_unscoped(viewer)
    if cloisonne:
        commandes = commandes.filter(restaurant_id__in=staff_restaurant_ids(viewer))

    clients = commandes.values("customer_id").distinct().count()
    resultat["customers_who_ordered"] = clients
    resultat["conversion_rate"] = None if cloisonne else clients / destinataires
    resultat["revenue"] = [
        {"amount": str(ligne["somme"]), "currency": ligne["total_currency"]}
        for ligne in commandes.values("total_currency")
        .annotate(somme=models.Sum("total_minor"))
        .order_by("total_currency")
    ]
    return resultat
