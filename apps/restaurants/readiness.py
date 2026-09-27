"""Complétude d'un établissement avant sa mise en service — ADR-002, ADR-006.

Ouvrir au public suppose plus qu'une fiche remplie : il faut une carte, des
horaires, une flotte. Ces pièces vivent dans trois applications différentes, et
`restaurants` n'a le droit d'en connaître aucune — son graphe autorisé s'arrête
à `accounts` et `geography`.

Deux façons de contourner cela ont été écartées.

* **Lire par relation inverse** (`self.items`, `self.couriers`). C'est un
  couplage réel que l'analyse d'imports ne voit pas, et qui casse en silence le
  jour où un `related_name` change. Le projet a déjà tranché contre ce procédé,
  au même endroit et pour la même raison — voir le commentaire de
  `notifications` dans `tests/architecture/test_dependency_graph.py`.
* **Déplacer la vérification** dans une application qui voit tout. Aucune ne le
  fait sans que le geste devienne incompréhensible : provisionner un
  établissement depuis `search` ou `orders` n'a pas de sens pour qui relit.

D'où ce registre. `catalog` et `delivery` **s'abonnent** au moment du `ready()`
de leur application, exactement comme `loyalty` et `notifications` s'abonnent
aux commandes livrées : l'arête va de l'abonné vers l'émetteur, elle est
déclarée dans le graphe (`catalog → restaurants`, `delivery → restaurants`), et
elle est donc vérifiée en CI.

Un contrôle absent — application retirée du `INSTALLED_APPS`, `ready()` non
appelé — se traduit par une exigence en moins, jamais par une erreur. C'est le
bon sens de défaillance ici : le registre sert à empêcher d'ouvrir un
restaurant vide, pas à protéger une ressource.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - uniquement pour l'annotation
    from apps.restaurants.models import Restaurant

__all__ = ["ReadinessCheck", "gaps_from_registry", "register_readiness_check"]

#: Un contrôle rend les phrases décrivant ce qui manque, ou une liste vide.
ReadinessCheck = Callable[["Restaurant"], list[str]]

_CHECKS: list[ReadinessCheck] = []


def register_readiness_check(check: ReadinessCheck) -> ReadinessCheck:
    """Abonne un contrôle de complétude, depuis le `ready()` d'une application.

    Idempotent par identité de fonction : `ready()` peut être appelé deux fois
    dans une même session de test, et le même contrôle inscrit deux fois
    afficherait deux fois la même phrase manquante.
    """
    if check not in _CHECKS:
        _CHECKS.append(check)
    return check


def gaps_from_registry(restaurant: Restaurant) -> list[str]:
    """Ce que les applications abonnées trouvent d'incomplet."""
    return [phrase for check in _CHECKS for phrase in check(restaurant)]
