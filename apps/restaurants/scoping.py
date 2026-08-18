"""Périmètre d'un membre du personnel — ADR-005, troisième étage.

Le modèle d'autorisation a trois étages : le type de compte, la permission
nommée, et **l'appartenance de la ressource**. Les deux premiers vivent dans
`common.permissions` ; le troisième est ici, et il s'applique dans les
`get_queryset` — pas dans une permission d'objet, sinon la ressource interdite
serait d'abord chargée puis refusée, ce qui trahit son existence par le code de
statut.

Sans ce filtre, « personnel » désigne une population indistincte : un opérateur
du restaurant de Kara lit et fait avancer les commandes de Lomé. La permission
dit ce qu'on a le droit de faire ; ce module dit sur quoi.
"""

from __future__ import annotations

import uuid

from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.restaurants.models import StaffMembership
from common.permissions import is_unscoped

__all__ = ["assert_in_scope", "is_in_scope", "is_unscoped", "staff_restaurant_ids"]

# `is_unscoped` vit dans le socle depuis que la géographie en a eu besoin : un
# pays n'appartient à aucun établissement, et `geography` ne connaît pas
# `restaurants` (ADR-002). Il reste exporté ici, où les appelants le
# cherchent — le périmètre du personnel est le sujet de ce module.


def staff_restaurant_ids(user: User) -> set[uuid.UUID]:
    """Établissements sur lesquels ce compte a un rattachement.

    Ensemble vide pour un membre du personnel non rattaché : il ne verra rien,
    et c'est le bon défaut. Une panne visible se corrige en une ligne de
    back-office ; un accès trop large, silencieux, ne se découvre pas.
    """
    return set(StaffMembership.objects.filter(user=user).values_list("restaurant_id", flat=True))


def is_in_scope(user: User, restaurant_id: uuid.UUID) -> bool:
    """Ce compte a-t-il ce restaurant dans son périmètre ?"""
    return is_unscoped(user) or restaurant_id in staff_restaurant_ids(user)


def assert_in_scope(user: User, restaurant_id: uuid.UUID) -> None:
    """Refuse une **écriture** hors périmètre.

    Le filtre de `get_queryset` suffit à cacher ce qu'on n'a pas le droit de
    lire ; il ne peut rien contre une création, qui désigne son établissement
    dans le corps de la requête. Sans cette garde, un opérateur de Kara
    ajouterait un article à la carte de Lomé — l'objet n'existe pas encore, il
    n'y a donc aucun `get_object` pour le refuser.

    Le refus est explicite (403) et non un « introuvable » : contrairement à la
    lecture, il n'y a ici aucune existence à trahir, et un message clair évite
    de faire chercher une panne de configuration là où il y a un droit
    manquant.
    """
    if not is_in_scope(user, restaurant_id):
        raise PermissionDenied(
            "Cet établissement n'est pas dans votre périmètre : "
            "un rattachement est nécessaire pour y écrire."
        )
