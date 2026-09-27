"""Réglages communs aux tests WebSocket.

## Pourquoi ce module existe

`WebsocketCommunicator.connect()` attend **une seconde** par défaut. Or ouvrir
un socket ici n'est pas une poignée de main : le consommateur valide un jeton
RS256, charge le compte, puis interroge la commande et la course — trois
allers-retours en base, tous sur l'unique fil d'exécution que
`database_sync_to_async` partage (`thread_sensitive`).

Sur un poste chargé — la suite complète dure vingt minutes, PostgreSQL tourne
dans un conteneur, et huit gigaoctets de mémoire sont partagés avec le reste —
cette seconde est franchie de temps à autre. Le symptôme est un
`asyncio.TimeoutError` dans `connect()`, qui se lit exactement comme un refus
du serveur : c'est la cause de l'instabilité observée sur `TestChat` en suite
complète, jamais en isolation (5/5 verts, plusieurs fois).

Le délai généreux ne masque rien : le test affirme toujours si le socket a été
**accepté ou refusé**, et avec quel code de fermeture. Ce qu'il cesse de
mesurer est la vitesse de la machine, qu'aucun de ces tests ne prétend éprouver.
Un vrai refus reste instantané ; seul le cas où la réponse tarde change d'issue.
"""

from __future__ import annotations

#: Délai laissé à l'établissement d'un socket, en secondes.
DELAI_OUVERTURE = 10
