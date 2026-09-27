"""Le back-office des sorties d'argent — retraits livreurs, remboursements.

Les tests décisifs sont `test_constater_solde_la_demande_et_signe` et
`test_refuser_rend_les_gains_une_seule_fois` : avant ce module,
`WithdrawalService.settle` et `fail` n'avaient **aucun appelant** hors des
tests. Une demande de retrait débitait les gains du livreur, puis restait en
attente pour toujours — l'argent n'était plus dans l'application et n'était pas
chez lui.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import CourierProfile, VehicleType, VerificationStatus
from apps.notifications.models import Notification
from apps.orders.models import Order
from apps.payments.models import PaymentProvider, PaymentStatus, Refund, Transaction, Withdrawal
from apps.payments.services import RefundService, WithdrawalService
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money
from tests.fixtures import XOF

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LISTE = "v1:payments:managed-withdrawal-list"
CONSTATER = "v1:payments:managed-withdrawal-settle"
REFUSER = "v1:payments:managed-withdrawal-reject"


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def paid_courier(courier: CourierProfile) -> CourierProfile:
    courier.total_earnings = Money(10_000, XOF)
    courier.save(update_fields=["total_earnings_minor", "total_earnings_currency"])
    return courier


@pytest.fixture
def demande(paid_courier: CourierProfile) -> Withdrawal:
    return WithdrawalService.request(courier=paid_courier, amount=Money(4_000, XOF))


@pytest.fixture
def caissier(restaurant: Restaurant) -> User:
    return personnel("caisse@elcorazon.test", restaurant, "payouts.read", "payouts.settle")


@pytest.fixture
def ailleurs(restaurant: Restaurant) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=restaurant.zone,
        address="Kara",
        location=restaurant.location,
        phone="+22890000009",
    )


class TestLecture:
    def test_la_demande_apparait_avec_de_quoi_verser(
        self, caissier: User, demande: Withdrawal
    ) -> None:
        response = connecte(caissier).get(reverse(LISTE))

        assert response.status_code == status.HTTP_200_OK
        (ligne,) = response.data["results"]
        assert ligne["status"] == PaymentStatus.PENDING
        assert ligne["courier_name"] == demande.courier.user.full_name
        assert ligne["restaurant"] == demande.courier.restaurant.slug

    def test_sans_payouts_read_la_liste_est_refusee(
        self, restaurant: Restaurant, demande: Withdrawal
    ) -> None:
        operateur = personnel("op@elcorazon.test", restaurant, "orders.read")

        response = connecte(operateur).get(reverse(LISTE))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_gerant_ne_voit_pas_les_retraits_d_un_autre_etablissement(
        self, ailleurs: Restaurant, demande: Withdrawal
    ) -> None:
        gerant_kara = personnel("kara@elcorazon.test", ailleurs, "payouts.read")

        response = connecte(gerant_kara).get(reverse(LISTE))

        assert response.data["results"] == []

    def test_un_livreur_n_y_accede_pas(self, paid_courier: CourierProfile) -> None:
        response = connecte(paid_courier.user).get(reverse(LISTE))

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestConstat:
    def test_constater_solde_la_demande_et_signe(self, caissier: User, demande: Withdrawal) -> None:
        response = connecte(caissier).post(
            reverse(CONSTATER, args=[demande.pk]),
            {"provider_reference": "PD-VIR-2026-0042"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == PaymentStatus.COMPLETED
        assert response.data["processed_by_name"] == caissier.full_name
        demande.refresh_from_db()
        assert demande.provider_reference == "PD-VIR-2026-0042"
        assert demande.processed_by == caissier
        assert demande.completed_at is not None

    def test_la_reference_du_virement_est_exigee(self, caissier: User, demande: Withdrawal) -> None:
        """Un constat sans référence ne prouve rien le jour où le livreur
        affirme n'avoir rien reçu."""
        response = connecte(caissier).post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "  "}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        demande.refresh_from_db()
        assert demande.status == PaymentStatus.PENDING

    def test_lire_ne_donne_pas_le_droit_de_verser(
        self, restaurant: Restaurant, demande: Withdrawal
    ) -> None:
        lecteur = personnel("lecteur@elcorazon.test", restaurant, "payouts.read")

        response = connecte(lecteur).post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "X"}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_on_ne_signe_pas_deux_fois_le_meme_versement(
        self, caissier: User, demande: Withdrawal
    ) -> None:
        client = connecte(caissier)
        client.post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "A"}, format="json"
        )

        response = client.post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "B"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        demande.refresh_from_db()
        assert demande.provider_reference == "A"

    def test_hors_perimetre_la_demande_est_introuvable(
        self, ailleurs: Restaurant, demande: Withdrawal
    ) -> None:
        gerant_kara = personnel("kara@elcorazon.test", ailleurs, "payouts.read", "payouts.settle")

        response = connecte(gerant_kara).post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "X"}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_le_livreur_apprend_que_c_est_verse_et_sous_quelle_reference(
        self, caissier: User, demande: Withdrawal
    ) -> None:
        connecte(caissier).post(
            reverse(CONSTATER, args=[demande.pk]),
            {"provider_reference": "PD-VIR-7"},
            format="json",
        )

        avis = Notification.objects.get(user=demande.courier.user, title="Retrait versé")
        assert "PD-VIR-7" in avis.body


class TestRefus:
    def test_refuser_rend_les_gains_une_seule_fois(
        self, caissier: User, demande: Withdrawal, paid_courier: CourierProfile
    ) -> None:
        client = connecte(caissier)
        premier = client.post(
            reverse(REFUSER, args=[demande.pk]), {"reason": "Numéro invalide"}, format="json"
        )
        second = client.post(
            reverse(REFUSER, args=[demande.pk]), {"reason": "Encore"}, format="json"
        )

        assert premier.status_code == status.HTTP_200_OK
        assert premier.data["status"] == PaymentStatus.FAILED
        assert second.status_code == status.HTTP_409_CONFLICT
        paid_courier.refresh_from_db()
        # 10 000 − 4 000 demandés + 4 000 rendus, et pas une seconde fois.
        assert paid_courier.total_earnings == Money(10_000, XOF)

    def test_le_motif_est_exige(self, caissier: User, demande: Withdrawal) -> None:
        response = connecte(caissier).post(
            reverse(REFUSER, args=[demande.pk]), {"reason": ""}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_versement_constate_ne_se_refuse_plus(
        self, caissier: User, demande: Withdrawal, paid_courier: CourierProfile
    ) -> None:
        """Refuser après coup recréditerait des gains déjà versés : le livreur
        toucherait deux fois."""
        client = connecte(caissier)
        client.post(
            reverse(CONSTATER, args=[demande.pk]), {"provider_reference": "A"}, format="json"
        )

        response = client.post(
            reverse(REFUSER, args=[demande.pk]), {"reason": "Oups"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        paid_courier.refresh_from_db()
        assert paid_courier.total_earnings == Money(6_000, XOF)

    def test_le_livreur_lit_le_motif_et_sait_ses_gains_rendus(
        self, caissier: User, demande: Withdrawal
    ) -> None:
        connecte(caissier).post(
            reverse(REFUSER, args=[demande.pk]), {"reason": "Numéro invalide."}, format="json"
        )

        avis = Notification.objects.get(user=demande.courier.user, title="Retrait non versé")
        assert "Numéro invalide." in avis.body
        assert ".." not in avis.body
        assert "rendu à vos gains" in avis.body


class TestAlerteDeLExploitation:
    def test_une_demande_previent_qui_peut_la_lire_et_seulement_lui(
        self, restaurant: Restaurant, paid_courier: CourierProfile
    ) -> None:
        caissier = personnel("caisse@elcorazon.test", restaurant, "payouts.read")
        cuisinier = personnel("cuisine@elcorazon.test", restaurant, "orders.read")

        WithdrawalService.request(courier=paid_courier, amount=Money(2_000, XOF))

        assert Notification.objects.filter(user=caissier, title="Retrait à verser").exists()
        assert not Notification.objects.filter(user=cuisinier, title="Retrait à verser").exists()


class TestRemboursements:
    @pytest.fixture
    def rembourse(self, order: Order, restaurant: Restaurant) -> Refund:
        transaction = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-001",
            amount=order.total,
            status=PaymentStatus.COMPLETED,
        )
        demandeur = personnel("gerant@elcorazon.test", restaurant, "orders.read", "orders.refund")
        return RefundService.refund(
            order=order,
            transaction_id=str(transaction.pk),
            amount=Money(1_000, XOF),
            reason="Plat manquant",
            actor=demandeur,
        )

    def test_la_liste_montre_ce_qu_il_reste_a_verser(
        self, restaurant: Restaurant, rembourse: Refund
    ) -> None:
        lecteur = personnel("op@elcorazon.test", restaurant, "orders.read")

        response = connecte(lecteur).get(reverse("v1:payments:managed-refund-list"))

        (ligne,) = response.data["results"]
        assert ligne["status"] == PaymentStatus.PENDING
        assert ligne["order_reference"] == rembourse.order.reference

    def test_constater_exige_orders_refund(self, restaurant: Restaurant, rembourse: Refund) -> None:
        lecteur = personnel("op@elcorazon.test", restaurant, "orders.read")

        response = connecte(lecteur).post(
            reverse("v1:payments:managed-refund-settle", args=[rembourse.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_le_back_office_clot_ce_qu_il_a_demande(
        self, restaurant: Restaurant, rembourse: Refund
    ) -> None:
        """Jusqu'ici, seule l'administration Django le pouvait."""
        gerant = personnel("gerant2@elcorazon.test", restaurant, "orders.read", "orders.refund")

        response = connecte(gerant).post(
            reverse("v1:payments:managed-refund-settle", args=[rembourse.pk]),
            {"provider_reference": "PD-RB-9"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == PaymentStatus.COMPLETED
        rembourse.refresh_from_db()
        assert "PD-RB-9" in rembourse.reason

    def test_hors_perimetre_le_remboursement_est_introuvable(
        self, ailleurs: Restaurant, rembourse: Refund
    ) -> None:
        gerant_kara = personnel("kara@elcorazon.test", ailleurs, "orders.read", "orders.refund")

        response = connecte(gerant_kara).post(
            reverse("v1:payments:managed-refund-settle", args=[rembourse.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


def test_les_retraits_d_une_autre_cuisine_ne_se_melangent_pas(
    ailleurs: Restaurant, caissier: User, demande: Withdrawal
) -> None:
    """Deux cuisines, deux livreurs : chacune ne voit que les siens."""
    livreur_kara = CourierProfile.objects.create(
        user=User.objects.create_user(
            "kara-livreur@elcorazon.test", "motdepasse", user_type=UserType.COURIER
        ),
        restaurant=ailleurs,
        vehicle_type=VehicleType.MOTORCYCLE,
        verification_status=VerificationStatus.APPROVED,
        total_earnings=Money(5_000, XOF),
    )
    WithdrawalService.request(courier=livreur_kara, amount=Money(1_000, XOF))

    identifiants = {ligne["id"] for ligne in connecte(caissier).get(reverse(LISTE)).data["results"]}

    assert identifiants == {str(demande.pk)}
