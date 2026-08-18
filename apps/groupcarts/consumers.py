"""Synchronisation temps réel d'un panier collaboratif — ADR-008.

C'est le cas d'usage qui justifie le plus directement un WebSocket dans ce
projet : plusieurs personnes modifient la même chose en même temps, et chacune
doit voir arriver les plats des autres. Sans diffusion, il faudrait interroger
l'API en boucle pendant tout le temps que met un groupe à se décider — c'est-à-
dire longtemps.

L'autorisation porte sur **l'appartenance au panier**, vérifiée avant
l'acceptation du socket. Un identifiant de panier deviné ne suffit donc pas à
écouter le déjeuner d'un autre groupe.
"""

from __future__ import annotations

from channels.db import database_sync_to_async

from apps.groupcarts.models import GroupCart
from common.consumers import AuthorizedConsumer
from common.realtime import group_cart_group

__all__ = ["GroupCartConsumer"]


class GroupCartConsumer(AuthorizedConsumer):
    """Flux d'un panier collaboratif, pour ses participants.

    Lecture seule, comme le suivi de commande : les contributions passent par
    l'API HTTP, qui valide l'article, ses options et l'échéance. Accepter un ajout
    par le socket obligerait à dupliquer ces contrôles dans un second point
    d'entrée — c'est exactement ce que faisait l'ancienne implémentation, où
    l'écriture temps réel n'était validée nulle part.
    """

    async def authorized_group(self) -> str | None:
        identifier = self.scope["url_route"]["kwargs"]["group_cart_id"]
        if not await self._is_member(identifier):
            return None
        return group_cart_group(identifier)

    @database_sync_to_async
    def _is_member(self, group_cart_id: str) -> bool:
        """L'appelant participe-t-il à ce panier ?

        L'hôte n'est pas traité à part : il est membre comme les autres, inscrit à
        l'ouverture. Le distinguer ici aurait fait dépendre l'accès temps réel
        d'une seconde définition de l'appartenance.
        """
        return GroupCart.objects.filter(pk=group_cart_id, members__user=self.user).exists()
