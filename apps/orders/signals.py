"""Événements de domaine émis par les commandes — ADR-002.

Le graphe de dépendances est acyclique et `orders` en est presque la racine :
`notifications`, `loyalty`, `gamification` et `analytics` doivent réagir à ce
qui s'y passe **sans que `orders` les connaisse**. Un appel direct inverserait
le sens de la flèche et, à la quatrième app abonnée, reconstituerait le
monolithe enchevêtré que le découpage cherche à éviter.

D'où un signal. `orders` annonce ; qui veut écoute, en s'abonnant depuis son
propre `AppConfig.ready()`. Ajouter un abonné ne modifie pas une ligne ici.

Le signal est émis **dans** la transaction : un abonné qui écrit en base — la
notification en est une — doit le faire de façon atomique avec le changement
qui l'a déclenché. Ce qui sort vers le réseau, lui, est reporté après le commit
par l'abonné lui-même.
"""

from __future__ import annotations

import django.dispatch

__all__ = ["order_status_changed"]

#: Arguments : `order`, `previous`, `target`, `reason`.
order_status_changed = django.dispatch.Signal()
