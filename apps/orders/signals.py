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

__all__ = ["order_created", "order_status_changed"]

#: Arguments : `order`.
#:
#: Une commande **vient d'être créée**, et c'est un événement distinct de tout
#: changement de statut : à la création il n'y a pas de statut précédent, et
#: aucune transition n'a eu lieu.
#:
#: Ce signal manquait, et son absence ouvrait un trou au tout premier maillon de
#: la chaîne. La seule voie automatique vers `confirmed` est l'encaissement
#: (`PaymentService`, webhook du prestataire) — or le règlement **en espèces à
#: la livraison** est aujourd'hui le seul moyen de paiement actif dans
#: l'application cliente. Aucun webhook ne part donc jamais, et une commande
#: passée par un client restait en `pending` sans que rien ne l'annonce : ni
#: notification au personnel — `STAFF_ANNOUNCEMENTS` est indexé sur des
#: transitions — ni événement sur le tableau de bord temps réel, qui ne diffuse
#: que `order.status`. Le repas n'était donc préparé que si quelqu'un
#: rafraîchissait la liste des commandes et remarquait la ligne.
order_created = django.dispatch.Signal()

#: Arguments : `order`, `previous`, `target`, `reason`.
order_status_changed = django.dispatch.Signal()
