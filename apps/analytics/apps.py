from __future__ import annotations

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    name = "apps.analytics"
    label = "analytics"
    verbose_name = "Analytics"

    def ready(self) -> None:
        from apps.analytics import receivers  # noqa: F401
