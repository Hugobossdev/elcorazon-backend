"""Flux personnel d'un compte — `ws/me/`.

Le seul canal du projet qui ne soit pas rattaché à une ressource, et c'est
délibéré : un appel entrant doit faire sonner le destinataire **où qu'il soit**
dans l'app. Un canal par commande ne le permettrait pas — il faudrait que le
destinataire ait deviné laquelle écouter avant qu'on l'appelle.

En lecture seule, comme les autres : décrocher, refuser et raccrocher passent
par l'API HTTP, qui valide la transition. Accepter ces gestes par le socket
obligerait à dupliquer la machine à états dans un second point d'entrée.
"""

from __future__ import annotations

from common.consumers import AuthorizedConsumer
from common.realtime import user_group

__all__ = ["UserFeedConsumer"]


class UserFeedConsumer(AuthorizedConsumer):
    """`ws/me/` — la file du porteur du jeton.

    Aucun identifiant dans l'URL, donc aucun identifiant à falsifier : le
    groupe est déduit du jeton, exactement comme `ws/couriers/me/`.
    """

    async def authorized_group(self) -> str | None:
        return user_group(self.user.pk)
