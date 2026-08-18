"""Notation du livreur par le client — `POST /delivery/orders/{id}/rating/`.

Le geste est modeste, ses pièges le sont moins : noter la livraison d'autrui,
noter deux fois pour faire monter une moyenne, ou noter une course qui n'a
jamais été livrée. Les trois sont refusés ici, et deux le sont aussi en base.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, CourierRating
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from tests.fixtures import build_order

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
def delivered(order: Order, courier: CourierProfile) -> Assignment:
    """Commande livrée par le livreur de référence — l'état nominal."""
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED)
    return Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.DELIVERED)


def rating_url(order: Order) -> str:
    return reverse("v1:delivery:order-rating", kwargs={"order_id": order.pk})


class TestNotation:
    def test_le_client_note_sa_livraison(
        self, as_customer: APIClient, delivered: Assignment
    ) -> None:
        response = as_customer.post(
            rating_url(delivered.order), {"score": 4, "comment": "Rapide"}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["score"] == 4
        assert response.data["comment"] == "Rapide"

    def test_la_moyenne_du_livreur_suit(
        self, as_customer: APIClient, delivered: Assignment, courier: CourierProfile
    ) -> None:
        as_customer.post(rating_url(delivered.order), {"score": 5}, format="json")

        courier.refresh_from_db()
        assert courier.rating_count == 1
        assert courier.rating_average == Decimal("5.00")

    def test_la_moyenne_est_incrementale_et_arrondie(
        self,
        as_customer: APIClient,
        delivered: Assignment,
        courier: CourierProfile,
        restaurant: Restaurant,
        customer: User,
    ) -> None:
        """Deux notes, 5 puis 4 : 4,5. Puis une troisième à 4 : 4,33 et non 4,3.

        L'arrondi compte — la colonne n'accepte que deux décimales, et une
        division non arrondie la ferait déborder.
        """
        as_customer.post(rating_url(delivered.order), {"score": 5}, format="json")

        for rang, score in enumerate((4, 4), start=2):
            autre = build_order(restaurant, customer, reference=f"EC00000{rang}")
            Order.objects.filter(pk=autre.pk).update(status=OrderStatus.DELIVERED)
            Assignment.objects.create(order=autre, courier=courier, status=DeliveryStatus.DELIVERED)
            as_customer.post(rating_url(autre), {"score": score}, format="json")

        courier.refresh_from_db()
        assert courier.rating_count == 3
        assert courier.rating_average == Decimal("4.33")

    def test_on_ne_note_pas_deux_fois_la_meme_livraison(
        self, as_customer: APIClient, delivered: Assignment, courier: CourierProfile
    ) -> None:
        """Sans cette garde, il suffisait de rejouer la requête pour porter un
        livreur à 5/5 — ou l'y clouer."""
        as_customer.post(rating_url(delivered.order), {"score": 5}, format="json")
        response = as_customer.post(rating_url(delivered.order), {"score": 1}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

        courier.refresh_from_db()
        assert courier.rating_count == 1
        assert courier.rating_average == Decimal("5.00")

    def test_une_course_non_livree_ne_se_note_pas(
        self, as_customer: APIClient, order: Order, courier: CourierProfile
    ) -> None:
        Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.ON_THE_WAY)

        response = as_customer.post(rating_url(order), {"score": 5}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_une_commande_sans_course_ne_se_note_pas(
        self, as_customer: APIClient, order: Order
    ) -> None:
        assert (
            as_customer.post(rating_url(order), {"score": 5}, format="json").status_code
            == status.HTTP_404_NOT_FOUND
        )

    def test_le_score_reste_entre_1_et_5(
        self, as_customer: APIClient, delivered: Assignment
    ) -> None:
        for score in (0, 6, -1):
            response = as_customer.post(
                rating_url(delivered.order), {"score": score}, format="json"
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST

        assert not CourierRating.objects.exists()


class TestCloisonnement:
    def test_on_ne_note_pas_la_livraison_d_un_autre(
        self, client: APIClient, delivered: Assignment
    ) -> None:
        """404 et non 403 : l'existence de la commande d'un tiers ne se déduit
        pas de la réponse."""
        intrus = User.objects.create_user(
            "intrus@elcorazon.test", "motdepasse", full_name="Kodjo Intrus"
        )
        client.force_authenticate(intrus)

        response = client.post(rating_url(delivered.order), {"score": 1}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not CourierRating.objects.exists()

    def test_le_livreur_ne_se_note_pas_lui_meme(
        self, client: APIClient, delivered: Assignment, courier: CourierProfile
    ) -> None:
        client.force_authenticate(courier.user)

        response = client.post(rating_url(delivered.order), {"score": 5}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_la_notation_exige_un_jeton(self, client: APIClient, delivered: Assignment) -> None:
        response = client.post(rating_url(delivered.order), {"score": 5}, format="json")

        assert response.status_code in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }

    def test_le_livreur_note_est_bien_celui_de_la_course(
        self, as_customer: APIClient, delivered: Assignment, courier: CourierProfile
    ) -> None:
        """Le client ne désigne pas le livreur : il vient de la course.

        Un champ `courier` accepté en entrée aurait permis de noter n'importe
        quel livreur de la flotte depuis sa propre commande.
        """
        autre = CourierProfile.objects.create(
            user=User.objects.create_user(
                "autre.livreur@elcorazon.test",
                "motdepasse",
                full_name="Yao Autre",
                user_type=UserType.COURIER,
            ),
            restaurant=delivered.courier.restaurant,
            vehicle_type=delivered.courier.vehicle_type,
        )

        as_customer.post(
            rating_url(delivered.order), {"score": 1, "courier": str(autre.pk)}, format="json"
        )

        autre.refresh_from_db()
        courier.refresh_from_db()
        assert autre.rating_count == 0
        assert courier.rating_count == 1


class TestRelecture:
    def test_le_client_relit_sa_note(self, as_customer: APIClient, delivered: Assignment) -> None:
        as_customer.post(rating_url(delivered.order), {"score": 3}, format="json")

        response = as_customer.get(rating_url(delivered.order))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["score"] == 3
        assert str(response.data["order"]) == str(delivered.order.pk)

    def test_une_livraison_non_notee_rend_404(
        self, as_customer: APIClient, delivered: Assignment
    ) -> None:
        """C'est la réponse à « ai-je déjà noté ? », pas un incident."""
        assert as_customer.get(rating_url(delivered.order)).status_code == (
            status.HTTP_404_NOT_FOUND
        )
