from __future__ import annotations

from django.apps import AppConfig


class RestaurantsConfig(AppConfig):
    name = "apps.restaurants"
    label = "restaurants"
    verbose_name = "Établissements"

    def ready(self) -> None:
        """Branche les abonnements — voir `receivers`.

        Ici et nulle part ailleurs, comme pour `notifications` : importé plus
        tôt, le module chargerait les modèles d'`accounts` avant que le
        registre des applications soit prêt.
        """
        from apps.restaurants import receivers  # noqa: F401
