"""Ce que le catalogue exige avant qu'un établissement ouvre.

L'arête va d'ici vers `restaurants` — le sens déclaré dans le graphe de
l'ADR-002, et le seul qui n'introduise pas de cycle. `restaurants` ne connaît
pas ce module ; il expose un point d'abonnement et reçoit des phrases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.catalog.models import Category, MenuItem

if TYPE_CHECKING:  # pragma: no cover
    from apps.restaurants.models import Restaurant

__all__ = ["catalogue_gaps"]


def catalogue_gaps(restaurant: Restaurant) -> list[str]:
    """Un établissement sans carte n'a rien à vendre.

    Les deux contrôles sont distincts et le restent : une catégorie sans
    article et un article sans catégorie active produisent le même écran vide
    pour le client, mais pas le même geste de correction.
    """
    manques: list[str] = []

    if not Category.objects.filter(restaurant=restaurant, is_active=True).exists():
        manques.append("Le catalogue n'a aucune catégorie active.")
    if not MenuItem.objects.filter(restaurant=restaurant, is_available=True).exists():
        manques.append("Le catalogue n'a aucun article disponible.")

    return manques
