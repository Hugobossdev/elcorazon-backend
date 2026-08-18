from __future__ import annotations

from django.apps import AppConfig


class CommonConfig(AppConfig):
    name = "common"
    verbose_name = "Socle transverse"

    def ready(self) -> None:
        # Branché ici et non à l'import du module : les modèles ne sont pas
        # tous chargés avant `ready()`, et `apps.get_models()` en rendrait une
        # liste incomplète — donc un branchement silencieusement partiel.
        from common import files

        files.register()
