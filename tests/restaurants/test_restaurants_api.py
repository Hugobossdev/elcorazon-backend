"""API des établissements — ADR-006, ADR-009."""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status
from rest_framework.test import APIClient

from apps.geography.models import DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, Weekday

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

# Mardi 28 juillet 2026, 12 h UTC — Lomé est à UTC+0 toute l'année.
MARDI_MIDI = "2026-07-28 12:00:00+00:00"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def ouvert_le_mardi(restaurant: Restaurant) -> Restaurant:
    OpeningHours.objects.create(
        restaurant=restaurant,
        weekday=Weekday.TUESDAY,
        opens_at=dt.time(11, 0),
        closes_at=dt.time(23, 0),
    )
    return restaurant


class TestListe:
    def test_lisible_sans_compte(self, client: APIClient, restaurant: Restaurant) -> None:
        response = client.get(reverse("v1:restaurants:restaurant-list"))

        assert response.status_code == status.HTTP_200_OK
        assert [r["slug"] for r in response.data["results"]] == [restaurant.slug]

    def test_un_etablissement_inactif_disparait(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        Restaurant.objects.filter(pk=restaurant.pk).update(is_active=False)

        assert client.get(reverse("v1:restaurants:restaurant-list")).data["count"] == 0

    def test_les_frais_et_le_delai_viennent_de_la_zone(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        """Le client doit pouvoir annoncer un prix de livraison avant même
        d'avoir une adresse : c'est le barème de la zone du restaurant."""
        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["delivery_fee_from"] == {"amount": "500", "currency": "XOF"}
        assert fiche["estimated_delivery_minutes"] == 30
        assert fiche["currency"] == "XOF"

    def test_la_position_sort_nommee(self, client: APIClient, restaurant: Restaurant) -> None:
        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["location"] == {"lat": pytest.approx(6.1319), "lon": pytest.approx(1.2255)}


class TestProximite:
    @pytest.fixture
    def deux_etablissements(self, zone: DeliveryZone, restaurant: Restaurant) -> Restaurant:
        """Un second établissement environ 2 km à l'est du premier."""
        return Restaurant.objects.create(
            name="El Corazón Est",
            slug="el-corazon-est",
            zone=zone,
            address="Est de Lomé",
            location=Point(1.2455, 6.1319, srid=4326),
            phone="+22890000001",
        )

    def test_le_tri_par_distance_est_fait_par_postgis(
        self, client: APIClient, deux_etablissements: Restaurant, restaurant: Restaurant
    ) -> None:
        """Sur l'ellipsoïde, en mètres, servi par l'index GiST — pas en Python
        après avoir tout chargé."""
        response = client.get(
            reverse("v1:restaurants:restaurant-list"), {"lat": 6.1319, "lon": 1.2455}
        )

        assert [r["slug"] for r in response.data["results"]] == [
            deux_etablissements.slug,
            restaurant.slug,
        ]

    def test_la_distance_est_annoncee_en_metres(
        self, client: APIClient, deux_etablissements: Restaurant
    ) -> None:
        response = client.get(
            reverse("v1:restaurants:restaurant-list"), {"lat": 6.1319, "lon": 1.2255}
        )
        premier, second = response.data["results"]

        assert premier["distance_m"] == pytest.approx(0, abs=1)
        assert 2_000 < second["distance_m"] < 2_500

    def test_sans_point_de_reference_la_distance_reste_nulle(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        """Un `0` inventé ferait croire à une proximité qu'on n'a pas
        mesurée."""
        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["distance_m"] is None

    def test_une_coordonnee_seule_est_refusee(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        """Une latitude sans longitude produirait un tri à une dimension :
        faux, mais plausible à la lecture."""
        response = client.get(reverse("v1:restaurants:restaurant-list"), {"lat": 6.1319})

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestOuverture:
    @freeze_time(MARDI_MIDI)
    def test_ouvert_pendant_le_service(
        self, client: APIClient, ouvert_le_mardi: Restaurant
    ) -> None:
        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["is_open"] is True
        assert fiche["can_order_now"] is True

    @freeze_time("2026-07-28 06:00:00+00:00")
    def test_ferme_hors_service(self, client: APIClient, ouvert_le_mardi: Restaurant) -> None:
        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["is_open"] is False
        assert fiche["can_order_now"] is False

    @freeze_time(MARDI_MIDI)
    def test_ouvert_mais_ne_prenant_plus_de_commande(
        self, client: APIClient, ouvert_le_mardi: Restaurant
    ) -> None:
        """Les trois booléens disent trois choses : « fermé, ouvre à 11 h » et
        « débordé, réessayez » n'appellent pas le même geste du client."""
        Restaurant.objects.filter(pk=ouvert_le_mardi.pk).update(accepts_orders=False)

        fiche = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["is_open"] is True
        assert fiche["accepts_orders"] is False
        assert fiche["can_order_now"] is False


class TestFiche:
    def test_la_fiche_porte_les_horaires_que_la_liste_omet(
        self, client: APIClient, ouvert_le_mardi: Restaurant
    ) -> None:
        """Sept plages par établissement multiplieraient par sept une réponse
        de liste que personne ne lit."""
        liste = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0]
        fiche = client.get(
            reverse("v1:restaurants:restaurant-detail", args=[ouvert_le_mardi.slug])
        ).data

        assert "opening_hours" not in liste
        assert fiche["opening_hours"] == [
            {
                "id": str(ouvert_le_mardi.opening_hours.get().pk),
                "weekday": Weekday.TUESDAY,
                "opens_at": "11:00:00",
                "closes_at": "23:00:00",
                "crosses_midnight": False,
            }
        ]

    def test_la_fiche_se_lit_par_slug(self, client: APIClient, restaurant: Restaurant) -> None:
        """Un slug est partageable et lisible dans un lien ; un UUID ne
        l'est pas."""
        response = client.get(reverse("v1:restaurants:restaurant-detail", args=[restaurant.slug]))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == str(restaurant.pk)


class TestFiltres:
    def test_par_ville(self, client: APIClient, restaurant: Restaurant) -> None:
        response = client.get(
            reverse("v1:restaurants:restaurant-list"), {"zone__city__slug": "lome"}
        )
        assert response.data["count"] == 1

        response = client.get(
            reverse("v1:restaurants:restaurant-list"), {"zone__city__slug": "kara"}
        )
        assert response.data["count"] == 0
