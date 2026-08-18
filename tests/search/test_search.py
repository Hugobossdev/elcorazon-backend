"""Recherche transverse — `/search/?q=…`.

Les deux tests décisifs sont dans `TestDroits` et `TestPerimetre` : ce sont les
deux garde-fous que l'implémentation précédente n'avait pas. Elle interrogeait
quatre tables depuis le navigateur, sans vérifier ni la permission ni le
rattachement — un opérateur de Kara y trouvait les commandes de Lomé, et un
compte privé de `customers.read` y lisait des numéros de téléphone.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import MenuItem
from apps.delivery.models import CourierProfile
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

URL = "v1:search:search"


def staff(email: str, permissions: list[str], restaurant: Restaurant | None = None) -> APIClient:
    membre = User.objects.create_user(
        email, "motdepasse", full_name="Agent", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=permissions))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


def kinds(response: object) -> set[str]:
    return {ligne["kind"] for ligne in response.data}  # type: ignore[attr-defined]


class TestRecherche:
    def test_une_commande_se_retrouve_par_sa_reference(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        build_order(restaurant, customer, reference="EC000042")
        client = staff("cmd@elcorazon.test", ["orders.read"], restaurant)

        response = client.get(reverse(URL), {"q": "EC000042"})

        assert response.status_code == status.HTTP_200_OK
        assert [ligne["title"] for ligne in response.data] == ["EC000042"]

    def test_un_client_se_retrouve_par_son_telephone(self, customer: User) -> None:
        customer.phone = "+22890111222"
        customer.save(update_fields=["phone"])
        client = staff("cli@elcorazon.test", ["customers.read"])

        response = client.get(reverse(URL), {"q": "90111222"})

        assert [ligne["kind"] for ligne in response.data] == ["customer"]

    def test_un_article_se_retrouve_par_son_nom(
        self, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        client = staff("carte@elcorazon.test", ["catalog.read"], restaurant)

        response = client.get(reverse(URL), {"q": menu_item.name[:5]})

        assert [ligne["kind"] for ligne in response.data] == ["menu_item"]

    def test_une_requete_trop_courte_ne_ramene_rien(self, customer: User) -> None:
        """Deux caractères ramèneraient une part notable de chaque table sans
        rien désigner : c'est un refus, pas une optimisation."""
        client = staff("court@elcorazon.test", ["customers.read"])

        response = client.get(reverse(URL), {"q": "ab"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_plusieurs_familles_repondent_au_meme_terme(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        customer.full_name = "Awa Komlan"
        customer.save(update_fields=["full_name"])
        build_order(restaurant, customer, reference="EC000043", recipient_name="Awa Komlan")
        client = staff("tout@elcorazon.test", ["orders.read", "customers.read"], restaurant)

        response = client.get(reverse(URL), {"q": "Awa"})

        assert kinds(response) == {"order", "customer"}


class TestDroits:
    def test_une_famille_sans_permission_est_absente(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        """Absente, et non vide : « rien trouvé » et « pas le droit » ne se
        corrigent pas de la même façon."""
        customer.full_name = "Awa Komlan"
        customer.save(update_fields=["full_name"])
        build_order(restaurant, customer, reference="EC000044", recipient_name="Awa Komlan")
        client = staff("cmdseul@elcorazon.test", ["orders.read"], restaurant)

        response = client.get(reverse(URL), {"q": "Awa"})

        assert kinds(response) == {"order"}

    def test_un_compte_sans_aucune_permission_ne_trouve_rien(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        build_order(restaurant, customer, reference="EC000045")
        client = staff("rien@elcorazon.test", [], restaurant)

        response = client.get(reverse(URL), {"q": "EC000045"})

        assert response.status_code == status.HTTP_200_OK
        assert response.data == []

    def test_un_client_n_accede_pas_a_la_recherche(self, customer: User) -> None:
        """Elle traverse les familles du back-office ; un client a sa carte et
        ses commandes."""
        client = APIClient()
        client.force_authenticate(customer)

        response = client.get(reverse(URL), {"q": "burger"})

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_livreur_non_plus(self, courier_user: User) -> None:
        client = APIClient()
        client.force_authenticate(courier_user)

        response = client.get(reverse(URL), {"q": "burger"})

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestPerimetre:
    @pytest.fixture
    def autre_restaurant(self, restaurant: Restaurant) -> Restaurant:
        return Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=Point(1.19, 9.55, srid=4326),
            phone="+22890000003",
        )

    def test_une_commande_hors_perimetre_est_introuvable(
        self, restaurant: Restaurant, autre_restaurant: Restaurant, customer: User
    ) -> None:
        build_order(autre_restaurant, customer, reference="EC000046")
        client = staff("lome@elcorazon.test", ["orders.read"], restaurant)

        response = client.get(reverse(URL), {"q": "EC000046"})

        assert response.data == []

    def test_un_livreur_hors_perimetre_est_introuvable(
        self, restaurant: Restaurant, autre_restaurant: Restaurant
    ) -> None:
        etranger = User.objects.create_user(
            "kossi@elcorazon.test", "motdepasse", full_name="Kossi Ali", user_type=UserType.COURIER
        )
        CourierProfile.objects.create(
            user=etranger, restaurant=autre_restaurant, vehicle_type="motorcycle"
        )
        client = staff("flotte@elcorazon.test", ["couriers.read"], restaurant)

        response = client.get(reverse(URL), {"q": "Kossi"})

        assert response.data == []

    def test_le_siege_voit_tous_les_etablissements(
        self, autre_restaurant: Restaurant, customer: User
    ) -> None:
        build_order(autre_restaurant, customer, reference="EC000047")
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse-siege")
        client = APIClient()
        client.force_authenticate(siege)

        response = client.get(reverse(URL), {"q": "EC000047"})

        assert [ligne["title"] for ligne in response.data] == ["EC000047"]
