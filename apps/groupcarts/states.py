"""Cycle de vie d'un panier collaboratif — ADR-010.

Déclaré séparément des modèles, comme pour la commande : la table de transitions
doit être importable par les tests et par la contrainte `CHECK` sans tirer tout
le registre Django.

Le graphe est **acyclique**, et c'est ce qui compte ici. Un panier collaboratif
se referme et se rouvre volontiers dans l'esprit de qui l'utilise — « attendez,
j'ajoute une boisson » — mais autoriser `locked → open` rendrait la confirmation
non déterministe : l'hôte relit un total, quelqu'un ajoute un plat, l'hôte paie
un autre montant que celui qu'il a lu. Le geste existe donc côté produit, il
prend juste la forme d'un nouveau panier plutôt que d'un retour en arrière.
"""

from __future__ import annotations

from django.db import models

from common.state_machine import StateMachine

__all__ = ["GROUP_CART_MACHINE", "GROUP_CART_TRANSITIONS", "GroupCartStatus"]


class GroupCartStatus(models.TextChoices):
    OPEN = "open", "Ouvert aux ajouts"
    LOCKED = "locked", "Clos, en attente de confirmation"
    CONFIRMED = "confirmed", "Confirmé en commande"
    CANCELLED = "cancelled", "Annulé"
    EXPIRED = "expired", "Échéance dépassée"


GROUP_CART_TRANSITIONS: dict[str, set[str]] = {
    # L'hôte clôt les ajouts pour relire un total stable, ou renonce.
    GroupCartStatus.OPEN: {
        GroupCartStatus.LOCKED,
        GroupCartStatus.CANCELLED,
        GroupCartStatus.EXPIRED,
    },
    # Depuis `locked`, l'échéance peut encore tomber : l'hôte a clos les ajouts
    # puis n'a jamais confirmé, et le panier ne doit pas rester éternellement en
    # attente d'un paiement qui ne vient pas.
    GroupCartStatus.LOCKED: {
        GroupCartStatus.CONFIRMED,
        GroupCartStatus.CANCELLED,
        GroupCartStatus.EXPIRED,
    },
    GroupCartStatus.CONFIRMED: set(),
    GroupCartStatus.CANCELLED: set(),
    GroupCartStatus.EXPIRED: set(),
}

GROUP_CART_MACHINE = StateMachine(GROUP_CART_TRANSITIONS, name="panier collaboratif")

#: États depuis lesquels l'échéance a encore un sens.
EXPIRABLE = frozenset({GroupCartStatus.OPEN, GroupCartStatus.LOCKED})
