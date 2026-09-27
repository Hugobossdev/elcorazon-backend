"""Affectation automatique des courses — commande prête → livreur compatible.

## Ce qui existait

L'affectation était **entièrement manuelle** : une commande passait « prête »,
et rien ne se produisait tant qu'un membre du personnel n'ouvrait pas la liste
des livreurs disponibles pour en choisir un. Un soir de coup de feu, avec vingt
commandes et un seul superviseur, le repas refroidissait au passe pendant que
trois livreurs attendaient en ligne.

## La règle

Quand une commande entre en `ready`, dans une cuisine qui l'autorise
(`Restaurant.auto_dispatch_couriers`), la course est proposée au meilleur
livreur **compatible** :

1. rattaché à la cuisine de la commande, dossier validé, en ligne, compte
   ouvert, sans course engagée — `CourierService.available_for`, la même liste
   que voit le back-office, qui porte aussi le périmètre de **zone** ;
2. parmi eux, d'abord ceux qui **n'ont pas déjà refusé** cette commande, puis
   ceux qui **n'ont aucune proposition en attente** ailleurs, puis le plus
   proche de la cuisine.

Le refus antérieur ne l'exclut pas : il le fait passer derrière. Un livreur
seul en ligne qui a laissé passer une proposition doit pouvoir la recevoir de
nouveau — l'exclure laisserait la commande sans personne, indéfiniment.

## Ce qui relance

* le passage en `ready` ;
* un **refus** par le livreur ;
* une proposition **restée sans réponse** au-delà de
  `DELIVERY_OFFER_TTL_SECONDS` — l'horloge `expire-stale-offers` la clôt et
  propose au suivant ;
* un livreur qui **se met en ligne** alors que des commandes prêtes attendent.

## Ce que ce module ne fait pas

Il ne court-circuite **aucune** garde : il appelle `AssignmentService.offer`,
qui relit le dossier (L1), l'unicité de la course active (L2), la course
engagée du livreur (L6) et le périmètre de zone, sous verrou. Une course que la
proposition manuelle refuserait, l'automatique la refuse aussi. L'affectation
manuelle reste possible à tout moment.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.dispatch import receiver
from django.utils import timezone

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService, CourierService
from apps.delivery.signals import assignment_declined, courier_went_online
from apps.delivery.states import TERMINAL_STATUSES, DeliveryStatus
from apps.orders.models import Order
from apps.orders.signals import order_status_changed
from apps.orders.states import OrderStatus
from common.exceptions import BusinessRuleViolation
from common.realtime import courier_group, publish

__all__ = ["EXPIRED_OFFER_REASON", "DispatchService"]

logger = logging.getLogger(__name__)

#: Le motif écrit sur une proposition close faute de réponse. Une course reste
#: `declined` — il n'existe pas d'état « expirée », et en ajouter un changerait
#: la contrainte de la machine pour un cas que le motif dit déjà.
EXPIRED_OFFER_REASON = "Sans réponse dans le délai : course proposée à un autre livreur."

#: Candidats essayés au plus pour une commande. Au-delà, une concurrence
#: exceptionnelle (chacun venant d'accepter ailleurs) se règle au tour suivant
#: de l'horloge plutôt que par une boucle sans fin sous verrou.
_ESSAIS_MAX = 5

#: Commandes prêtes rattrapées au plus par tour d'horloge.
_RATTRAPAGE_MAX = 200


class DispatchService:
    @staticmethod
    def dispatch(order_id: uuid.UUID) -> Assignment | None:
        """Propose la course de cette commande au meilleur livreur compatible.

        Rend la course proposée, ou `None` — commande introuvable, pas prête,
        cuisine en affectation manuelle, course déjà en cours, ou personne de
        compatible. Ne lève jamais : elle est appelée après un `commit`, là où
        une exception n'aurait plus personne pour la rattraper.
        """
        order = Order.objects.select_related("restaurant").filter(pk=order_id).first()
        if order is None or order.status != OrderStatus.READY:
            return None
        if not order.restaurant.auto_dispatch_couriers:
            return None
        if order.assignments.exclude(status__in=TERMINAL_STATUSES).exists():
            return None

        for courier in DispatchService.candidates(order)[:_ESSAIS_MAX]:
            try:
                assignment = AssignmentService.offer(order=order, courier=courier, actor=None)
            except BusinessRuleViolation as refus:
                # Entre la lecture et le verrou, ce livreur a accepté ailleurs,
                # ou la commande a reçu une course : le premier cas se règle au
                # suivant, le second arrête tout.
                if order.assignments.exclude(status__in=TERMINAL_STATUSES).exists():
                    return None
                logger.info(
                    "delivery.dispatch.skipped",
                    extra={"order": order.reference, "courier": str(courier.pk), "why": str(refus)},
                )
                continue

            logger.info(
                "delivery.dispatch.offered",
                extra={
                    "order": order.reference,
                    "kitchen": order.restaurant.slug,
                    "zone": order.delivery_zone_name,
                    "courier": str(courier.pk),
                },
            )
            return assignment

        logger.info(
            "delivery.dispatch.no_courier",
            extra={
                "order": order.reference,
                "kitchen": order.restaurant.slug,
                "zone": order.delivery_zone_name,
            },
        )
        return None

    @staticmethod
    def candidates(order: Order) -> list[CourierProfile]:
        """Les livreurs compatibles, dans l'ordre où la course leur est proposée."""
        a_refuse = Exists(
            Assignment.objects.filter(
                courier=OuterRef("pk"), order=order, status=DeliveryStatus.DECLINED
            )
        )
        deja_sollicite = Exists(
            Assignment.objects.filter(courier=OuterRef("pk"), status=DeliveryStatus.OFFERED)
        )
        return list(
            CourierService.available_for(order)
            .annotate(a_refuse=a_refuse, deja_sollicite=deja_sollicite)
            .order_by("a_refuse", "deja_sollicite", F("to_restaurant").asc(nulls_last=True))
        )

    @staticmethod
    @transaction.atomic
    def expire(assignment_id: uuid.UUID) -> bool:
        """Clôt une proposition restée sans réponse. Rend vrai si elle l'a close.

        Le verrou est pris sur la **commande**, dans le même ordre qu'`offer` et
        `accept` : une acceptation qui arrive à la même milliseconde passe
        avant, ou trouve la proposition déjà close — jamais les deux.
        """
        assignment = Assignment.objects.select_related("order").filter(pk=assignment_id).first()
        if assignment is None:
            return False
        Order.objects.select_for_update().get(pk=assignment.order_id)
        assignment.refresh_from_db()
        if assignment.status != DeliveryStatus.OFFERED:
            return False

        assignment.status = DeliveryStatus.DECLINED
        assignment.decline_reason = EXPIRED_OFFER_REASON
        assignment.save(update_fields=["status", "decline_reason", "updated_at"])

        # Retirée de la file du livreur : sans cet événement, son application
        # garderait affichée une proposition qu'il ne peut plus accepter.
        transaction.on_commit(
            lambda: publish(
                courier_group(assignment.courier_id),
                "delivery.offer_expired",
                {"assignment": str(assignment.pk), "order": str(assignment.order_id)},
            )
        )
        logger.info(
            "delivery.dispatch.expired",
            extra={"order": assignment.order.reference, "courier": str(assignment.courier_id)},
        )
        return True

    @staticmethod
    def expire_stale_offers(*, now: dt.datetime | None = None) -> int:
        """Clôt les propositions trop anciennes des cuisines en affectation automatique."""
        limite = (now or timezone.now()) - dt.timedelta(seconds=settings.DELIVERY_OFFER_TTL_SECONDS)
        perimees = Assignment.objects.filter(
            status=DeliveryStatus.OFFERED,
            offered_at__lt=limite,
            order__restaurant__auto_dispatch_couriers=True,
        ).values_list("pk", "order_id")

        closes = 0
        commandes: set[uuid.UUID] = set()
        for assignment_id, order_id in perimees:
            if DispatchService.expire(assignment_id):
                closes += 1
                commandes.add(order_id)
        for order_id in commandes:
            DispatchService.dispatch(order_id)
        return closes

    @staticmethod
    def dispatch_waiting(*, restaurant_id: uuid.UUID | None = None) -> int:
        """Propose les commandes prêtes restées sans course. Rend le nombre proposé.

        C'est le rattrapage : une commande passée prête quand personne n'était
        en ligne, ou dont la proposition a échoué, ne doit pas attendre qu'un
        superviseur la remarque.
        """
        actives = Assignment.objects.filter(order=OuterRef("pk")).exclude(
            status__in=TERMINAL_STATUSES
        )
        attente = (
            Order.objects.filter(status=OrderStatus.READY, restaurant__auto_dispatch_couriers=True)
            .exclude(Exists(actives))
            .order_by("placed_at")
        )
        if restaurant_id is not None:
            attente = attente.filter(restaurant_id=restaurant_id)

        proposees = 0
        for order_id in attente.values_list("pk", flat=True)[:_RATTRAPAGE_MAX]:
            if DispatchService.dispatch(order_id) is not None:
                proposees += 1
        return proposees


def _apres_commit(travail: Any) -> None:
    """Exécute après le `commit`, et ne laisse jamais une panne remonter.

    La commande est déjà écrite quand l'affectation s'exécute : un échec ici —
    Redis indisponible pour la diffusion, une contrainte levée par une course
    concurrente — ne doit pas se lire comme un échec du passage en « prête ».
    L'horloge rattrape au tour suivant.
    """

    def _executer() -> None:
        try:
            travail()
        except Exception:  # pragma: no cover - filet, l'horloge rattrape
            logger.exception("delivery.dispatch.failed")

    transaction.on_commit(_executer)


@receiver(order_status_changed, sender=Order, dispatch_uid="delivery.dispatch_on_ready")
def on_order_ready(sender: type[Order], *, order: Order, target: str, **kwargs: Any) -> None:
    """Une commande prête cherche son livreur."""
    if target == OrderStatus.READY:
        order_id = order.pk
        _apres_commit(lambda: DispatchService.dispatch(order_id))


@receiver(order_status_changed, sender=Order, dispatch_uid="delivery.close_on_order_cancelled")
def on_order_cancelled(sender: type[Order], *, order: Order, target: str, **kwargs: Any) -> None:
    """Une commande annulée referme sa course encore ouverte.

    Synchrone, et non `_apres_commit` : la fermeture doit tenir ou tomber avec
    l'annulation. Une course laissée « proposée » sur une commande annulée
    pouvait être acceptée par le livreur.
    """
    if target == OrderStatus.CANCELLED:
        AssignmentService.close_for_cancelled_order(
            order=order, reason="La commande a été annulée."
        )


@receiver(assignment_declined, sender=Assignment, dispatch_uid="delivery.dispatch_on_decline")
def on_assignment_declined(
    sender: type[Assignment], *, assignment: Assignment, **kwargs: Any
) -> None:
    """Un refus relance la recherche."""
    order_id = assignment.order_id
    _apres_commit(lambda: DispatchService.dispatch(order_id))


@receiver(courier_went_online, sender=CourierProfile, dispatch_uid="delivery.dispatch_on_online")
def on_courier_online(
    sender: type[CourierProfile], *, courier: CourierProfile, **kwargs: Any
) -> None:
    """Un livreur qui se met en ligne reçoit ce qui attendait dans sa cuisine."""
    restaurant_id = courier.restaurant_id
    _apres_commit(lambda: DispatchService.dispatch_waiting(restaurant_id=restaurant_id))
