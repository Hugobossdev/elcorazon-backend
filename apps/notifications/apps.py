from __future__ import annotations

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        """Branche les abonnements aux événements de domaine.

        C'est le seul endroit où ils peuvent l'être : importer `receivers`
        ailleurs — depuis `models`, par exemple — le ferait charger avant que
        le registre des applications soit prêt, et l'import des modèles des
        apps émettrices échouerait.
        """
        from apps.notifications import receivers  # noqa: F401
