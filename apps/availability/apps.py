from __future__ import annotations

from django.apps import AppConfig


class AvailabilityConfig(AppConfig):
    name = "apps.availability"
    label = "availability"
    verbose_name = "Disponibilité"

    def ready(self) -> None:
        """Inscrit le juge auprès de la carte publique.

        Même mécanisme que la complétude d'un établissement
        (`apps.restaurants.readiness`) : l'abonné connaît l'émetteur, et
        `catalog` interroge le juge sans savoir qu'il vit ici — ce qu'il ne
        pourrait pas faire autrement, le juge dépendant de la production, qui
        dépend du catalogue.
        """
        from apps.availability.services import AvailabilityService
        from apps.catalog.availability import register_menu_judge

        register_menu_judge(AvailabilityService.menu)
