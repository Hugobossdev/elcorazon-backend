from __future__ import annotations

from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    name = "apps.loyalty"
    label = "loyalty"
    verbose_name = "Fidélité"

    def ready(self) -> None:
        """Branche l'abonnement aux commandes livrées.

        Même mécanisme que `notifications` : `orders` annonce, `loyalty`
        écoute, et aucun des deux ne connaît l'autre dans le mauvais sens.
        """
        from apps.loyalty import receivers  # noqa: F401
