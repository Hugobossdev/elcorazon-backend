from __future__ import annotations

from django.apps import AppConfig


class GamificationConfig(AppConfig):
    name = "apps.gamification"
    label = "gamification"
    verbose_name = "Gamification"

    def ready(self) -> None:
        from apps.gamification import receivers  # noqa: F401
