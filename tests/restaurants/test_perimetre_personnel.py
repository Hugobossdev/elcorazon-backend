"""Où travaille un membre du personnel, et ce qu'il y règle.

Deux défauts constatés le 21 septembre 2026, et leur frontière :

* **le périmètre** — le back-office lisait les établissements d'un compte sur
  `/restaurants/manage/`, qui exige `restaurants.read`. Le rôle « Opérateur »
  ne l'a pas : il ouvrait le poste de cuisine et lisait « Aucun établissement
  rattaché ». `/restaurants/manage/perimeter/` répond à tout le personnel, sous
  une forme réduite, et dans le seul périmètre du compte ;
* **l'exploitation courante** — horaires, fermetures et zones propres sont
  décrits par le serveur comme le geste du gérant, mais le rôle Manager n'avait
  que `restaurants.read`. Lui donner `restaurants.write` aurait aussi ouvert la
  fiche de l'établissement, dont le plafond des pertes de stock qui encadre ses
  propres écritures. `restaurants.operate` couvre les trois ressources, et rien
  d'autre : c'est ce que ces tests vérifient dans les deux sens.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.accounts.permissions import SYSTEM_ROLES
from apps.geography.models import DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, RestaurantStatus, StaffMembership

pytestmark = pytest.mark.django_db

PERIMETRE = "v1:restaurants:managed-restaurant-perimeter"


@pytest.fixture
def autre_restaurant(zone: DeliveryZone) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Bè",
        slug="el-corazon-be",
        zone=zone,
        address="Bè",
        location=Point(1.24, 6.14, srid=4326),
        phone="+22890000001",
        status=RestaurantStatus.ACTIVE,
    )


def _membre(email: str, role: str, restaurant: Restaurant | None) -> APIClient:
    user = User.objects.create_user(email, "motdepasse", full_name=role, user_type=UserType.STAFF)
    user.roles.add(
        Role.objects.create(name=f"{role} {email}", permissions=list(SYSTEM_ROLES[role]))
    )
    if restaurant is not None:
        StaffMembership.objects.create(user=user, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(user)
    return client


class TestPerimetre:
    def test_un_operateur_lit_son_etablissement_sans_restaurants_read(
        self, restaurant: Restaurant, autre_restaurant: Restaurant
    ) -> None:
        operateur = _membre("cuisine@elcorazon.test", "Opérateur", restaurant)

        # Le refus de la liste de gestion est celui d'avant, et il reste :
        # l'Opérateur n'administre pas le réseau.
        assert (
            operateur.get(reverse("v1:restaurants:managed-restaurant-list")).status_code
            == status.HTTP_403_FORBIDDEN
        )

        response = operateur.get(reverse(PERIMETRE))

        assert response.status_code == status.HTTP_200_OK
        assert [e["slug"] for e in response.data] == [restaurant.slug]

    def test_la_forme_ne_porte_que_l_identite_operationnelle(self, restaurant: Restaurant) -> None:
        operateur = _membre("cuisine@elcorazon.test", "Opérateur", restaurant)

        fiche = operateur.get(reverse(PERIMETRE)).data[0]

        for champ in ("slug", "name", "currency", "timezone", "location", "zone"):
            assert champ in fiche
        # Ce que `restaurants.read` continue de réserver à qui gère.
        for champ in ("stock_adjustment_ceiling", "orders_count", "configuration_gaps"):
            assert champ not in fiche

    def test_un_compte_non_rattache_n_a_aucun_etablissement(self, restaurant: Restaurant) -> None:
        orphelin = _membre("orphelin@elcorazon.test", "Opérateur", None)

        response = orphelin.get(reverse(PERIMETRE))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == []

    def test_le_siege_voit_tout_le_reseau_en_service_d_abord(
        self, restaurant: Restaurant, autre_restaurant: Restaurant
    ) -> None:
        autre_restaurant.status = RestaurantStatus.INACTIVE
        autre_restaurant.save()
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
        client = APIClient()
        client.force_authenticate(siege)

        slugs = [e["slug"] for e in client.get(reverse(PERIMETRE)).data]

        assert slugs == [restaurant.slug, autre_restaurant.slug]

    def test_un_client_n_a_pas_de_perimetre(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        assert client.get(reverse(PERIMETRE)).status_code == status.HTTP_403_FORBIDDEN


class TestExploitationCourante:
    def _plage(self, restaurant: Restaurant) -> dict[str, object]:
        return {
            "restaurant": str(restaurant.pk),
            "weekday": 1,
            "opens_at": "08:00",
            "closes_at": "10:00",
        }

    def test_le_gerant_regle_les_horaires_de_sa_cuisine(self, restaurant: Restaurant) -> None:
        gerant = _membre("gerant@elcorazon.test", "Manager", restaurant)

        response = gerant.post(
            reverse("v1:restaurants:managed-opening-hours-list"),
            self._plage(restaurant),
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_le_gerant_ne_regle_pas_ceux_d_une_autre_cuisine(
        self, restaurant: Restaurant, autre_restaurant: Restaurant
    ) -> None:
        gerant = _membre("gerant@elcorazon.test", "Manager", restaurant)
        avant = OpeningHours.objects.filter(restaurant=autre_restaurant).count()

        response = gerant.post(
            reverse("v1:restaurants:managed-opening-hours-list"),
            self._plage(autre_restaurant),
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert OpeningHours.objects.filter(restaurant=autre_restaurant).count() == avant

    def test_le_gerant_declare_une_fermeture_exceptionnelle(self, restaurant: Restaurant) -> None:
        gerant = _membre("gerant@elcorazon.test", "Manager", restaurant)
        debut = timezone.now() + dt.timedelta(hours=1)

        response = gerant.post(
            reverse("v1:restaurants:managed-closure-list"),
            {
                "restaurant": str(restaurant.pk),
                "starts_at": debut.isoformat(),
                "ends_at": (debut + dt.timedelta(hours=2)).isoformat(),
                "reason": "Coupure d'électricité",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_le_gerant_ne_touche_pas_au_plafond_des_pertes(self, restaurant: Restaurant) -> None:
        """Le plafond encadre ses propres déclarations de perte : le lui ouvrir
        supprimerait le contrôle à quatre yeux de l'inventaire."""
        gerant = _membre("gerant@elcorazon.test", "Manager", restaurant)

        response = gerant.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"stock_adjustment_ceiling": {"amount": "1000000", "currency": "XOF"}},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        restaurant.refresh_from_db()
        assert restaurant.stock_adjustment_ceiling is None

    def test_l_operateur_ne_regle_pas_les_horaires(self, restaurant: Restaurant) -> None:
        operateur = _membre("cuisine@elcorazon.test", "Opérateur", restaurant)

        response = operateur.post(
            reverse("v1:restaurants:managed-opening-hours-list"),
            self._plage(restaurant),
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_le_manager_n_a_pas_restaurants_write(self) -> None:
        assert "restaurants.operate" in SYSTEM_ROLES["Manager"]
        assert "restaurants.write" not in SYSTEM_ROLES["Manager"]
        assert "restaurants.operate" not in SYSTEM_ROLES["Opérateur"]
