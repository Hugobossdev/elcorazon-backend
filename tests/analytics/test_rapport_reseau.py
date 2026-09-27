"""Rapport réseau et audit de cohérence.

Le rapport par pays, ville, zone et cuisine lit la géographie **figée** sur la
commande, et se compose avec le périmètre du compte comme les autres rapports :
un gérant de Lomé ne lit pas le chiffre d'Abidjan en demandant `level=country`.

L'audit, lui, ne répare rien : il nomme ce que le schéma laisse passer.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import CourierProfile
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.states import OrderStatus
from apps.restaurants.models import OpeningHours, Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from tests.fixtures import LOME, build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
FENETRE = {"start": "2000-01-01", "end": "2100-01-01"}


@pytest.fixture
def abidjan(zone: DeliveryZone) -> Restaurant:
    pays = Country.objects.create(
        iso_code="CI", name="Côte d'Ivoire", currency=XOF, phone_prefix="+225", timezone="UTC"
    )
    ville = City.objects.create(country=pays, name="Abidjan", slug="abidjan", centroid=LOME)
    cocody = DeliveryZone.objects.create(
        city=ville,
        name="Cocody",
        boundary=zone.boundary,
        base_fee=Money(500, XOF),
        fee_per_km=Money(0, XOF),
    )
    return Restaurant.objects.create(
        name="Abidjan",
        slug="abidjan",
        zone=cocody,
        address="x",
        location=LOME,
        phone="+22507000002",
        status=RestaurantStatus.ACTIVE,
    )


def commande(restaurant: Restaurant, customer: User, reference: str, statut: str) -> None:
    zone = restaurant.zone
    build_order(
        restaurant,
        customer,
        reference=reference,
        status=statut,
        country=zone.city.country,
        city=zone.city,
        delivery_zone=zone,
        delivery_zone_name=zone.name,
    )


class TestRapportReseau:
    def test_le_siege_lit_chaque_pays_et_ses_statuts(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        commande(restaurant, customer, "EC400001", OrderStatus.DELIVERED)
        commande(restaurant, customer, "EC400002", OrderStatus.CANCELLED)
        commande(abidjan, customer, "EC400003", OrderStatus.PREPARING)
        siege = APIClient()
        siege.force_authenticate(User.objects.create_superuser("siege.rapport@elcorazon.test", "x"))

        reponse = siege.get(reverse("v1:analytics:report-network"), {**FENETRE, "level": "country"})

        assert reponse.status_code == status.HTTP_200_OK, reponse.data
        par_pays = {ligne["country"]: ligne for ligne in reponse.data}
        assert par_pays["TG"]["orders_count"] == 2
        assert par_pays["TG"]["delivered_count"] == 1
        assert par_pays["TG"]["cancelled_count"] == 1
        assert par_pays["TG"]["revenue_minor"] == 4_000  # livrées seulement
        assert par_pays["CI"]["in_progress_count"] == 1
        assert par_pays["CI"]["revenue_minor"] == 0

    def test_un_gerant_ne_lit_que_son_perimetre(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        commande(restaurant, customer, "EC400004", OrderStatus.DELIVERED)
        commande(abidjan, customer, "EC400005", OrderStatus.DELIVERED)
        gerant = User.objects.create_user(
            "gerant.rapport@elcorazon.test", "x", full_name="G", user_type=UserType.STAFF
        )
        gerant.roles.add(Role.objects.create(name="Rapports", permissions=["analytics.read"]))
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(gerant)

        reponse = client.get(
            reverse("v1:analytics:report-network"), {**FENETRE, "level": "kitchen", "country": "CI"}
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data == []

    def test_le_filtre_de_zone_et_l_export(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        commande(restaurant, customer, "EC400006", OrderStatus.DELIVERED)
        commande(abidjan, customer, "EC400007", OrderStatus.DELIVERED)
        siege = APIClient()
        siege.force_authenticate(User.objects.create_superuser("siege.zone@elcorazon.test", "x"))

        reponse = siege.get(
            reverse("v1:analytics:report-network"),
            {**FENETRE, "level": "zone", "zone": str(abidjan.zone_id)},
        )
        export = siege.get(
            reverse("v1:analytics:report-network"), {**FENETRE, "level": "city", "export": "csv"}
        )

        assert [ligne["name"] for ligne in reponse.data] == ["Cocody"]
        assert export.status_code == status.HTTP_200_OK
        assert "orders_count" in export.content.decode("utf-8")


class TestAuditReseau:
    def test_un_reseau_sain_ne_signale_rien(self, restaurant: Restaurant) -> None:
        sortie = io.StringIO()
        call_command("audit_reseau", "--strict", stdout=sortie)
        assert "Aucune anomalie." in sortie.getvalue()

    def test_les_anomalies_sont_nommees_et_rien_n_est_ecrit(
        self, restaurant: Restaurant, city: City, customer: User, courier: CourierProfile
    ) -> None:
        OpeningHours.objects.filter(restaurant=restaurant).delete()
        kara = City.objects.create(country=city.country, name="Kara", slug="kara", centroid=LOME)
        ailleurs = DeliveryZone.objects.create(
            city=kara,
            name="Kara centre",
            boundary=restaurant.zone.boundary,
            base_fee=Money(500, XOF),
            fee_per_km=Money(0, XOF),
        )
        courier.service_zones.add(ailleurs)  # contourne la garde du service, exprès
        build_order(restaurant, customer, reference="EC400010")

        sortie = io.StringIO()
        with pytest.raises(CommandError):
            call_command("audit_reseau", "--strict", stdout=sortie)

        rapport = sortie.getvalue()
        assert "Cuisine en service sans horaires" in rapport
        assert "Livreur affecté hors de la desserte de sa cuisine" in rapport
        assert "Commandes sans géographie figée" in rapport
        assert courier.service_zones.count() == 1  # rien n'a été « réparé »
