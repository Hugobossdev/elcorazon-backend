"""File de courses du livreur — ADR-008.

C'est le seul flux où **rater un événement a un coût métier direct** : une
course non vue est une course non prise, donc un repas qui refroidit. Le
WebSocket en est la voie rapide ; la notification push, qui passe application
fermée, viendra la doubler — un livreur n'a pas son téléphone au premier plan
en roulant.
"""

from __future__ import annotations

from channels.db import database_sync_to_async

from apps.accounts.models import UserType
from apps.delivery.models import CourierProfile
from common.consumers import AuthorizedConsumer
from common.realtime import courier_group

__all__ = ["CourierFeedConsumer"]


class CourierFeedConsumer(AuthorizedConsumer):
    """`ws/couriers/me/` — les courses proposées à l'appelant.

    La route ne porte **aucun identifiant** : le groupe est déduit du jeton.
    Un livreur ne peut donc pas écouter la file d'un collègue, même en
    connaissant son identifiant — il n'y a pas de paramètre où le glisser.
    """

    async def authorized_group(self) -> str | None:
        if self.user.user_type != UserType.COURIER:
            return None

        courier_id = await self._own_courier_id()
        return courier_group(courier_id) if courier_id else None

    @database_sync_to_async
    def _own_courier_id(self) -> str:
        """Le dossier doit exister **et** être en état de recevoir des courses.

        Ouvrir la file à un dossier suspendu entretiendrait une connexion que
        rien n'alimenterait — et laisserait croire au livreur qu'il est
        joignable alors qu'aucune course ne lui sera proposée (L1).
        """
        courier = CourierProfile.objects.filter(user=self.user).select_related("user").first()
        if courier is None or not courier.can_accept_orders:
            return ""
        return str(courier.pk)
