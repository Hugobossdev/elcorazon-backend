"""`UnavailableGateway` — le refus qui remplace `SandboxGateway` en production.

`config.settings.prod` redirige `PAYMENT_GATEWAYS["paydunya"]` vers ce
connecteur quand `PAYDUNYA_GATEWAY` n'est pas configuré, plutôt que de laisser
tourner tout le service sur `SandboxGateway` (voir
`tests/config/test_reglages_prod.py::TestConnecteurDePaiement`). Ce qui compte
alors est qu'il refuse effectivement tout, sans jamais authentifier une
notification qu'aucun secret ne protège.
"""

from __future__ import annotations

import pytest

from apps.payments.gateway import GatewayError, UnavailableGateway


class TestConnecteurIndisponible:
    def test_ouvrir_un_paiement_est_refuse(self) -> None:
        with pytest.raises(GatewayError):
            # `transaction` n'est jamais lu : le refus est immédiat, quel que
            # soit l'appelant.
            UnavailableGateway().open_checkout(None)  # type: ignore[arg-type]

    def test_lire_une_notification_est_refuse(self) -> None:
        with pytest.raises(GatewayError):
            UnavailableGateway().parse({"status": "completed"})

    def test_aucune_notification_n_est_authentifiee(self) -> None:
        """Sans configuration réelle, rien ne peut prouver une notification.

        Rendre `True` ici referait exactement la faille d'origine : un corps
        forgé, accepté faute de secret à vérifier.
        """
        assert (
            UnavailableGateway().authenticate(raw_body=b"{}", headers={}, data={}) is False
        )
