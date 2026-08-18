"""API du paiement — invariants C5, P1, P3.

Cette suite est écrite comme une suite d'attaques. Chacune reproduit ce que
l'implémentation précédente laissait passer :

* se déclarer payé sans encaissement (P2, fermé par la structure) ;
* payer deux fois, ou payer une commande annulée (C5) ;
* rétrograder un encaissement par un rejeu de webhook (P1) ;
* rembourser plus que ce qui a été encaissé, en une fois ou en plusieurs (P3).
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Refund, Transaction
from apps.payments.services import PaymentService
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
SECRET = "secret-de-test"

#: Le secret partagé, posé pour la durée du test. `override_settings` ne sait
#: décorer qu'un `SimpleTestCase` ; la fixture `settings` de pytest-django fait
#: la même chose et se pose sur une classe par `usefixtures`.
signed = pytest.mark.usefixtures("webhook_secret")


@pytest.fixture
def webhook_secret(settings: object) -> None:
    settings.PAYMENT_WEBHOOK_SECRET = SECRET  # type: ignore[attr-defined]


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def as_staff(restaurant: Restaurant) -> APIClient:
    member = User.objects.create_user(
        "caisse@elcorazon.test", "motdepasse", full_name="Kofi Caisse", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name="Caisse", permissions=["orders.refund"]))
    # Sans rattachement, la permission `orders.refund` ne porterait sur aucune
    # commande — c'est le troisième étage de l'ADR-005.
    StaffMembership.objects.create(user=member, restaurant=restaurant)
    separate = APIClient()
    separate.force_authenticate(member)
    return separate


def post_webhook(
    client: APIClient,
    payload: dict[str, object],
    *,
    provider: str = PaymentProvider.PAYDUNYA,
    secret: str = SECRET,
) -> object:
    """Poste une notification signée comme le ferait le prestataire."""
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        reverse("v1:payments:webhook", args=[provider]),
        data=body,
        content_type="application/json",
        headers={"X-Signature": signature},
    )


@pytest.fixture
def initiated(as_customer: APIClient, order: Order) -> Transaction:
    """Commande dont le paiement est ouvert chez le prestataire."""
    as_customer.post(reverse("v1:payments:initiate", args=[order.pk]))
    return Transaction.objects.get(order=order)


class TestInitiation:
    def test_ouvre_une_transaction_et_rend_une_url(
        self, as_customer: APIClient, order: Order
    ) -> None:
        response = as_customer.post(reverse("v1:payments:initiate", args=[order.pk]))

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["checkout_url"]
        assert response.data["transaction"]["amount"] == {"amount": "4000", "currency": XOF}
        assert response.data["transaction"]["status"] == PaymentStatus.PROCESSING

    def test_une_commande_annulee_n_accepte_plus_de_paiement(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """C5 — garde absente de l'implémentation précédente : on pouvait
        payer une commande que personne ne préparerait."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED)

        response = as_customer.post(reverse("v1:payments:initiate", args=[order.pk]))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "annulée" in response.data["detail"]

    @signed
    def test_une_commande_deja_reglee_n_accepte_pas_un_second_paiement(
        self, as_customer: APIClient, client: APIClient, order: Order, initiated: Transaction
    ) -> None:
        """C5 — sans cette garde, un client tapant deux fois payait deux fois."""
        post_webhook(
            client,
            {
                "event_id": "evt-1",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
        )

        response = as_customer.post(reverse("v1:payments:initiate", args=[order.pk]))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "déjà réglée" in response.data["detail"]

    def test_la_commande_d_autrui_est_introuvable(
        self, client: APIClient, courier_user: User, order: Order
    ) -> None:
        client.force_authenticate(courier_user)

        response = client.post(reverse("v1:payments:initiate", args=[order.pk]))

        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        assert Transaction.objects.count() == 0


@signed
class TestWebhook:
    def test_l_encaissement_confirme_la_commande(
        self, client: APIClient, order: Order, initiated: Transaction
    ) -> None:
        """Le webhook est la **seule** source de vérité : c'est lui, et rien
        d'autre, qui fait passer la commande en confirmée."""
        response = post_webhook(
            client,
            {
                "event_id": "evt-ok",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        initiated.refresh_from_db()
        order.refresh_from_db()
        assert initiated.status == PaymentStatus.COMPLETED
        assert initiated.completed_at is not None
        assert order.status == OrderStatus.CONFIRMED

    def test_une_signature_invalide_est_rejetee(
        self, client: APIClient, initiated: Transaction
    ) -> None:
        """Sans signature vérifiée, n'importe qui déclarerait n'importe quelle
        commande payée en postant du JSON."""
        response = post_webhook(
            client,
            {
                "event_id": "evt-faux",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
            secret="mauvais-secret",
        )

        # 403 et non 401 : la route ne déclare aucun authentificateur DRF —
        # la signature *est* le justificatif — donc aucun schéma n'est proposé
        # en défi, et DRF rend un refus sec plutôt qu'une invitation.
        assert response.status_code == status.HTTP_403_FORBIDDEN
        initiated.refresh_from_db()
        assert initiated.status == PaymentStatus.PROCESSING

    def test_une_notification_non_signee_n_est_pas_enregistree(
        self, client: APIClient, initiated: Transaction
    ) -> None:
        """Rien n'entre en base avant la vérification, sans quoi la table des
        événements se remplit au gré des inconnus."""
        from apps.payments.models import WebhookEvent

        post_webhook(client, {"event_id": "evt-spam"}, secret="mauvais")

        assert WebhookEvent.objects.count() == 0

    def test_un_rejeu_ne_rejoue_rien(
        self, client: APIClient, order: Order, initiated: Transaction
    ) -> None:
        """P1 — l'unicité de `(provider, event_id)` arbitre, pas un
        `if déjà_traité` que deux workers franchiraient tous les deux."""
        payload = {
            "event_id": "evt-unique",
            "provider_reference": initiated.provider_reference,
            "status": PaymentStatus.COMPLETED,
        }
        post_webhook(client, payload)
        rejeu = post_webhook(client, payload)

        assert rejeu.status_code == status.HTTP_200_OK
        assert rejeu.data["accepted"] is True
        assert order.status_events.count() == 1

    def test_un_rejeu_n_ecrit_rien_et_ne_heurte_pas_la_contrainte(
        self, client: APIClient, initiated: Transaction
    ) -> None:
        """Le rejeu se résout par une **lecture**, pas par une erreur avalée.

        L'idempotence était obtenue en tentant l'insertion puis en rattrapant
        l'`IntegrityError`. Correct fonctionnellement, mais chaque rejeu — cas
        parfaitement normal, les prestataires renvoient leurs notifications —
        laissait un `duplicate key value violates unique constraint
        "webhook_event_unique_per_provider"` dans le journal PostgreSQL. Les
        vraies erreurs s'y noyaient.
        """
        from apps.payments.models import WebhookEvent

        payload = {
            "event_id": "evt-rejeu",
            "provider_reference": initiated.provider_reference,
            "status": PaymentStatus.COMPLETED,
        }
        post_webhook(client, payload)

        with CaptureQueriesContext(connection) as capture:
            rejeu = post_webhook(client, payload)

        assert rejeu.status_code == status.HTTP_200_OK
        assert WebhookEvent.objects.filter(event_id="evt-rejeu").count() == 1

        inserts = [
            q["sql"]
            for q in capture.captured_queries
            if "INSERT" in q["sql"] and "payments_webhookevent" in q["sql"]
        ]
        assert not inserts, f"Le rejeu tente encore une insertion : {inserts}"

    def test_un_encaissement_ne_redescend_jamais(
        self, client: APIClient, order: Order, initiated: Transaction
    ) -> None:
        """P1 — la transition `completed → failed` n'existe pas dans la
        machine ; un rejeu tardif ne peut donc pas défaire un encaissement.

        La notification est **acceptée et tracée**, pas refusée : un 409 rendu
        au prestataire le ferait revenir indéfiniment sur une transition qui ne
        sera jamais légale. L'invariant tient par la transaction qui ne bouge
        pas, pas par le code de statut qu'on renvoie.
        """
        from apps.payments.models import WebhookEvent

        post_webhook(
            client,
            {
                "event_id": "evt-1",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
        )
        tardif = post_webhook(
            client,
            {
                "event_id": "evt-2",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.FAILED,
            },
        )

        assert tardif.status_code == status.HTTP_200_OK
        initiated.refresh_from_db()
        assert initiated.status == PaymentStatus.COMPLETED

        trace = WebhookEvent.objects.get(event_id="evt-2")
        assert "Transition refusée" in trace.processing_error

    def test_un_echec_est_enregistre_avec_son_motif(
        self, client: APIClient, order: Order, initiated: Transaction
    ) -> None:
        post_webhook(
            client,
            {
                "event_id": "evt-ko",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.FAILED,
                "reason": "Solde insuffisant",
            },
        )

        initiated.refresh_from_db()
        order.refresh_from_db()
        assert initiated.status == PaymentStatus.FAILED
        assert initiated.failure_reason == "Solde insuffisant"
        assert order.status == OrderStatus.PENDING

    def test_une_reference_inconnue_est_acceptee_et_tracee(self, client: APIClient) -> None:
        """Répondre en erreur ferait retenter le prestataire indéfiniment ; la
        notification est donc acceptée, et l'anomalie tracée."""
        from apps.payments.models import WebhookEvent

        response = post_webhook(
            client,
            {"event_id": "evt-orphelin", "provider_reference": "INCONNUE", "status": "completed"},
        )

        assert response.status_code == status.HTTP_200_OK
        assert "INCONNUE" in WebhookEvent.objects.get().processing_error

    def test_un_prestataire_inconnu_est_refuse(
        self, client: APIClient, initiated: Transaction
    ) -> None:
        response = post_webhook(
            client,
            {"event_id": "e", "provider_reference": "x", "status": "completed"},
            provider="banque-imaginaire",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestSecretAbsent:
    def test_sans_secret_configure_toute_notification_est_refusee(
        self, client: APIClient, settings: object, initiated: Transaction
    ) -> None:
        """Une configuration oubliée doit fermer la porte, pas l'ouvrir."""
        settings.PAYMENT_WEBHOOK_SECRET = ""  # type: ignore[attr-defined]

        response = post_webhook(
            client,
            {
                "event_id": "evt",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


@signed
class TestRemboursement:
    @pytest.fixture
    def paid(self, client: APIClient, order: Order, initiated: Transaction) -> Transaction:
        post_webhook(
            client,
            {
                "event_id": "evt-paid",
                "provider_reference": initiated.provider_reference,
                "status": PaymentStatus.COMPLETED,
            },
        )
        initiated.refresh_from_db()
        return initiated

    def rembourser(self, client: APIClient, order: Order, txn: Transaction, montant: int) -> object:
        return client.post(
            reverse("v1:payments:refund", args=[order.pk]),
            {
                "transaction": str(txn.pk),
                "amount": {"amount": str(montant), "currency": XOF},
                "reason": "Commande incomplète",
            },
            format="json",
        )

    def test_le_personnel_rembourse_partiellement(
        self, as_staff: APIClient, order: Order, paid: Transaction
    ) -> None:
        response = self.rembourser(as_staff, order, paid, 1_000)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["amount"] == {"amount": "1000", "currency": XOF}

    def test_au_dela_de_l_encaissement_c_est_refuse(
        self, as_staff: APIClient, order: Order, paid: Transaction
    ) -> None:
        """P3 — l'ancien code laissait rembourser plus que le montant payé."""
        response = self.rembourser(as_staff, order, paid, 5_000)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Refund.objects.count() == 0

    def test_le_cumul_des_remboursements_est_plafonne(
        self, as_staff: APIClient, order: Order, paid: Transaction
    ) -> None:
        """La moitié qui manquait : plafonner chaque demande isolément laisse
        rembourser trois fois la totalité en trois appels."""
        self.rembourser(as_staff, order, paid, 3_000)
        second = self.rembourser(as_staff, order, paid, 3_000)

        assert second.status_code == status.HTTP_409_CONFLICT
        assert Refund.objects.count() == 1

    def test_une_transaction_non_encaissee_ne_se_rembourse_pas(
        self, as_staff: APIClient, order: Order, initiated: Transaction
    ) -> None:
        response = self.rembourser(as_staff, order, initiated, 100)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_client_ne_se_rembourse_pas_lui_meme(
        self, as_customer: APIClient, order: Order, paid: Transaction
    ) -> None:
        response = self.rembourser(as_customer, order, paid, 1_000)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Refund.objects.count() == 0


class TestHistorique:
    def test_le_client_voit_les_transactions_de_ses_commandes(
        self, as_customer: APIClient, initiated: Transaction
    ) -> None:
        response = as_customer.get(reverse("v1:payments:transaction-list"))

        assert response.data["count"] == 1

    def test_il_ne_voit_pas_celles_des_autres(
        self, client: APIClient, courier_user: User, initiated: Transaction
    ) -> None:
        client.force_authenticate(courier_user)

        assert client.get(reverse("v1:payments:transaction-list")).data["count"] == 0


class TestServiceDirect:
    def test_le_total_encaisse_ignore_les_transactions_en_cours(
        self, order: Order, customer: User
    ) -> None:
        """Une tentative en cours n'est pas de l'argent reçu."""
        Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="EN-COURS",
            amount=Money(4_000, XOF),
            status=PaymentStatus.PROCESSING,
        )

        from apps.payments.services import settled_total

        assert settled_total(order) == Money(0, XOF)

    def test_un_paiement_partiel_ne_confirme_pas_la_commande(
        self, order: Order, customer: User
    ) -> None:
        """Cas du paiement partagé : la commande n'est engagée que lorsque
        toutes les parts sont réglées."""
        Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="MOITIE",
            amount=Money(2_000, XOF),
            status=PaymentStatus.COMPLETED,
        )
        PaymentService._confirm_order(order)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING


class TestMoyenDePaiement:
    def test_les_especes_passent_par_le_prestataire_caisse(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """Une commande en espèces ouvre quand même une transaction : sans
        elle, la livraison n'aurait rien à solder et le montant encaissé au
        pied de l'immeuble n'apparaîtrait nulle part."""
        Order.objects.filter(pk=order.pk).update(payment_method=PaymentMethod.CASH)

        as_customer.post(reverse("v1:payments:initiate", args=[order.pk]))

        assert Transaction.objects.get().provider == PaymentProvider.CASH
