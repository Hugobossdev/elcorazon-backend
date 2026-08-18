"""Tests du formateur de journal JSON.

Ces tests existent parce qu'un bug est passé : `_RESERVED` faisait
`vars(record) | {…}`, c'est-à-dire `dict | set`, ce qui lève un `TypeError`.
Les réglages de test surchargent `LOGGING`, donc la suite ne l'exerçait pas —
la panne n'est apparue qu'au premier `manage.py` lancé en configuration de
développement, où elle empêchait purement et simplement le démarrage.

La leçon est générale : un composant que les tests court-circuitent doit être
testé directement, sinon il n'est testé nulle part.
"""

from __future__ import annotations

import json
import logging

import pytest

from config.logging import JSONFormatter


@pytest.fixture
def formatter() -> JSONFormatter:
    return JSONFormatter()


def record(**extra: object) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="apps.orders",
        level=logging.INFO,
        pathname="/app/apps/orders/services.py",
        lineno=42,
        msg="Commande %s confirmée",
        args=("abc",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


class TestConfigurationDuFormateur:
    def test_le_module_s_importe(self) -> None:
        """Le bug d'origine se manifestait à l'import, donc au démarrage."""
        assert isinstance(JSONFormatter(), logging.Formatter)

    def test_la_configuration_django_est_applicable(self) -> None:
        """Reproduit ce que fait `django.setup()` : sans cela, l'application ne
        démarre pas, et aucun test de format n'aurait servi à l'attraper."""
        import logging.config

        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {"json": {"()": "config.logging.JSONFormatter"}},
                "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
                "root": {"handlers": ["console"], "level": "INFO"},
            }
        )


class TestSortie:
    def test_produit_du_json_valide(self, formatter: JSONFormatter) -> None:
        payload = json.loads(formatter.format(record()))

        assert payload["level"] == "INFO"
        assert payload["logger"] == "apps.orders"
        assert payload["message"] == "Commande abc confirmée"
        assert "timestamp" in payload

    def test_les_donnees_metier_sont_conservees(self, formatter: JSONFormatter) -> None:
        """C'est l'intérêt du format : un incident s'interroge par champ, pas
        par expression régulière sur du texte libre."""
        payload = json.loads(
            formatter.format(record(order_id="01931f4e", restaurant="el-corazon-lome"))
        )

        assert payload["order_id"] == "01931f4e"
        assert payload["restaurant"] == "el-corazon-lome"

    def test_les_attributs_internes_sont_ecartes(self, formatter: JSONFormatter) -> None:
        """Sans filtrage, chaque ligne embarquerait `pathname`, `thread`,
        `relativeCreated` et une vingtaine d'autres champs sans valeur."""
        payload = json.loads(formatter.format(record()))

        for interne in ("pathname", "relativeCreated", "msecs", "args", "levelno"):
            assert interne not in payload

    def test_les_accents_ne_sont_pas_echappes(self, formatter: JSONFormatter) -> None:
        """`ensure_ascii=False` : les journaux sont en français et doivent
        rester lisibles tels quels dans le collecteur."""
        assert "confirmée" in formatter.format(record())

    def test_une_exception_est_jointe(self, formatter: JSONFormatter) -> None:
        try:
            raise ValueError("stock insuffisant")
        except ValueError:
            import sys

            rec = record()
            rec.exc_info = sys.exc_info()
            payload = json.loads(formatter.format(rec))

        assert "ValueError: stock insuffisant" in payload["exception"]

    def test_une_valeur_non_serialisable_ne_fait_pas_tomber_le_journal(
        self, formatter: JSONFormatter
    ) -> None:
        """`default=str` : un objet exotique passé en `extra=` doit dégrader la
        lisibilité d'un champ, jamais faire perdre la ligne entière."""
        from decimal import Decimal

        payload = json.loads(formatter.format(record(montant=Decimal("1250.00"))))

        assert payload["montant"] == "1250.00"
