"""Signalisation d'appel — `/api/v1/calls/`.

Le test décisif est `test_on_ne_fait_pas_sonner_le_telephone_d_un_tiers` :
l'implémentation précédente acceptait `receiver_id` du client, si bien que
n'importe quel compte pouvait appeler n'importe quel autre. Ici le destinataire
est déduit de la course, et la commande est cherchée parmi celles de
l'appelant.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.calls.models import Call
from apps.calls.states import CallStatus
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    authenticated = APIClient()
    authenticated.force_authenticate(customer)
    return authenticated


@pytest.fixture
def as_courier(courier: CourierProfile) -> APIClient:
    authenticated = APIClient()
    authenticated.force_authenticate(courier.user)
    return authenticated


@pytest.fixture
def course(order: Order, courier: CourierProfile) -> Assignment:
    """Livraison en cours — le seul état où un appel a un sens."""
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.ON_THE_WAY)
    return Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.ON_THE_WAY)


def place_url(order: Order) -> str:
    return reverse("v1:calls:place", kwargs={"order_id": order.pk})


def action_url(call: Call, action: str) -> str:
    return reverse(f"v1:calls:call-{action}", kwargs={"pk": call.pk})


class TestOuverture:
    def test_le_client_appelle_son_livreur(
        self, as_customer: APIClient, course: Assignment, courier: CourierProfile
    ) -> None:
        response = as_customer.post(place_url(course.order), {}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == CallStatus.RINGING
        # Le destinataire vient de la course, il n'est pas déclaré.
        assert str(response.data["callee"]) == str(courier.user.pk)

    def test_le_livreur_appelle_son_client(
        self, as_courier: APIClient, course: Assignment, customer: User
    ) -> None:
        response = as_courier.post(place_url(course.order), {}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert str(response.data["callee"]) == str(customer.pk)

    def test_le_canal_est_derive_de_l_appel(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        """L'app composait `order_{id}_call` : connaître une commande suffisait
        à rejoindre la conversation en cours."""
        response = as_customer.post(place_url(course.order), {}, format="json")

        assert response.data["channel_name"] == f"call-{response.data['id']}"

    def test_sans_livraison_en_cours_il_n_y_a_personne_a_appeler(
        self, as_customer: APIClient, order: Order
    ) -> None:
        response = as_customer.post(place_url(order), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_seul_appel_actif_par_commande(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        as_customer.post(place_url(course.order), {}, format="json")
        response = as_customer.post(place_url(course.order), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Call.objects.count() == 1


class TestCloisonnement:
    def test_on_ne_fait_pas_sonner_le_telephone_d_un_tiers(
        self, client: APIClient, course: Assignment
    ) -> None:
        """404 : la commande est cherchée parmi celles de l'appelant."""
        intrus = User.objects.create_user(
            "intrus@elcorazon.test", "motdepasse", full_name="Kodjo Intrus"
        )
        client.force_authenticate(intrus)

        response = client.post(place_url(course.order), {}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not Call.objects.exists()

    def test_un_tiers_ne_lit_pas_l_appel_des_autres(
        self, as_customer: APIClient, client: APIClient, course: Assignment
    ) -> None:
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]

        intrus = User.objects.create_user(
            "curieux@elcorazon.test", "motdepasse", full_name="Yao Curieux"
        )
        client.force_authenticate(intrus)

        response = client.get(reverse("v1:calls:call-detail", kwargs={"pk": call_id}))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_l_appelant_ne_decroche_pas_a_la_place_du_destinataire(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)

        response = as_customer.post(action_url(call, "accept"), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        call.refresh_from_db()
        assert call.status == CallStatus.RINGING

    def test_un_appel_exige_un_jeton(self, client: APIClient, course: Assignment) -> None:
        response = client.post(place_url(course.order), {}, format="json")

        assert response.status_code in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }


class TestCycleDeVie:
    def test_decrocher_puis_raccrocher_compte_une_duree(
        self, as_customer: APIClient, as_courier: APIClient, course: Assignment
    ) -> None:
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)

        as_courier.post(action_url(call, "accept"), {}, format="json")
        response = as_customer.post(action_url(call, "end"), {}, format="json")

        assert response.data["status"] == CallStatus.ENDED
        assert response.data["answered_at"] is not None

    def test_raccrocher_avant_decrochage_donne_un_appel_manque(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        """« Manqué » et « terminé » ne racontent pas la même chose à
        l'historique."""
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)

        response = as_customer.post(action_url(call, "end"), {}, format="json")

        assert response.data["status"] == CallStatus.MISSED
        assert response.data["duration_seconds"] == 0

    def test_un_appel_refuse_ne_se_rattrape_pas(
        self, as_customer: APIClient, as_courier: APIClient, course: Assignment
    ) -> None:
        """`declined` est terminal : la machine à états rend le rejeu
        inexprimable."""
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)

        as_courier.post(action_url(call, "decline"), {}, format="json")
        response = as_courier.post(action_url(call, "accept"), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_appel_termine_libere_la_commande(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        premier = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        as_customer.post(action_url(Call.objects.get(pk=premier), "end"), {}, format="json")

        response = as_customer.post(place_url(course.order), {}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["id"] != premier


class TestJetonRtc:
    def test_chaque_partie_recoit_un_uid_distinct(
        self, as_customer: APIClient, as_courier: APIClient, course: Assignment
    ) -> None:
        """Deux participants au même `uid` s'expulsent du canal — ce que faisait
        le hachage tronqué de l'app."""
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)
        as_courier.post(action_url(call, "accept"), {}, format="json")

        url = reverse("v1:calls:call-rtc-token", kwargs={"pk": call.pk})
        appelant = as_customer.get(url).data
        destinataire = as_courier.get(url).data

        assert appelant["uid"] != destinataire["uid"]
        assert appelant["channel_name"] == destinataire["channel_name"]
        assert appelant["token"] != destinataire["token"]

    def test_le_certificat_ne_sort_jamais(self, as_customer: APIClient, course: Assignment) -> None:
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]

        response = as_customer.get(reverse("v1:calls:call-rtc-token", kwargs={"pk": call_id}))

        assert "certificate" not in str(response.data).lower()
        assert response.data["app_id"]
        assert response.data["expires_in"] > 0

    def test_un_appel_termine_ne_delivre_plus_de_jeton(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        """Sinon le canal se rouvrirait après le raccrochage."""
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]
        call = Call.objects.get(pk=call_id)
        as_customer.post(action_url(call, "end"), {}, format="json")

        response = as_customer.get(reverse("v1:calls:call-rtc-token", kwargs={"pk": call.pk}))

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_tiers_n_obtient_pas_de_jeton(
        self, as_customer: APIClient, client: APIClient, course: Assignment
    ) -> None:
        call_id = as_customer.post(place_url(course.order), {}, format="json").data["id"]

        autre = User.objects.create_user(
            "autre@elcorazon.test", "motdepasse", full_name="Afi Autre", user_type=UserType.STAFF
        )
        client.force_authenticate(autre)

        response = client.get(reverse("v1:calls:call-rtc-token", kwargs={"pk": call_id}))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestHistorique:
    def test_chacun_ne_voit_que_ses_appels(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        as_customer.post(place_url(course.order), {}, format="json")

        response = as_customer.get(reverse("v1:calls:call-list"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1
