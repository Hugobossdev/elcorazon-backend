"""Événements de domaine émis par les établissements — ADR-002.

Ouvrir, suspendre ou fermer un établissement change ce que trois applications
affichent — la carte du client, les courses proposées au livreur, le tableau du
back-office — et personne n'était prévenu. Un restaurant suspendu à midi
disparaissait de l'application cliente, et son équipe l'apprenait en constatant
que les commandes ne rentraient plus.

Le mécanisme est celui des commandes, pour les mêmes raisons : `restaurants` est
près de la racine du graphe, et `notifications` doit réagir **sans** que
`restaurants` le connaisse. Un appel direct inverserait la flèche.

Le signal est émis **dans** la transaction de la transition : un abonné qui
écrit en base doit le faire de façon atomique avec le changement qui l'a
déclenché. Ce qui part vers le réseau est reporté après le commit par l'abonné.
"""

from __future__ import annotations

import django.dispatch

__all__ = ["restaurant_status_changed"]

#: Arguments : `restaurant`, `previous`, `target`.
#:
#: Un établissement **vient de changer d'état** : mis en service, suspendu,
#: rouvert, repassé en configuration. La création n'émet rien — un brouillon
#: n'intéresse encore personne, et prévenir d'une fiche vide apprendrait à son
#: auteur ce qu'il vient de faire.
#:
#: `previous` voyage avec la cible parce que le sens du geste est dans la
#: paire : « suspendu → en service » est une réouverture, « brouillon → en
#: service » est une inauguration, et les deux n'appellent pas le même message.
restaurant_status_changed = django.dispatch.Signal()
