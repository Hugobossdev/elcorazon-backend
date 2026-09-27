"""Connecteur FCM — testé hors réseau.

Ce qui compte dans ce connecteur n'est pas l'envoi, qui est un POST. C'est la
**classification des erreurs** : distinguer l'appareil définitivement
injoignable de la panne passagère. Confondre les deux donne soit une purge
d'appareils sains au premier hoquet, soit la boucle infinie de
l'implémentation précédente, qui retentait un téléphone désinstallé à chaque
notification.

Ces tests ne prouvent pas que Google renvoie bien ces codes-là ; ils prouvent
que, s'il les renvoie, nous en tirons la bonne conséquence. La confrontation au
service réel a été faite séparément, le 5 août 2026, contre le projet
`elcorazon-9595` : `INVALID_ARGUMENT` (400) et `UNREGISTERED` (404) sont les
codes effectivement reçus, et tous deux figurent bien dans
`ERREURS_DEFINITIVES`. Voir `docs/firebase.md` §5.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from apps.notifications.fcm import (
    ERREURS_DEFINITIVES,
    FirebaseCloudMessagingBackend,
    _TransportOAuth,
)
from apps.notifications.push import PushMessage

MESSAGE = PushMessage(title="Livrée", body="Bon appétit !", data={"order": "abc"})


@pytest.fixture
def configure(settings: Any) -> Any:
    settings.FCM_PROJECT_ID = "el-corazon-test"
    settings.FCM_CREDENTIALS_PATH = "/run/secrets/fcm.json"
    return settings


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, configure: Any) -> FirebaseCloudMessagingBackend:
    """Connecteur dont l'authentification est court-circuitée.

    Le jeton OAuth demande un compte de service réel et un aller-retour vers
    Google : le simuler ici laisse le test porter sur ce qui nous appartient.
    """
    monkeypatch.setattr(
        FirebaseCloudMessagingBackend,
        "_authorization",
        lambda self: {"Authorization": "Bearer jeton-de-test"},
    )
    return FirebaseCloudMessagingBackend()


def transport(handler: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    vrai_client = httpx.Client

    def client_simule(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.pop("timeout", None)
        return vrai_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_simule)


def erreur(code: str, statut: int = 400) -> httpx.Response:
    return httpx.Response(
        statut,
        json={"error": {"status": "INVALID_ARGUMENT", "details": [{"errorCode": code}]}},
    )


class TestEnvoi:
    def test_un_appareil_servi_est_compte_comme_livre(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport(
            lambda request: httpx.Response(200, json={"name": "projects/x/messages/1"}), monkeypatch
        )

        resultat = backend.send(["jeton-a"], MESSAGE)

        assert resultat.delivered == ("jeton-a",)
        assert resultat.unregistered == ()
        assert resultat.failed == ()

    def test_l_envoi_est_unitaire(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L'API v1 n'a pas de diffusion groupée : un client à trois téléphones
        coûte trois appels. C'est pourquoi tout cela vit dans une tâche."""
        appels: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            appels.append(json.loads(request.content)["message"]["token"])
            return httpx.Response(200, json={})

        transport(handler, monkeypatch)

        backend.send(["a", "b", "c"], MESSAGE)

        assert appels == ["a", "b", "c"]

    def test_le_message_porte_titre_corps_et_donnees(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vus: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            vus.update(json.loads(request.content)["message"])
            return httpx.Response(200, json={})

        transport(handler, monkeypatch)

        backend.send(["jeton"], MESSAGE)

        assert vus["notification"] == {"title": "Livrée", "body": "Bon appétit !"}
        assert vus["data"] == {"order": "abc"}

    def test_la_priorite_haute_est_demandee(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sans elle, une notification de commande arrive quand le système le
        décide — parfois après la livraison."""
        vus: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            vus.update(json.loads(request.content)["message"])
            return httpx.Response(200, json={})

        transport(handler, monkeypatch)

        backend.send(["jeton"], MESSAGE)

        assert vus["android"]["priority"] == "high"
        assert vus["apns"]["headers"]["apns-priority"] == "10"


class TestClassementDesErreurs:
    """Le cœur du connecteur."""

    @pytest.mark.parametrize("code", sorted(ERREURS_DEFINITIVES))
    def test_un_appareil_definitivement_injoignable_est_signale(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        transport(lambda request: erreur(code), monkeypatch)

        resultat = backend.send(["mort"], MESSAGE)

        assert resultat.unregistered == ("mort",)
        assert resultat.failed == ()

    @pytest.mark.parametrize("code", ["QUOTA_EXCEEDED", "UNAVAILABLE", "INTERNAL"])
    def test_une_panne_passagere_n_efface_pas_l_appareil(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        """Purger sur un quota dépassé effacerait des appareils parfaitement
        sains, et le client cesserait de recevoir sans que rien ne l'explique."""
        transport(lambda request: erreur(code, statut=429), monkeypatch)

        resultat = backend.send(["vivant"], MESSAGE)

        assert resultat.failed == ("vivant",)
        assert resultat.unregistered == ()

    def test_le_statut_http_ne_decide_pas_seul(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un 400 peut signaler un jeton mort comme une charge utile mal
        formée. Purger sur le second effacerait des appareils sains à cause
        d'un défaut de notre côté."""
        transport(
            lambda request: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}),
            monkeypatch,
        )

        resultat = backend.send(["jeton"], MESSAGE)

        assert resultat.failed == ("jeton",)

    def test_un_corps_illisible_ne_fait_rien_supprimer(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport(lambda request: httpx.Response(500, content=b"<html>erreur</html>"), monkeypatch)

        resultat = backend.send(["jeton"], MESSAGE)

        assert resultat.failed == ("jeton",)

    def test_un_reseau_coupe_est_passager(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("injoignable")

        transport(handler, monkeypatch)

        assert backend.send(["jeton"], MESSAGE).failed == ("jeton",)

    def test_un_echec_n_interrompt_pas_les_autres(
        self, backend: FirebaseCloudMessagingBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Les trois listes sont indépendantes : c'est ce qui permet à la tâche
        de supprimer les morts, reprendre les indécis et laisser tranquilles
        ceux qui ont reçu."""

        def handler(request: httpx.Request) -> httpx.Response:
            token = json.loads(request.content)["message"]["token"]
            if token == "mort":
                return erreur("UNREGISTERED")
            if token == "rate":
                return erreur("UNAVAILABLE", statut=503)
            return httpx.Response(200, json={})

        transport(handler, monkeypatch)

        resultat = backend.send(["vivant", "mort", "rate"], MESSAGE)

        assert resultat.delivered == ("vivant",)
        assert resultat.unregistered == ("mort",)
        assert resultat.failed == ("rate",)


class TestJournalisation:
    """Un refus doit être lisible.

    C'est la seule trace de ce que Google a répondu : sans elle, un
    `FCM_PROJECT_ID` erroné et une coupure réseau se présentent tous deux comme
    une suite d'échecs muets. C'est aussi le code qu'on compare à
    `ERREURS_DEFINITIVES` à la validation d'avant mise en service.
    """

    def test_le_code_de_refus_est_journalise(
        self,
        backend: FirebaseCloudMessagingBackend,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transport(lambda request: erreur("SENDER_ID_MISMATCH", statut=403), monkeypatch)

        with caplog.at_level(logging.WARNING, logger="apps.notifications.fcm"):
            backend.send(["jeton-de-test"], MESSAGE)

        (rejet,) = [record for record in caplog.records if record.message == "fcm.rejet"]
        assert rejet.code == "SENDER_ID_MISMATCH"  # type: ignore[attr-defined]
        assert rejet.definitif is True  # type: ignore[attr-defined]
        assert rejet.status == 403  # type: ignore[attr-defined]

    def test_une_panne_passagere_porte_aussi_son_code(
        self,
        backend: FirebaseCloudMessagingBackend,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Un quota dépassé et un compte de service sans droit d'envoi mènent
        à la même reprise : seul le code les distingue."""
        transport(lambda request: erreur("QUOTA_EXCEEDED", statut=429), monkeypatch)

        with caplog.at_level(logging.WARNING, logger="apps.notifications.fcm"):
            backend.send(["jeton-de-test"], MESSAGE)

        (rejet,) = [record for record in caplog.records if record.message == "fcm.rejet"]
        assert rejet.code == "QUOTA_EXCEEDED"  # type: ignore[attr-defined]
        assert rejet.definitif is False  # type: ignore[attr-defined]

    def test_le_jeton_entier_ne_part_pas_dans_le_journal(
        self,
        backend: FirebaseCloudMessagingBackend,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Le journal part chez un collecteur ; un jeton d'appareil complet y
        serait un identifiant durable de plus, pour rien — huit caractères
        suffisent à reconnaître l'appareil dans une rafale."""
        transport(lambda request: erreur("UNREGISTERED"), monkeypatch)
        jeton = "jeton-tres-long-et-reconnaissable-0123456789"

        with caplog.at_level(logging.WARNING, logger="apps.notifications.fcm"):
            backend.send([jeton], MESSAGE)

        (rejet,) = [record for record in caplog.records if record.message == "fcm.rejet"]
        assert rejet.device == "23456789"  # type: ignore[attr-defined]
        assert jeton not in caplog.text


class TestAuthentification:
    def test_sans_identifiants_aucun_appareil_n_est_purge(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La panne est de notre côté, pas du leur : les compter comme morts
        viderait la table des appareils sur une erreur de configuration.
        """
        configure.FCM_CREDENTIALS_PATH = ""

        resultat = FirebaseCloudMessagingBackend().send(["a", "b"], MESSAGE)

        assert resultat.failed == ("a", "b")
        assert resultat.unregistered == ()
        assert resultat.delivered == ()

    def test_le_rafraichissement_du_jeton_aboutit(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le chemin qu'aucun test n'empruntait — et où tout se cassait.

        Les autres tests court-circuitent `_authorization`, ce qui laissait
        passer une `ImportError` levée à chaque rafraîchissement : le connecteur
        importait `google.auth.transport.requests`, donc le paquet `requests`,
        absent des dépendances. `send()` attrapait l'erreur, journalisait
        `fcm.authentification` et rendait tous les appareils en échec passager.
        Aucune notification ne partait, et rien ne le disait à l'appelant.

        Ce test parcourt le vrai chemin : identifiants expirés, rafraîchissement,
        en-tête produit.
        """

        class _Identifiants:
            def __init__(self) -> None:
                self.valid = False
                self.token = ""
                self.transport: Any = None

            def refresh(self, request: Any) -> None:
                self.transport = request
                self.valid = True
                self.token = "jeton-rafraichi"

        identifiants = _Identifiants()
        monkeypatch.setattr("apps.notifications.fcm._credentials", lambda: identifiants)

        entete = FirebaseCloudMessagingBackend()._authorization()

        assert entete["Authorization"] == "Bearer jeton-rafraichi"
        assert entete["Content-Type"] == "application/json"
        # Le transport remis à google-auth doit être le nôtre, celui bâti sur
        # httpx : c'est ce qui évite de dépendre de `requests`.
        assert isinstance(identifiants.transport, _TransportOAuth)


class TestTransportOAuth:
    """L'adaptateur remis à `google-auth` — testé sans réseau.

    `google-auth` attend un appelable rendant un objet à trois propriétés :
    `status`, `headers`, `data`. S'il en manque une, l'échange OAuth échoue au
    premier rafraîchissement, c'est-à-dire en production et jamais en test.
    """

    def test_rend_statut_entetes_et_corps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(requete: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "x"}, headers={"X-Test": "oui"})

        transport(handler, monkeypatch)

        reponse = _TransportOAuth()("https://oauth2.googleapis.com/token", method="POST")

        assert reponse.status == 200
        assert reponse.headers["X-Test"] == "oui"
        assert json.loads(reponse.data)["access_token"] == "x"

    def test_transmet_la_methode_le_corps_et_les_entetes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vues: dict[str, Any] = {}

        def handler(requete: httpx.Request) -> httpx.Response:
            vues["method"] = requete.method
            vues["content"] = requete.content
            vues["content_type"] = requete.headers.get("content-type")
            return httpx.Response(200, json={})

        transport(handler, monkeypatch)

        _TransportOAuth()(
            "https://oauth2.googleapis.com/token",
            method="POST",
            body=b"grant_type=refresh",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        # L'échange OAuth est un POST formulaire : perdre le corps ou l'en-tête
        # rendrait un `invalid_request` que rien dans nos journaux n'expliquerait.
        assert vues["method"] == "POST"
        assert vues["content"] == b"grant_type=refresh"
        assert vues["content_type"] == "application/x-www-form-urlencoded"

    def test_sans_entetes_l_appel_reste_valide(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport(lambda requete: httpx.Response(200, json={}), monkeypatch)

        assert _TransportOAuth()("https://oauth2.googleapis.com/token").status == 200
