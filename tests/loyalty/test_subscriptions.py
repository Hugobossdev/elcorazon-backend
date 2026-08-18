"""Abonnements — invariant P4.

**Le prix vient du catalogue, jamais du client.** L'implémentation précédente
acceptait `monthly_price` dans la requête d'inscription : n'importe qui
pouvait s'abonner au tarif de son choix. `TestP4` vérifie que rien dans le
contrat d'entrée ne laisse passer un montant.

Le reste de la suite couvre ce qui rend le règlement fiable : l'abonnement
s'active **par l'encaissement vérifié** (`TestActivationParWebhook`), jamais
par la requête qui l'a demandé — même chemin que le paiement d'une commande
(P1, P2) — et un seul abonnement ouvert à la fois par client
(`TestUnSeulAbonnementOuvert`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.loyalty.models import (
    Subscription,
    SubscriptionPayment,
    SubscriptionPlan,
    SubscriptionStatus,
)
from apps.loyalty.subscriptions import SubscriptionService
from apps.orders.models import Order
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.state_machine import IllegalTransition

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
SECRET = "secret-de-test"


@pytest.fixture
def webhook_secret(settings: Any) -> None:
    settings.PAYMENT_WEBHOOK_SECRET = SECRET


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def plan() -> SubscriptionPlan:
    return SubscriptionPlan.objects.create(
        name="Essentiel",
        price=Money(2_000, XOF),
        billing_period_days=30,
    )


def encaisse(client: APIClient, txn: Transaction, *, event_id: str | None = None) -> Any:
    """Solde une transaction par notification signée — même chemin qu'un paiement de commande."""
    txn.refresh_from_db()
    payload = {
        "event_id": event_id or f"evt-{txn.pk}",
        "provider_reference": txn.provider_reference,
        "status": PaymentStatus.COMPLETED,
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        reverse("v1:payments:webhook", args=[PaymentProvider.PAYDUNYA]),
        data=body,
        content_type="application/json",
        headers={"X-Signature": signature},
    )


class TestP4:
    """Le prix vient du catalogue, jamais du client."""

    def test_le_contrat_d_entree_n_accepte_qu_un_identifiant_de_plan(self) -> None:
        from apps.loyalty.serializers import SubscribeRequestSerializer

        assert set(SubscribeRequestSerializer().fields) == {"plan"}

    def test_le_montant_facture_est_celui_du_plan_pas_celui_envoye(
        self, as_customer: APIClient, customer: User, plan: SubscriptionPlan
    ) -> None:
        response = as_customer.post(
            reverse("v1:loyalty:subscription-subscribe"),
            {"plan": str(plan.pk), "amount": {"amount": "1", "currency": XOF}},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        txn = Transaction.objects.get()
        assert txn.amount == Money(2_000, XOF)

    def test_un_plan_retire_du_catalogue_est_introuvable(
        self, as_customer: APIClient, customer: User, plan: SubscriptionPlan
    ) -> None:
        plan.is_active = False
        plan.save(update_fields=["is_active"])

        response = as_customer.post(
            reverse("v1:loyalty:subscription-subscribe"), {"plan": str(plan.pk)}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestSouscription:
    def test_souscrire_ouvre_un_abonnement_en_attente_et_une_demande_de_paiement(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, instruction = SubscriptionService.subscribe(user=customer, plan=plan)

        assert subscription.status == SubscriptionStatus.PENDING
        assert instruction.checkout_url
        link = SubscriptionPayment.objects.get(subscription=subscription)
        assert link.transaction.status == PaymentStatus.PROCESSING
        assert link.transaction.order is None

    def test_un_plan_suspendu_ne_s_offre_pas(self, customer: User, plan: SubscriptionPlan) -> None:
        plan.is_active = False
        plan.save(update_fields=["is_active"])

        with pytest.raises(BusinessRuleViolation, match="plus proposé"):
            SubscriptionService.subscribe(user=customer, plan=plan)


class TestUnSeulAbonnementOuvert:
    def test_pas_deux_abonnements_ouverts_pour_le_meme_client(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        SubscriptionService.subscribe(user=customer, plan=plan)

        with pytest.raises(BusinessRuleViolation, match="déjà ouvert"):
            SubscriptionService.subscribe(user=customer, plan=plan)

        assert Subscription.objects.filter(user=customer).count() == 1

    def test_la_contrainte_de_base_le_tient_independamment_du_service(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        """`QuerySet.update()` contourne le service : c'est la base qui doit
        refuser, pas seulement lui."""
        from django.db.utils import IntegrityError

        Subscription.objects.create(user=customer, plan=plan, status=SubscriptionStatus.PENDING)

        with pytest.raises(IntegrityError):
            Subscription.objects.create(user=customer, plan=plan, status=SubscriptionStatus.ACTIVE)

    def test_un_second_abonnement_est_possible_apres_resiliation(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        premier, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        SubscriptionService.cancel(subscription=premier)

        second, _ = SubscriptionService.subscribe(user=customer, plan=plan)

        assert second.pk != premier.pk


class TestActivationParWebhook:
    def test_l_abonnement_s_active_a_l_encaissement_pas_a_la_demande(
        self, client: APIClient, customer: User, plan: SubscriptionPlan, webhook_secret: None
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        link = SubscriptionPayment.objects.get(subscription=subscription)

        subscription.refresh_from_db()
        assert subscription.status == SubscriptionStatus.PENDING

        encaisse(client, link.transaction)

        subscription.refresh_from_db()
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.current_period_start is not None
        assert subscription.current_period_end is not None

    def test_un_paiement_qui_ne_regle_pas_d_abonnement_ne_fait_rien(
        self, client: APIClient, order: Order, customer: User, webhook_secret: None
    ) -> None:
        """La plupart des transactions réglées n'ont rien à voir avec un
        abonnement : le récepteur doit s'effacer proprement."""
        from apps.payments.services import PaymentService

        txn, _ = PaymentService.initiate(order=order, payer=customer)

        response = encaisse(client, txn)

        assert response.status_code == status.HTTP_200_OK

    def test_un_renouvellement_prolonge_sans_repasser_par_pending(
        self, client: APIClient, customer: User, plan: SubscriptionPlan, webhook_secret: None
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        premier_lien = SubscriptionPayment.objects.get(subscription=subscription)
        encaisse(client, premier_lien.transaction)

        subscription.refresh_from_db()
        premiere_echeance = subscription.current_period_end
        assert premiere_echeance is not None

        _, instruction = SubscriptionService._charge(subscription)
        second_lien = SubscriptionPayment.objects.filter(subscription=subscription).latest(
            "created_at"
        )
        encaisse(client, second_lien.transaction, event_id="evt-renouvellement")

        subscription.refresh_from_db()
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.current_period_end is not None
        assert subscription.current_period_end > premiere_echeance
        assert instruction.checkout_url


class TestResiliation:
    def test_le_client_resilie_son_abonnement(self, customer: User, plan: SubscriptionPlan) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)

        resilie = SubscriptionService.cancel(subscription=subscription)

        assert resilie.status == SubscriptionStatus.CANCELLED
        assert resilie.auto_renew is False
        assert resilie.cancelled_at is not None

    def test_un_abonnement_deja_resilie_ne_se_resilie_pas_deux_fois(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        SubscriptionService.cancel(subscription=subscription)

        with pytest.raises(IllegalTransition):
            SubscriptionService.cancel(subscription=subscription)


class TestRenouvellementPlanifie:
    def test_un_abonnement_echu_est_facture(self, customer: User, plan: SubscriptionPlan) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now() - dt.timedelta(days=30),
            current_period_end=timezone.now() - dt.timedelta(hours=1),
        )
        # La transaction d'ouverture doit être soldée : sinon `due_for_renewal`
        # l'exclurait pour règlement déjà en cours, ce qui est un autre test.
        premier_lien = SubscriptionPayment.objects.get(subscription=subscription)
        Transaction.objects.filter(pk=premier_lien.transaction_id).update(
            status=PaymentStatus.COMPLETED
        )

        compte = SubscriptionService.renew_due()

        assert compte == 1
        assert SubscriptionPayment.objects.filter(subscription=subscription).count() == 2

    def test_un_renouvellement_deja_en_cours_n_en_ouvre_pas_un_second(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now() - dt.timedelta(days=30),
            current_period_end=timezone.now() - dt.timedelta(hours=1),
        )
        # La transaction d'ouverture reste `processing` : un renouvellement ne
        # doit pas en ouvrir une seconde tant que le webhook n'est pas revenu.

        compte = SubscriptionService.renew_due()

        assert compte == 0
        assert SubscriptionPayment.objects.filter(subscription=subscription).count() == 1

    def test_un_abonnement_hors_delai_de_grace_expire(
        self, customer: User, plan: SubscriptionPlan, settings: Any
    ) -> None:
        settings.SUBSCRIPTION_RENEWAL_GRACE_DAYS = 3
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() - dt.timedelta(days=10),
        )

        SubscriptionService.renew_due()

        subscription.refresh_from_db()
        assert subscription.status == SubscriptionStatus.EXPIRED

    def test_un_abonnement_resilie_n_est_pas_repris_par_la_tache(
        self, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        SubscriptionService.cancel(subscription=subscription)
        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_end=timezone.now() - dt.timedelta(days=1)
        )

        compte = SubscriptionService.renew_due()

        subscription.refresh_from_db()
        assert compte == 0
        assert subscription.status == SubscriptionStatus.CANCELLED


class TestRoutes:
    def test_le_catalogue_ne_montre_pas_les_plans_retires(
        self, as_customer: APIClient, plan: SubscriptionPlan
    ) -> None:
        SubscriptionPlan.objects.create(name="Retiré", price=Money(500, XOF), is_active=False)

        response = as_customer.get(reverse("v1:loyalty:subscription-plan-list"))

        assert [p["name"] for p in response.data["results"]] == [plan.name]

    def test_le_prix_sort_en_montant_et_pas_en_entier_nu(
        self, as_customer: APIClient, plan: SubscriptionPlan
    ) -> None:
        response = as_customer.get(reverse("v1:loyalty:subscription-plan-list"))

        assert response.data["results"][0]["price"] == {"amount": "2000", "currency": XOF}

    def test_le_client_consulte_ses_abonnements(
        self, as_customer: APIClient, customer: User, plan: SubscriptionPlan
    ) -> None:
        SubscriptionService.subscribe(user=customer, plan=plan)

        response = as_customer.get(reverse("v1:loyalty:subscription-list"))

        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["status"] == SubscriptionStatus.PENDING

    def test_le_client_annule_son_abonnement_par_l_api(
        self, as_customer: APIClient, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)

        response = as_customer.post(
            reverse("v1:loyalty:subscription-cancel", args=[subscription.pk])
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == SubscriptionStatus.CANCELLED


class TestCloisonnement:
    def test_l_abonnement_d_un_autre_client_est_introuvable(
        self, client: APIClient, customer: User, plan: SubscriptionPlan
    ) -> None:
        """Le mouvement d'autrui est introuvable, pas interdit (ADR-005) — donc
        un autre *client*, pas un rôle différent qu'`IsCustomer` aurait déjà
        écarté avant même la question de propriété."""
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        autre = User.objects.create_user("autre@elcorazon.test", "motdepasse", full_name="Autre")
        client.force_authenticate(autre)

        response = client.post(reverse("v1:loyalty:subscription-cancel", args=[subscription.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_un_livreur_n_a_pas_acces_aux_abonnements(
        self, client: APIClient, courier_user: User, customer: User, plan: SubscriptionPlan
    ) -> None:
        subscription, _ = SubscriptionService.subscribe(user=customer, plan=plan)
        client.force_authenticate(courier_user)

        response = client.post(reverse("v1:loyalty:subscription-cancel", args=[subscription.pk]))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_les_abonnements_d_autrui_sont_invisibles(
        self, as_customer: APIClient, courier_user: User, plan: SubscriptionPlan
    ) -> None:
        SubscriptionService.subscribe(user=courier_user, plan=plan)

        response = as_customer.get(reverse("v1:loyalty:subscription-list"))

        assert response.data["results"] == []
