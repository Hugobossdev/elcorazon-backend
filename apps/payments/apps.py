from __future__ import annotations

from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "apps.payments"
    label = "payments"

    def ready(self) -> None:
        from apps.payments import receivers  # noqa: F401 - encaissement des espèces à la remise
