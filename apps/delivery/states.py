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
    "ENGAGED_STATUSES",
    "ORDER_STATUS_PROJECTION",
    "TERMINAL_STATUSES",
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


#: Étapes où le livreur **porte** la course — L6.
#:
#: `offered` n'en fait délibérément pas partie : une proposition ne mobilise
#: personne, et un livreur peut en recevoir plusieurs et choisir. Ce sont les
#: trois étapes suivantes qui l'occupent physiquement — il roule vers un
#: restaurant, puis vers un client.
#:
#: La distinction porte une règle : **un livreur ne tient qu'une course engagée
#: à la fois**. Elle n'existait nulle part. Seule l'unicité par *commande* était
#: gardée (`one_active_assignment_per_order`), ce qui laissait un même livreur en
#: accepter deux — et `Dely` supposait pourtant l'inverse, au point de ne
#: rapporter sa position que pour la première course trouvée. Le client de la
#: seconde commande voyait un livreur figé.
#:
#: Un tuple et non un ensemble, et l'ordre n'est pas décoratif : ces valeurs
#: entrent dans une contrainte de base, dont Django compare la *déconstruction*
#: littérale d'une migration à l'autre. Un ensemble, dont l'ordre d'itération ne
#: se promet pas, ferait apparaître une migration « retirer la contrainte, créer
#: la contrainte » sur une contrainte pourtant identique. L'ordre suivi est celui
#: du cycle de vie.
ENGAGED_STATUSES: tuple[str, ...] = (
    DeliveryStatus.ACCEPTED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.ON_THE_WAY,
)

#: Étapes où la course n'est plus à faire — elle est livrée, refusée ou retirée.
#:
#: Le complément de « vivante ». Écrit une fois ici plutôt que recopié à chaque
#: `exclude(...)` : c'est la liste qu'on oublie de compléter quand une étape
#: s'ajoute, et l'oubli laisse passer une course terminée pour une course en
#: cours.
#:
#: L'ordre reprend celui qu'écrivait `one_active_assignment_per_order` avant
#: d'être extrait ici, pour que ce déplacement ne produise aucune migration : la
#: contrainte est la même, elle doit le rester jusque dans sa déconstruction.
TERMINAL_STATUSES: tuple[str, ...] = (
    DeliveryStatus.DECLINED,
    DeliveryStatus.CANCELLED,
    DeliveryStatus.DELIVERED,
)

# Garde-fou à l'import, du même esprit que celui de la projection : les deux
# ensembles doivent partitionner la machine, sans recouvrement ni oubli. Une
# étape ajoutée à `DeliveryStatus` sans être classée fait échouer le démarrage,
# et non la production.
_non_classes = set(DELIVERY_TRANSITIONS) - set(ENGAGED_STATUSES) - set(TERMINAL_STATUSES)
if _non_classes != {DeliveryStatus.OFFERED}:  # pragma: no cover - vérifié à l'import
    raise ValueError(
        "Étapes de course non classées entre engagées et terminales : "
        f"{sorted(_non_classes - {DeliveryStatus.OFFERED})}. "
        "Toute étape doit dire si elle occupe le livreur."
    )


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
