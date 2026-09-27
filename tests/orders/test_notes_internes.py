"""Notes internes — sur une commande, sur un client.

Le cahier des charges les demande (§4.2.4, §4.2.6) et l'état des
fonctionnalités cochait celles des commandes : il n'en existait aucune. Le
test décisif est `test_le_client_ne_lit_jamais_une_note_interne` : une note qui
fuirait dans la réponse que lit le client deviendrait un message qu'on ne
voulait pas lui adresser.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.orders.models import Order
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


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


class TestNotesDeCommande:
    def test_une_note_s_ajoute_et_se_relit_avec_son_auteur(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        agent = personnel("agent@elcorazon.test", restaurant, "orders.read")
        url = reverse("v1:orders:managed-order-notes", args=[order.pk])

        cree = connecte(agent).post(url, {"content": "  Client rappelé, attend un geste.  "})
        lues = connecte(agent).get(url)

        assert cree.status_code == status.HTTP_201_CREATED
        assert cree.data["content"] == "Client rappelé, attend un geste."
        assert [n["author_name"] for n in lues.data] == [agent.full_name]

    def test_une_note_vide_est_refusee(self, restaurant: Restaurant, order: Order) -> None:
        agent = personnel("agent@elcorazon.test", restaurant, "orders.read")

        response = connecte(agent).post(
            reverse("v1:orders:managed-order-notes", args=[order.pk]), {"content": "   "}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_autre_cuisine_ne_les_lit_pas(self, restaurant: Restaurant, order: Order) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000009",
        )
        kara = personnel("kara@elcorazon.test", ailleurs, "orders.read")

        response = connecte(kara).get(reverse("v1:orders:managed-order-notes", args=[order.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_le_client_ne_lit_jamais_une_note_interne(
        self, restaurant: Restaurant, order: Order, customer: User
    ) -> None:
        agent = personnel("agent@elcorazon.test", restaurant, "orders.read")
        connecte(agent).post(
            reverse("v1:orders:managed-order-notes", args=[order.pk]),
            {"content": "Client difficile, rester ferme."},
        )

        vue_client = connecte(customer).get(reverse("v1:orders:order-detail", args=[order.pk]))

        assert vue_client.status_code == status.HTTP_200_OK
        assert "rester ferme" not in str(vue_client.data)


class TestNotesClient:
    def test_une_note_sur_un_client_se_relit(self, restaurant: Restaurant, customer: User) -> None:
        agent = personnel("agent@elcorazon.test", restaurant, "customers.read")
        url = reverse("v1:administration:customer-notes", args=[customer.pk])

        connecte(agent).post(url, {"content": "Litige ouvert sur EC000042."})
        lues = connecte(agent).get(url)

        assert [n["content"] for n in lues.data] == ["Litige ouvert sur EC000042."]

    def test_sans_customers_read_elles_sont_fermees(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        cuisinier = personnel("cuisine@elcorazon.test", restaurant, "orders.read")

        response = connecte(cuisinier).get(
            reverse("v1:administration:customer-notes", args=[customer.pk])
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
