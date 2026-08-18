"""Retrait des gains d'un livreur — `/api/v1/payments/withdrawals/`.

L'app livreur appelait elle-même l'API de décaissement PayDunya, avec un montant
qu'elle calculait, puis écrivait « payé » en base : le bénéficiaire décidait de
ce qu'il touchait. Les tests décisifs sont ici
`test_on_ne_retire_pas_plus_que_ses_gains` et
`test_deux_demandes_ne_vident_pas_le_solde_deux_fois`.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.delivery.models import CourierProfile
from apps.payments.models import PaymentStatus, Withdrawal
from apps.payments.services import WithdrawalService
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
URL = "v1:payments:withdrawals"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def paid_courier(courier: CourierProfile) -> CourierProfile:
    """Livreur avec 10 000 F de gains acquis."""
    courier.total_earnings = Money(10_000, XOF)
    courier.save(update_fields=["total_earnings_minor", "total_earnings_currency"])
    return courier


@pytest.fixture
def as_courier(paid_courier: CourierProfile) -> APIClient:
    authenticated = APIClient()
    authenticated.force_authenticate(paid_courier.user)
    return authenticated


def montant(value: int) -> dict[str, str]:
    return {"amount": str(value), "currency": XOF}


class TestDemande:
    def test_le_livreur_demande_un_retrait(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        response = as_courier.post(reverse(URL), {"amount": montant(4_000)}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        # En attente, et non « versé » : le mouvement d'argent est un geste de
        # l'exploitation, pas une conséquence de cette requête.
        assert response.data["status"] == PaymentStatus.PENDING

    def test_les_gains_sont_debites_immediatement(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        as_courier.post(reverse(URL), {"amount": montant(4_000)}, format="json")

        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(6_000, XOF)

    def test_on_ne_retire_pas_plus_que_ses_gains(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        response = as_courier.post(reverse(URL), {"amount": montant(15_000)}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(10_000, XOF)

    def test_deux_demandes_ne_vident_pas_le_solde_deux_fois(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        """Le débit est fait à la demande, sous verrou : la seconde demande voit
        le solde déjà réduit."""
        as_courier.post(reverse(URL), {"amount": montant(7_000)}, format="json")
        response = as_courier.post(reverse(URL), {"amount": montant(7_000)}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(3_000, XOF)

    def test_un_montant_nul_est_refuse(self, as_courier: APIClient) -> None:
        response = as_courier.post(reverse(URL), {"amount": montant(0)}, format="json")

        assert response.status_code in {
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_409_CONFLICT,
        }

    def test_sans_gains_il_n_y_a_rien_a_retirer(
        self, client: APIClient, courier: CourierProfile
    ) -> None:
        client.force_authenticate(courier.user)

        response = client.post(reverse(URL), {"amount": montant(1_000)}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT


class TestCloisonnement:
    def test_un_client_ne_retire_rien(self, client: APIClient, customer: User) -> None:
        client.force_authenticate(customer)

        response = client.post(reverse(URL), {"amount": montant(1_000)}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_le_beneficiaire_ne_se_designe_pas(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        """Un `courier` dans le corps est ignoré : le bénéficiaire est
        l'appelant."""
        autre = CourierProfile.objects.create(
            user=User.objects.create_user(
                "autre.livreur@elcorazon.test", "motdepasse", full_name="Yao Autre"
            ),
            restaurant=paid_courier.restaurant,
            vehicle_type=paid_courier.vehicle_type,
        )

        as_courier.post(
            reverse(URL),
            {"amount": montant(1_000), "courier": str(autre.pk)},
            format="json",
        )

        assert Withdrawal.objects.filter(courier=autre).count() == 0
        assert Withdrawal.objects.filter(courier=paid_courier).count() == 1

    def test_chacun_ne_lit_que_ses_retraits(
        self, as_courier: APIClient, paid_courier: CourierProfile
    ) -> None:
        as_courier.post(reverse(URL), {"amount": montant(1_000)}, format="json")

        response = as_courier.get(reverse(URL))

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1

    def test_le_statut_ne_s_ecrit_pas_depuis_la_requete(self, as_courier: APIClient) -> None:
        """Une demande qui naîtrait « versée » ferait sortir de l'argent sans
        que personne l'ait versé."""
        response = as_courier.post(
            reverse(URL),
            {"amount": montant(1_000), "status": PaymentStatus.COMPLETED},
            format="json",
        )

        assert response.data["status"] == PaymentStatus.PENDING


class TestReglement:
    def test_un_versement_echoue_rend_les_gains(self, paid_courier: CourierProfile) -> None:
        """Sans recrédit, l'argent ne serait ni sur le compte du livreur, ni
        dans l'application."""
        withdrawal = WithdrawalService.request(courier=paid_courier, amount=Money(4_000, XOF))
        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(6_000, XOF)

        WithdrawalService.fail(withdrawal=withdrawal, reason="Numéro invalide")

        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(10_000, XOF)
        withdrawal.refresh_from_db()
        assert withdrawal.status == PaymentStatus.FAILED

    def test_un_versement_execute_ne_rend_rien(self, paid_courier: CourierProfile) -> None:
        withdrawal = WithdrawalService.request(courier=paid_courier, amount=Money(4_000, XOF))

        WithdrawalService.settle(withdrawal=withdrawal, provider_reference="PD-123")

        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(6_000, XOF)
        withdrawal.refresh_from_db()
        assert withdrawal.status == PaymentStatus.COMPLETED
        assert withdrawal.completed_at is not None
