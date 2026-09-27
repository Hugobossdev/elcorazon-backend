"""Rapports sur un périmètre à deux devises — XOF et XAF.

Le 21 septembre 2026, le tableau de bord du siège affichait un « chiffre
d'affaires du jour » qui additionnait les commandes de Lomé (XOF) et de Douala
(XAF). Les deux francs CFA sont à parité, mais ce sont deux monnaies : l'une
n'a pas cours au Cameroun, l'autre pas au Togo, et une comptabilité qui les
mêle ne se rapproche d'aucun relevé bancaire. Le rapport réseau faisait déjà
une ligne par devise ; les autres non.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, RestaurantStatus
from common.money import Money
from tests.fixtures import LOME, build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XAF = "XAF"
FENETRE = {"start": "2000-01-01", "end": "2100-01-01"}


@pytest.fixture
def douala(zone: DeliveryZone) -> Restaurant:
    pays = Country.objects.create(
        iso_code="CM", name="Cameroun", currency=XAF, phone_prefix="+237", timezone="UTC"
    )
    ville = City.objects.create(country=pays, name="Douala", slug="douala", centroid=LOME)
    akwa = DeliveryZone.objects.create(
        city=ville,
        name="Akwa",
        boundary=zone.boundary,
        base_fee=Money(500, XAF),
        fee_per_km=Money(0, XAF),
    )
    return Restaurant.objects.create(
        name="Douala",
        slug="douala",
        zone=akwa,
        address="Akwa",
        location=LOME,
        phone="+237600000000",
        status=RestaurantStatus.ACTIVE,
    )


def livree(restaurant: Restaurant, customer: User, reference: str, total: int) -> None:
    devise = restaurant.zone.city.country.currency
    build_order(
        restaurant,
        customer,
        reference=reference,
        status=OrderStatus.DELIVERED,
        delivered_at=timezone.now() - dt.timedelta(hours=1),
        subtotal=Money(total - 500, devise),
        delivery_fee=Money(500, devise),
        discount=Money(0, devise),
        total=Money(total, devise),
    )


@pytest.fixture
def siege() -> APIClient:
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege.devises@elcorazon.test", "x"))
    return client


class TestAperçuADeuxDevises:
    def test_le_chiffre_d_affaires_n_additionne_pas_xof_et_xaf(
        self, restaurant: Restaurant, douala: Restaurant, customer: User, siege: APIClient
    ) -> None:
        livree(restaurant, customer, "EC500001", 4_000)
        livree(restaurant, customer, "EC500002", 2_000)
        livree(douala, customer, "EC500003", 10_000)

        reponse = siege.get(reverse("v1:analytics:report-overview"), FENETRE)

        assert reponse.status_code == status.HTTP_200_OK
        # Pas de total : il mêlerait deux monnaies.
        assert reponse.data["revenue_minor"] is None
        assert reponse.data["average_basket_minor"] is None
        assert reponse.data["currency"] is None
        par_devise = {ligne["currency"]: ligne for ligne in reponse.data["revenues"]}
        assert par_devise["XOF"]["revenue_minor"] == 6_000
        assert par_devise["XOF"]["average_basket_minor"] == 3_000
        assert par_devise["XAF"]["revenue_minor"] == 10_000
        assert par_devise["XAF"]["orders_delivered"] == 1
        # La devise dominante vient en tête.
        assert reponse.data["revenues"][0]["currency"] == "XAF"
        # Les comptes, eux, s'additionnent sans difficulté.
        assert reponse.data["orders_delivered"] == 3

    def test_une_seule_devise_garde_son_total(
        self, restaurant: Restaurant, customer: User, siege: APIClient
    ) -> None:
        livree(restaurant, customer, "EC500004", 4_000)

        reponse = siege.get(reverse("v1:analytics:report-overview"), FENETRE)

        assert reponse.data["revenue_minor"] == 4_000
        assert reponse.data["currency"] == "XOF"
        assert [ligne["currency"] for ligne in reponse.data["revenues"]] == ["XOF"]


class TestSeriesADeuxDevises:
    def test_le_chiffre_d_affaires_quotidien_porte_sa_devise(
        self, restaurant: Restaurant, douala: Restaurant, customer: User, siege: APIClient
    ) -> None:
        livree(restaurant, customer, "EC500005", 4_000)
        livree(douala, customer, "EC500006", 10_000)

        reponse = siege.get(reverse("v1:analytics:report-revenue"), FENETRE)

        assert reponse.status_code == status.HTTP_200_OK
        assert sorted((ligne["currency"], ligne["revenue_minor"]) for ligne in reponse.data) == [
            ("XAF", 10_000),
            ("XOF", 4_000),
        ]

    def test_la_repartition_par_statut_ne_porte_plus_de_montant(
        self, restaurant: Restaurant, douala: Restaurant, customer: User, siege: APIClient
    ) -> None:
        livree(restaurant, customer, "EC500007", 4_000)
        livree(douala, customer, "EC500008", 10_000)

        reponse = siege.get(reverse("v1:analytics:report-orders"), FENETRE)

        ligne = next(ligne for ligne in reponse.data if ligne["status"] == OrderStatus.DELIVERED)
        assert ligne["orders_count"] == 2
        assert "revenue_minor" not in ligne
