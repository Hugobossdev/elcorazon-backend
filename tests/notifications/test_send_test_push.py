"""Commande `send_test_push` — voir `apps/notifications/fcm.py`.

Ce test ne prouve pas qu'un vrai projet Firebase répond correctement : cela
demande des credentials réels, absents de ce dépôt. Il prouve seulement que la
commande relaie fidèlement ce que `PUSH_BACKEND` lui rend, sur les trois issues
possibles d'un envoi.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.notifications.push import PushMessage, PushResult


class FakeBackend:
    """Simule les trois issues d'un envoi sans appel réseau."""

    résultat: PushResult = PushResult()

    def send(self, tokens: list[str], message: PushMessage) -> PushResult:
        return self.résultat


@pytest.fixture(autouse=True)
def configure_backend(settings: object) -> None:
    settings.PUSH_BACKEND = "tests.notifications.test_send_test_push.FakeBackend"  # type: ignore[attr-defined]


def run() -> str:
    out = StringIO()
    call_command("send_test_push", "jeton-de-test", stdout=out)
    return out.getvalue()


class TestSendTestPush:
    def test_livraison_signalee(self) -> None:
        FakeBackend.résultat = PushResult(delivered=("jeton-de-test",))

        assert "Livré" in run()

    def test_appareil_injoignable_signale(self) -> None:
        FakeBackend.résultat = PushResult(unregistered=("jeton-de-test",))

        assert "injoignable" in run()

    def test_echec_passager_signale(self) -> None:
        FakeBackend.résultat = PushResult(failed=("jeton-de-test",))

        assert "chec" in run()  # « Échec » — insensible à l'encodage de la console
