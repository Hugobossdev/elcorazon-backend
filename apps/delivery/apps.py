from __future__ import annotations

from django.apps import AppConfig


class DeliveryConfig(AppConfig):
    name = "apps.delivery"
    label = "delivery"

    def ready(self) -> None:
        """Abonne la flotte à la vérification de complétude d'un établissement.

        Sans cet abonnement, un établissement s'ouvrirait sans personne pour
        livrer : les commandes seraient prises, jamais retirées.
        """
        from apps.delivery import dispatch  # noqa: F401 - abonne l'affectation automatique
        from apps.delivery.readiness import fleet_gaps
        from apps.restaurants.readiness import register_readiness_check

        register_readiness_check(fleet_gaps)
