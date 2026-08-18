"""Cycles de vie de la livraison — ADR-010.

Deux machines, aux natures opposées :

* **La course** est monotone et acyclique. Une livraison ne se re-livre pas, et
  c'est cette propriété qui ferme C3 : rejouer `delivered` réincrémentait les
  compteurs du livreur dans l'implémentation précédente.
* **Le dossier livreur** est délibérément **cyclique**. Un dossier se
  ré-instruit : modifier ses pièces après approbation le repasse en attente
  (L5). D'où `require_acyclic=False`, unique exception assumée.

La projection vers le statut de commande est déclarée ici, à côté des
transitions. C'est précisément une projection écrite à la main qui avait produit
C4 — l'étape `accepted` écrivait sur la commande un statut inexistant.
"""

from __future__ import annotations

from django.db import models

from apps.orders.states import ORDER_MACHINE, OrderStatus
from common.state_machine import StateMachine

__all__ = [
    "DELIVERY_MACHINE",
    "ORDER_STATUS_PROJECTION",
    "VERIFICATION_MACHINE",
    "DeliveryStatus",
    "VerificationStatus",
]


class DeliveryStatus(models.TextChoices):
    OFFERED = "offered", "Proposée"
    ACCEPTED = "accepted", "Acceptée"
    PICKED_UP = "picked_up", "Récupérée"
    ON_THE_WAY = "on_the_way", "En route"
    DELIVERED = "delivered", "Livrée"
    DECLINED = "declined", "Refusée"
    CANCELLED = "cancelled", "Annulée"


DELIVERY_TRANSITIONS: dict[str, set[str]] = {
    DeliveryStatus.OFFERED: {
        DeliveryStatus.ACCEPTED,
        DeliveryStatus.DECLINED,
        DeliveryStatus.CANCELLED,
    },
    DeliveryStatus.ACCEPTED: {DeliveryStatus.PICKED_UP, DeliveryStatus.CANCELLED},
    DeliveryStatus.PICKED_UP: {DeliveryStatus.ON_THE_WAY},
    DeliveryStatus.ON_THE_WAY: {DeliveryStatus.DELIVERED},
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.DECLINED: set(),
    DeliveryStatus.CANCELLED: set(),
}

DELIVERY_MACHINE = StateMachine(DELIVERY_TRANSITIONS, name="course")


# Étapes de course qui doivent faire avancer la commande.
#
# `offered`, `accepted` et `declined` n'y figurent pas volontairement : ce sont
# des événements internes à l'affectation, sans contrepartie côté client. La
# commande reste `ready` tant que le repas n'est pas parti — c'est justement en
# voulant projeter `accepted` que l'ancien code écrivait un statut hors
# énumération.
#
# `cancelled` n'y figure pas non plus, et c'est un **retrait délibéré** par
# rapport à la première rédaction de ce module. Annuler une course est le geste
# courant de réaffectation — un livreur crève un pneu, on en envoie un autre —
# alors qu'annuler une commande est définitif et rembourse le client. Les
# projeter l'un sur l'autre rendait la réaffectation impossible : la commande
# tombait dans un état terminal au premier incident de flotte.
#
# Le sens inverse — une commande annulée doit arrêter la course — est traité
# par une garde dans `AssignmentService.transition_to`, et non par une
# projection : `orders` ne connaît pas `delivery` (ADR-002).
ORDER_STATUS_PROJECTION: dict[str, str] = {
    DeliveryStatus.PICKED_UP: OrderStatus.PICKED_UP,
    DeliveryStatus.ON_THE_WAY: OrderStatus.ON_THE_WAY,
    DeliveryStatus.DELIVERED: OrderStatus.DELIVERED,
}

# Garde-fou à l'import : toute cible de projection doit exister dans la machine
# de la commande. Une faute de frappe fait échouer le démarrage, pas la
# production.
_unknown = set(ORDER_STATUS_PROJECTION.values()) - ORDER_MACHINE.states
if _unknown:  # pragma: no cover - vérifié à l'import, jamais atteint en test
    raise ValueError(
        f"Projection livraison → commande : statuts inconnus {sorted(_unknown)}. "
        "C'est exactement le défaut qui a produit C4."
    )


class VerificationStatus(models.TextChoices):
    PENDING = "pending", "En attente de validation"
    APPROVED = "approved", "Validé"
    REJECTED = "rejected", "Rejeté"
    SUSPENDED = "suspended", "Suspendu"


VERIFICATION_TRANSITIONS: dict[str, set[str]] = {
    VerificationStatus.PENDING: {VerificationStatus.APPROVED, VerificationStatus.REJECTED},
    # L5 — modifier ses pièces après approbation remet le dossier en attente.
    VerificationStatus.APPROVED: {VerificationStatus.PENDING, VerificationStatus.SUSPENDED},
    VerificationStatus.REJECTED: {VerificationStatus.PENDING},
    VerificationStatus.SUSPENDED: {VerificationStatus.APPROVED, VerificationStatus.REJECTED},
}

VERIFICATION_MACHINE = StateMachine(
    VERIFICATION_TRANSITIONS,
    name="dossier livreur",
    require_acyclic=False,
)
