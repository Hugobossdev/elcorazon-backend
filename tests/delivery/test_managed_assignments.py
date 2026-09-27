"""Courses vues par l'exploitation — `/delivery/manage/assignments/`.

Cette route comble un manque qui ne se voyait pas côté serveur, mais qui vidait
trois écrans du back-office : **rien ne disait au personnel qui porte une
commande**. `AssignmentSerializer` annonce une course « vue par le livreur ou
par le personnel », et pourtant la seule route qui la rendait
(`AssignmentViewSet`) est sous `IsCourier` et filtre sur le dossier de
l'appelant — un membre du personnel y recevait donc une liste vide, jamais une
erreur, ce qui est la pire des deux réponses.

Ce qui est vérifié ici est le cloisonnement, parce que c'est ce qu'une route de
lecture nouvelle peut casser : une course appartient à l'établissement de sa
commande, et un opérateur d'une autre enseigne ne doit pas la trouver.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import Assignment, CourierProfile
from apps.orders.models import Order
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def _superviseur(restaurant: Restaurant | None, email: str) -> User:
    """Compte du personnel muni de `orders.read`, rattaché ou non."""
    membre = User.objects.create_user(
        email, "motdepasse", full_name="Kossi Supervision", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Supervision {email}", permissions=["orders.read"]))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


@pytest.fixture
def course(order: Order, courier: CourierProfile) -> Assignment:
    return Assignment.objects.create(order=order, courier=courier)


@pytest.fixture
def as_superviseur(restaurant: Restaurant) -> APIClient:
    client = APIClient()
    client.force_authenticate(_superviseur(restaurant, "supervision@elcorazon.test"))
    return client


class TestLecture:
    def test_la_course_dit_qui_porte_la_commande(
        self, as_superviseur: APIClient, course: Assignment
    ) -> None:
        """Le seul point de cette route : le nom du porteur, et l'identifiant
        de la commande qui permet de les rapprocher côté écran."""
        reponse = as_superviseur.get(reverse("v1:delivery:managed-assignment-list"))

        assert reponse.status_code == status.HTTP_200_OK
        (ligne,) = reponse.data["results"]
        assert str(ligne["order"]) == str(course.order_id)
        assert str(ligne["courier"]["id"]) == str(course.courier_id)
        assert ligne["courier"]["full_name"] == course.courier.user.full_name

    def test_le_filtre_par_commande_repond_a_la_question_de_l_ecran(
        self, as_superviseur: APIClient, course: Assignment
    ) -> None:
        """« Qui porte *cette* commande » — la fiche d'une commande n'a pas à
        télécharger toutes les courses pour le savoir."""
        reponse = as_superviseur.get(
            reverse("v1:delivery:managed-assignment-list"),
            {"order": str(course.order_id)},
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert [str(ligne["id"]) for ligne in reponse.data["results"]] == [str(course.id)]

    def test_la_route_est_en_lecture_seule(
        self, as_superviseur: APIClient, course: Assignment
    ) -> None:
        """Affecter reste `orders.assign_courier`, sur sa propre route : lire
        qui porte une commande ne doit pas donner de quoi en changer."""
        reponse = as_superviseur.post(
            reverse("v1:delivery:managed-assignment-list"), {"order": str(course.order_id)}
        )

        assert reponse.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestCloisonnement:
    def test_un_operateur_d_une_autre_enseigne_ne_trouve_rien(self, course: Assignment) -> None:
        """Une course appartient à l'établissement de sa commande. Introuvable
        et non interdite, comme la commande elle-même."""
        autre = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=course.order.restaurant.zone,
            address="Kara centre",
            phone="+22890000002",
            location=course.order.restaurant.location,
        )
        client = APIClient()
        client.force_authenticate(_superviseur(autre, "kara@elcorazon.test"))

        reponse = client.get(reverse("v1:delivery:managed-assignment-list"))

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["results"] == []

    def test_sans_orders_read_la_route_est_fermee(self, course: Assignment) -> None:
        """La permission garde bien la route — un compte du personnel n'y a pas
        droit du seul fait d'être du personnel."""
        sans_droit = User.objects.create_user(
            "sansdroit@elcorazon.test",
            "motdepasse",
            full_name="Sans Droit",
            user_type=UserType.STAFF,
        )
        StaffMembership.objects.create(user=sans_droit, restaurant=course.order.restaurant)
        client = APIClient()
        client.force_authenticate(sans_droit)

        reponse = client.get(reverse("v1:delivery:managed-assignment-list"))

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_un_client_n_y_a_pas_acces(self, course: Assignment, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        reponse = client.get(reverse("v1:delivery:managed-assignment-list"))

        assert reponse.status_code == status.HTTP_403_FORBIDDEN
