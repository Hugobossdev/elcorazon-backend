"""Moyens de paiement acceptés — une seule règle, tenue par le serveur.

L'application client désactivait en dur mobile money et carte (« bientôt »)
pendant que le panier collaboratif payait en mobile money, et que le serveur
acceptait les quatre moyens sans distinction. Trois règles pour une seule
plateforme. Le réglage `PAYMENT_METHODS` les remplace : le serveur publie la
liste (`GET /payments/methods/`) et refuse à la création ce qu'il ne publie pas.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.carts.services import CartService
from apps.catalog.models import MenuItem
from apps.orders.models import Order, PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def garni(customer: User, restaurant: Restaurant, menu_item: MenuItem) -> None:
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])


def commander(client: APIClient, restaurant: Restaurant, address: Address, moyen: str) -> object:
    return client.post(
        reverse("v1:orders:order-list"),
        {"restaurant": restaurant.slug, "address": str(address.pk), "payment_method": moyen},
        format="json",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


class TestPublication:
    def test_la_liste_suit_le_reglage_et_son_ordre(self, settings: object) -> None:
        settings.PAYMENT_METHODS = ["cash", "mobile_money"]  # type: ignore[attr-defined]

        response = APIClient().get(reverse("v1:payments:methods"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == [
            {"code": "cash", "label": PaymentMethod.CASH.label},
            {"code": "mobile_money", "label": PaymentMethod.MOBILE_MONEY.label},
        ]

    def test_le_portefeuille_n_est_pas_publie_par_defaut(self) -> None:
        """Abandonné côté produit, sans encaissement réel derrière lui."""
        codes = [m["code"] for m in APIClient().get(reverse("v1:payments:methods")).data]

        assert "wallet" not in codes
        assert codes == ["mobile_money", "card", "cash"]


class TestCreation:
    def test_un_moyen_non_accepte_est_refuse_sans_rien_creer(
        self,
        settings: object,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        garni: None,
    ) -> None:
        settings.PAYMENT_METHODS = ["cash"]  # type: ignore[attr-defined]

        response = commander(as_customer, restaurant, address, PaymentMethod.MOBILE_MONEY)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "Mobile Money" in response.data["detail"]
        assert Order.objects.count() == 0

    def test_un_moyen_accepte_passe(
        self,
        settings: object,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        garni: None,
    ) -> None:
        settings.PAYMENT_METHODS = ["cash"]  # type: ignore[attr-defined]

        response = commander(as_customer, restaurant, address, PaymentMethod.CASH)

        assert response.status_code == status.HTTP_201_CREATED
