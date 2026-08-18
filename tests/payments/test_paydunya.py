"""Connecteur PayDunya — testé hors réseau.

Aucun compte marchand n'est configuré sur ce poste, et il ne faudrait pas qu'il
le soit : une suite qui appellerait un service externe serait lente, instable,
et rouge les jours où le prestataire l'est. Le transport HTTP est donc simulé,
ce qui permet de vérifier ce qui nous appartient réellement — la traduction
dans un sens puis dans l'autre.

Ce que ces tests **ne prouvent pas** : que PayDunya nomme bien ses champs comme
écrit ici. Cela demande une facture réelle en mode `test`, et c'est le premier
geste avant la mise en service.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from apps.payments.gateway import GatewayError, Notification
from apps.payments.models import Order, PaymentProvider, PaymentStatus, Transaction
from apps.payments.paydunya import PayDunyaGateway
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
MASTER_KEY = "cle-maitresse-de-test"


@pytest.fixture
def configure(settings: Any) -> Any:
    settings.PAYDUNYA_MODE = "test"
    settings.PAYDUNYA_MASTER_KEY = MASTER_KEY
    settings.PAYDUNYA_PRIVATE_KEY = "cle-privee"
    settings.PAYDUNYA_TOKEN = "jeton"
    settings.PAYDUNYA_CALLBACK_URL = "https://api.elcorazon.app/api/v1/payments/webhook/paydunya/"
    return settings


@pytest.fixture
def transaction(order: Order) -> Transaction:
    return Transaction(
        order=order,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference="",
        amount=Money(4_000, XOF),
    )


def gateway_repondant(handler: Any, monkeypatch: pytest.MonkeyPatch) -> PayDunyaGateway:
    """Connecteur dont le client HTTP est remplacé par un transport simulé."""
    vrai_client = httpx.Client

    def client_simule(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.pop("timeout", None)
        return vrai_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_simule)
    return PayDunyaGateway()


class TestCreationDeFacture:
    def test_la_facture_rend_un_jeton_et_une_url(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/checkout-invoice/create")
            assert request.headers["PAYDUNYA-MASTER-KEY"] == MASTER_KEY
            return httpx.Response(
                200,
                json={
                    "response_code": "00",
                    "response_text": "Facture créée",
                    "token": "PD-TOKEN-123",
                    "invoice_url": "https://paydunya.com/checkout/PD-TOKEN-123",
                },
            )

        instruction = gateway_repondant(handler, monkeypatch).open_checkout(transaction)

        assert instruction.provider_reference == "PD-TOKEN-123"
        assert instruction.checkout_url.startswith("https://")

    def test_le_montant_part_en_unite_mineure(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """4 000 F CFA s'écrit 4000 : le XOF n'a pas de décimale, et convertir
        ici introduirait le flottant que toute la chaîne exclut."""
        vus: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            vus.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"response_code": "00", "token": "T", "invoice_url": "https://x/y"},
            )

        gateway_repondant(handler, monkeypatch).open_checkout(transaction)

        assert vus["invoice"]["total_amount"] == 4000

    def test_notre_identifiant_voyage_avec_la_facture(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second chemin de rapprochement si le jeton venait à manquer dans la
        notification."""
        vus: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            vus.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"response_code": "00", "token": "T", "invoice_url": "https://x/y"},
            )

        gateway_repondant(handler, monkeypatch).open_checkout(transaction)

        assert vus["custom_data"]["transaction"] == str(transaction.pk)

    def test_un_refus_metier_arrive_en_200_et_doit_etre_lu(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PayDunya répond 200 même sur un rejet : se fier au statut HTTP
        ferait prendre un refus pour une facture ouverte, et le client
        attendrait devant une URL vide."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"response_code": "1001", "response_text": "Clés invalides"}
            )

        with pytest.raises(GatewayError, match="Clés invalides"):
            gateway_repondant(handler, monkeypatch).open_checkout(transaction)

    def test_une_reponse_incomplete_est_refusee(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sans jeton, aucune notification ne pourra être rapprochée : mieux
        vaut échouer ici que créer une transaction orpheline."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response_code": "00", "token": ""})

        with pytest.raises(GatewayError, match="incomplète"):
            gateway_repondant(handler, monkeypatch).open_checkout(transaction)

    def test_un_reseau_coupe_ne_ressemble_pas_a_une_faute_du_client(
        self, configure: Any, transaction: Transaction, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("injoignable")

        with pytest.raises(GatewayError, match="injoignable"):
            gateway_repondant(handler, monkeypatch).open_checkout(transaction)

    def test_le_mode_test_ne_vise_pas_la_production(self, configure: Any) -> None:
        """La seule variable dont une erreur se paie en argent réel."""
        assert "sandbox" in PayDunyaGateway()._base_url

        configure.PAYDUNYA_MODE = "live"
        assert "sandbox" not in PayDunyaGateway()._base_url


class TestAuthentificationDesNotifications:
    def test_l_empreinte_de_la_cle_maitresse_authentifie(self, configure: Any) -> None:
        empreinte = hashlib.sha512(MASTER_KEY.encode()).hexdigest()

        assert PayDunyaGateway().authenticate(
            raw_body=b"", headers={}, data={"data": {"hash": empreinte}}
        )

    def test_une_empreinte_fausse_est_rejetee(self, configure: Any) -> None:
        assert not PayDunyaGateway().authenticate(
            raw_body=b"", headers={}, data={"data": {"hash": "0" * 128}}
        )

    def test_sans_cle_configuree_rien_ne_passe(self, configure: Any) -> None:
        """Une configuration oubliée doit fermer la porte, pas l'ouvrir."""
        configure.PAYDUNYA_MASTER_KEY = ""

        assert not PayDunyaGateway().authenticate(
            raw_body=b"", headers={}, data={"data": {"hash": "peu importe"}}
        )


def confirmation_repondant(statut: str, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> Any:
    """Simule la réponse de `GET checkout-invoice/confirm/{token}`.

    C'est PayDunya, et non le corps posté, qui fixe désormais le statut
    appliqué : chaque test de lecture doit donc dire ce que *PayDunya*
    répondrait, pas seulement ce que la notification prétend.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/checkout-invoice/confirm/" in request.url.path
        return httpx.Response(200, json={"response_code": "00", "status": statut, **extra})

    return gateway_repondant(handler, monkeypatch)


class TestLectureDesNotifications:
    def test_un_encaissement_est_traduit(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connecteur = confirmation_repondant("completed", monkeypatch)

        notification = connecteur.parse({"data": {"invoice": {"token": "PD-1"}}})

        assert notification == Notification(
            event_id="PD-1:completed",
            provider_reference="PD-1",
            status=PaymentStatus.COMPLETED,
            reason="",
        )

    def test_le_statut_pose_par_le_corps_est_ignore(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le correctif de la faille : un corps qui prétend « payé » ne suffit
        plus. Seule la réponse de PayDunya à `_confirm` décide — ici « en
        attente » — même si la notification posée réclame « completed »."""
        connecteur = confirmation_repondant("pending", monkeypatch)

        notification = connecteur.parse(
            {"data": {"status": "completed", "invoice": {"token": "PD-1"}}}
        )

        assert notification.status == PaymentStatus.PROCESSING

    def test_l_identifiant_d_evenement_distingue_les_etapes(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PayDunya notifie plusieurs fois la même facture au fil de sa
        progression. La référence seule ferait ignorer l'encaissement au motif
        qu'on a déjà vu passer l'attente."""
        statuts = iter(["pending", "completed"])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response_code": "00", "status": next(statuts)})

        connecteur = gateway_repondant(handler, monkeypatch)

        attente = connecteur.parse({"data": {"invoice": {"token": "PD-2"}}})
        encaisse = connecteur.parse({"data": {"invoice": {"token": "PD-2"}}})

        assert attente.event_id != encaisse.event_id

    def test_un_abandon_devient_un_echec_motive(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La machine n'autorise pas `processing → cancelled` : l'abandon est
        enregistré en échec, avec un motif qui dit ce qui s'est passé."""
        connecteur = confirmation_repondant("cancelled", monkeypatch)

        notification = connecteur.parse({"data": {"invoice": {"token": "PD-3"}}})

        assert notification.status == PaymentStatus.FAILED
        assert "abandonné" in notification.reason.lower()

    def test_la_forme_a_plat_est_acceptee(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un formulaire encodé à plat dit la même chose qu'un imbriqué :
        refuser la seconde forme ferait échouer l'intégration sur un détail de
        sérialisation qu'on ne maîtrise pas."""
        connecteur = confirmation_repondant("completed", monkeypatch)

        notification = connecteur.parse({"data": {"invoice[token]": "PD-4"}})

        assert notification.provider_reference == "PD-4"

    def test_notre_identifiant_sert_de_repli(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connecteur = confirmation_repondant("completed", monkeypatch)

        notification = connecteur.parse({"data": {"custom_data": {"transaction": "abc-123"}}})

        assert notification.provider_reference == "abc-123"

    def test_un_statut_inconnu_est_signale(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le traduire au jugé écrirait un statut faux sur une transaction
        réelle ; le refuser fait remonter une intégration à revoir."""
        connecteur = confirmation_repondant("quelque_chose", monkeypatch)

        with pytest.raises(GatewayError, match="inconnu"):
            connecteur.parse({"data": {"invoice": {"token": "PD-1"}}})

    def test_sans_jeton_rien_n_est_confirmable(self, configure: Any) -> None:
        """Sans jeton, il n'y a rien à demander à PayDunya : mieux vaut
        échouer ici qu'interroger une facture au hasard."""
        with pytest.raises(GatewayError, match="jeton"):
            PayDunyaGateway().parse({"data": {"status": "completed"}})

    def test_une_confirmation_refusee_est_signalee(
        self, configure: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PayDunya peut répondre qu'il ne connaît pas ce jeton : ce n'est pas
        une transaction à appliquer au jugé."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"response_code": "1001", "response_text": "Jeton inconnu"}
            )

        connecteur = gateway_repondant(handler, monkeypatch)

        with pytest.raises(GatewayError, match="Jeton inconnu"):
            connecteur.parse({"data": {"invoice": {"token": "PD-INCONNU"}}})


class TestBoutEnBout:
    def test_une_notification_paydunya_encaisse_la_commande(
        self,
        configure: Any,
        order: Order,
        customer: Any,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Le chemin complet, sans réseau réel : la transaction existe,
        PayDunya confirme un encaissement, la commande passe en confirmée."""
        from django.urls import reverse
        from rest_framework.test import APIClient

        from apps.orders.states import OrderStatus

        settings.PAYMENT_GATEWAYS = {
            **settings.PAYMENT_GATEWAYS,
            "paydunya": "apps.payments.paydunya.PayDunyaGateway",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/checkout-invoice/confirm/PD-BOUT")
            return httpx.Response(200, json={"response_code": "00", "status": "completed"})

        gateway_repondant(handler, monkeypatch)  # remplace httpx.Client pour tout le module

        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-BOUT",
            amount=order.total,
            status=PaymentStatus.PROCESSING,
        )

        reponse = APIClient().post(
            reverse("v1:payments:webhook", args=[PaymentProvider.PAYDUNYA]),
            data={
                "data": {
                    "hash": hashlib.sha512(MASTER_KEY.encode()).hexdigest(),
                    "invoice": {"token": "PD-BOUT"},
                }
            },
            format="json",
        )

        assert reponse.status_code == 200
        txn.refresh_from_db()
        order.refresh_from_db()
        assert txn.status == PaymentStatus.COMPLETED
        assert order.status == OrderStatus.CONFIRMED

    def test_un_statut_forge_dans_le_corps_n_encaisse_pas(
        self,
        configure: Any,
        order: Order,
        customer: Any,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """La faille corrigée, rejouée de bout en bout : l'empreinte de
        PayDunya est constante — connue de quiconque a vu passer une seule
        notification légitime, y compris son propre paiement. Un corps forgé
        peut donc porter la bonne empreinte tout en réclamant un statut faux.
        Avant le correctif, ceci encaissait la commande. Désormais, seule la
        réponse de PayDunya à `_confirm` décide — ici « en attente » — et la
        commande ne bouge pas."""
        from django.urls import reverse
        from rest_framework.test import APIClient

        from apps.orders.states import OrderStatus

        settings.PAYMENT_GATEWAYS = {
            **settings.PAYMENT_GATEWAYS,
            "paydunya": "apps.payments.paydunya.PayDunyaGateway",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            # PayDunya lui-même n'a jamais vu ce paiement aboutir.
            return httpx.Response(200, json={"response_code": "00", "status": "pending"})

        gateway_repondant(handler, monkeypatch)

        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-FORGE",
            amount=order.total,
            status=PaymentStatus.PROCESSING,
        )

        reponse = APIClient().post(
            reverse("v1:payments:webhook", args=[PaymentProvider.PAYDUNYA]),
            data={
                "data": {
                    # Empreinte authentique — un attaquant l'a vue passer une
                    # fois — mais un statut et un jeton de son choix.
                    "hash": hashlib.sha512(MASTER_KEY.encode()).hexdigest(),
                    "status": "completed",
                    "invoice": {"token": "PD-FORGE"},
                }
            },
            format="json",
        )

        assert reponse.status_code == 200
        txn.refresh_from_db()
        order.refresh_from_db()
        assert txn.status == PaymentStatus.PROCESSING
        assert order.status != OrderStatus.CONFIRMED
