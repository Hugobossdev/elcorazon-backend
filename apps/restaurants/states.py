"""Cycle de vie d'un établissement — ADR-010.

Ouvrir un restaurant n'est pas un geste unitaire : il faut une position, des
horaires, une carte, des livreurs. Tant que ces pièces manquent, l'établissement
existe en base sans pouvoir servir qui que ce soit — et c'est exactement l'état
que le modèle ne savait pas représenter.

`is_active` valait `True` par défaut. Un `POST /restaurants/manage/` publiait
donc l'établissement **à l'instant de sa création** : il apparaissait dans
l'application cliente, sans carte ni horaires, et un client pouvait ouvrir sa
fiche pour y trouver le vide. Le seul contournement était de créer puis de
désactiver aussitôt — deux requêtes, avec une fenêtre entre les deux, et rien
qui empêche de l'oublier.

`status` porte donc la configuration **et** l'exploitation sur un seul axe :

    brouillon → en configuration → prêt → en service ⇄ suspendu

Les deux derniers états sont ceux que `is_active` distinguait ; les trois
premiers sont ceux qu'il ne savait pas dire. `is_active` reste en base, dérivé
de `status` (voir `Restaurant.save`), pour que les huit filtres de production
qui l'interrogent — panier, commande, catalogue, candidature livreur — restent
justes sans être réécrits.

Le retour est autorisé : un établissement suspendu revient en service, et un
établissement prêt retourne en configuration quand on s'aperçoit qu'il manque
une catégorie. D'où `require_acyclic=False`, comme pour le dossier livreur.
"""

from __future__ import annotations

from django.db import models

from common.state_machine import StateMachine

__all__ = [
    "KNOWN_TO_CUSTOMERS",
    "RESTAURANT_MACHINE",
    "RESTAURANT_TRANSITIONS",
    "RestaurantStatus",
]


class RestaurantStatus(models.TextChoices):
    DRAFT = "draft", "Brouillon"
    CONFIGURING = "configuring", "En configuration"
    READY = "ready", "Prêt à ouvrir"
    ACTIVE = "active", "En service"
    INACTIVE = "inactive", "Suspendu"


#: États qu'un client a pu connaître : en service, ou suspendu **après** l'avoir
#: été.
#:
#: Sert à la création de commande. Filtrée sur `is_active`, elle répondait à une
#: cuisine suspendue pendant que le client payait par un `400` « objet
#: introuvable » sur le champ `restaurant` — une erreur de saisie, pour une
#: cuisine que le client venait de voir. Elle laisse maintenant le juge répondre
#: `409 kitchen_suspended`. Un brouillon, jamais publié, reste introuvable : dire
#: « suspendu » d'un établissement qui n'a jamais ouvert révélerait son
#: existence.
KNOWN_TO_CUSTOMERS = frozenset({RestaurantStatus.ACTIVE, RestaurantStatus.INACTIVE})


#: Transitions autorisées.
#:
#: `DRAFT → CONFIGURING` est le seul chemin sortant du brouillon : on ne publie
#: pas depuis la fiche d'identité, il faut être passé par la configuration.
#:
#: `READY → ACTIVE` est la publication, et la seule transition que la
#: vérification de complétude garde (`Restaurant.configuration_gaps`).
#:
#: `INACTIVE → CONFIGURING` existe pour la réouverture après refonte : on
#: suspend, on refait la carte, on republie. Sans elle il faudrait rouvrir au
#: public pour pouvoir redescendre en configuration.
RESTAURANT_TRANSITIONS: dict[str, set[str]] = {
    RestaurantStatus.DRAFT: {RestaurantStatus.CONFIGURING},
    RestaurantStatus.CONFIGURING: {RestaurantStatus.DRAFT, RestaurantStatus.READY},
    RestaurantStatus.READY: {RestaurantStatus.CONFIGURING, RestaurantStatus.ACTIVE},
    RestaurantStatus.ACTIVE: {RestaurantStatus.INACTIVE},
    RestaurantStatus.INACTIVE: {RestaurantStatus.ACTIVE, RestaurantStatus.CONFIGURING},
}

RESTAURANT_MACHINE = StateMachine(
    RESTAURANT_TRANSITIONS,
    name="établissement",
    require_acyclic=False,
)
