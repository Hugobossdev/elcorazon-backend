"""Cycle de vie d'une commande — ADR-010.

Déclaré ici, séparé des modèles, pour que la table de transitions soit
importable par les tests et par la projection depuis la livraison sans tirer
tout le registre des modèles Django.

Le graphe est validé à l'import : une cible non déclarée ou un cycle font
échouer le démarrage, donc la CI. C'est ce qui rend C3 et C4 inexprimables
plutôt que « à ne pas oublier ».
"""

from __future__ import annotations

from django.db import models

from common.state_machine import StateMachine

__all__ = ["ORDER_MACHINE", "ORDER_TRANSITIONS", "OrderStatus"]


class OrderStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    CONFIRMED = "confirmed", "Confirmée"
    PREPARING = "preparing", "En préparation"
    READY = "ready", "Prête"
    PICKED_UP = "picked_up", "Récupérée"
    ON_THE_WAY = "on_the_way", "En route"
    DELIVERED = "delivered", "Livrée"
    CANCELLED = "cancelled", "Annulée"


ORDER_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.PENDING: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.PICKED_UP, OrderStatus.CANCELLED},
    # Passé l'enlèvement, l'annulation n'est plus possible : le repas est parti.
    # Un incident après ce point relève du remboursement, pas de l'annulation.
    OrderStatus.PICKED_UP: {OrderStatus.ON_THE_WAY},
    OrderStatus.ON_THE_WAY: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}

ORDER_MACHINE = StateMachine(ORDER_TRANSITIONS, name="commande")
