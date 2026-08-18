"""Tableau de bord temps réel du personnel — ADR-008.

Lecture seule : le personnel ne publie rien ici, il observe ce que `orders`
diffuse (`OrderService.transition_to`) sur `restaurant.{id}`. Même principe
d'autorisation que le troisième public de `OrderTrackingConsumer` — le
rattachement à l'établissement fait le droit, pas le type de compte — mais
porté ici sur l'établissement entier plutôt que sur une commande.
"""

from __future__ import annotations

from channels.db import database_sync_to_async

from apps.accounts.models import UserType
from apps.restaurants.models import Restaurant
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from common.consumers import AuthorizedConsumer
from common.realtime import restaurant_group

__all__ = ["RestaurantDashboardConsumer"]


class RestaurantDashboardConsumer(AuthorizedConsumer):
    """`ws/restaurants/{restaurant_id}/dashboard/`"""

    restaurant_id: str

    async def authorized_group(self) -> str | None:
        self.restaurant_id = str(self.scope["url_route"]["kwargs"]["restaurant_id"])
        allowed = await self._check_access()
        return restaurant_group(self.restaurant_id) if allowed else None

    @database_sync_to_async
    def _check_access(self) -> bool:
        """Personnel rattaché à l'établissement, avec le droit de lire ses
        commandes — le même couple (type de compte, permission, rattachement)
        qu'ailleurs dans l'API (ADR-005).

        Un établissement inexistant ou un compte non rattaché ferment le
        socket de la même façon : la ressource n'a pas à être distinguée d'un
        refus, sous peine de révéler son existence par le code de fermeture.
        """
        if self.user.user_type != UserType.STAFF or not self.user.has_permission("orders.read"):
            return False

        if not Restaurant.objects.filter(pk=self.restaurant_id).exists():
            return False

        return is_unscoped(self.user) or self.restaurant_id in {
            str(pk) for pk in staff_restaurant_ids(self.user)
        }
