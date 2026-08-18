"""Fiche chiffrée d'un client — `/analytics/reports/customers/{id}/`.

Le test décisif est `test_les_commandes_annulees_ne_comptent_pas` : c'est ce que
l'implémentation précédente faisait mal. Le service client y additionnait, côté
navigateur, les commandes que la pagination avait bien voulu rendre — toutes
confondues — si bien qu'un compte qui annulait tout ressortait à forte valeur.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.analytics.reports import ReportingService
from apps.loyalty.models import PointsAccount
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"


def deliver(order: Order) -> Order:
    for cible in (
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.PICKED_UP,
        OrderStatus.ON_THE_WAY,
        OrderStatus.DELIVERED,
    ):
        OrderService.transition_to(order=order, target=cible)
    order.refresh_from_db()
    return order


def commande(restaurant: Restaurant, customer: User, rang: int, montant: int) -> Order:
    return build_order(
        restaurant,
        customer,
        reference=f"EC00000{rang}",
        subtotal=Money(montant - 500, XOF),
        delivery_fee=Money(500, XOF),
        total=Money(montant, XOF),
    )


def stats_url(customer: User) -> str:
    return reverse("v1:analytics:report-customer", kwargs={"pk": customer.pk})


@pytest.fixture
def as_agent() -> APIClient:
    """Service client : consulte les dossiers, sans droit d'analyse ni de blocage."""
    agent = User.objects.create_user(
        "agent@elcorazon.test", "motdepasse", full_name="Agent", user_type=UserType.STAFF
    )
    agent.roles.add(Role.objects.create(name="Service client", permissions=["customers.read"]))
    client = APIClient()
    client.force_authenticate(agent)
    return client


class TestAgregat:
    def test_un_client_sans_commande_rend_des_zeros(
        self, as_agent: APIClient, customer: User
    ) -> None:
        """Et non une erreur : un compte tout juste créé est un dossier valide."""
        response = as_agent.get(stats_url(customer))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["orders_count"] == 0
        assert response.data["total_spent"] == {"amount": "0", "currency": XOF}
        assert response.data["last_order_at"] is None

    def test_le_total_et_le_panier_moyen_portent_sur_les_livraisons(
        self, as_agent: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        deliver(commande(restaurant, customer, 1, 4_000))
        deliver(commande(restaurant, customer, 2, 6_000))

        response = as_agent.get(stats_url(customer))

        assert response.data["orders_delivered"] == 2
        assert response.data["total_spent"] == {"amount": "10000", "currency": XOF}
        assert response.data["average_basket"] == {"amount": "5000", "currency": XOF}

    def test_les_commandes_annulees_ne_comptent_pas(
        self, as_agent: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        """Une commande annulée n'a rien encaissé ; l'inclure ferait d'un compte
        qui annule tout un client à forte valeur."""
        deliver(commande(restaurant, customer, 1, 4_000))
        annulee = commande(restaurant, customer, 2, 20_000)
        OrderService.transition_to(order=annulee, target=OrderStatus.CANCELLED)

        response = as_agent.get(stats_url(customer))

        assert response.data["orders_count"] == 2
        assert response.data["orders_cancelled"] == 1
        assert response.data["total_spent"] == {"amount": "4000", "currency": XOF}

    def test_les_commandes_en_cours_non_plus(
        self, as_agent: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        """Elles n'ont rien encaissé *encore* — le total doit rester acquis."""
        commande(restaurant, customer, 1, 9_000)

        response = as_agent.get(stats_url(customer))

        assert response.data["orders_count"] == 1
        assert response.data["total_spent"] == {"amount": "0", "currency": XOF}

    def test_la_fiche_compte_adresses_et_points(
        self, as_agent: APIClient, customer: User, address: Address
    ) -> None:
        PointsAccount.objects.create(user=customer, balance=250, lifetime_earned=900)

        response = as_agent.get(stats_url(customer))

        assert response.data["addresses_count"] == 1
        assert response.data["loyalty_balance"] == 250
        assert response.data["loyalty_lifetime_earned"] == 900

    def test_la_devise_est_celle_des_commandes(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Un même compte peut commander dans deux pays (ADR-006) : la devise se
        lit sur ses commandes, elle n'est pas une constante du serveur."""
        deliver(commande(restaurant, customer, 1, 4_000))

        stats = ReportingService.customer_stats(customer)

        assert stats.total_spent.currency == XOF


class TestAcces:
    def test_sans_customers_read_le_dossier_est_refuse(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """`analytics.read` ne l'ouvre pas : ce n'est pas un chiffre
        d'exploitation mais la donnée d'une personne."""
        analyste = User.objects.create_user(
            "chiffres@elcorazon.test", "motdepasse", full_name="Analyste", user_type=UserType.STAFF
        )
        analyste.roles.add(Role.objects.create(name="Chiffres", permissions=["analytics.read"]))
        client = APIClient()
        client.force_authenticate(analyste)

        response = client.get(stats_url(customer))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_client_ne_lit_pas_son_propre_dossier_ici(self, customer: User) -> None:
        """Il a `/auth/me/` et son historique de commandes ; cette route-ci est
        celle du guichet, et elle porte des agrégats d'exploitation."""
        client = APIClient()
        client.force_authenticate(customer)

        response = client.get(stats_url(customer))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_le_dossier_d_un_membre_du_personnel_est_introuvable(self, as_agent: APIClient) -> None:
        membre = User.objects.create_user(
            "collegue@elcorazon.test", "motdepasse", full_name="Collègue", user_type=UserType.STAFF
        )

        response = as_agent.get(stats_url(membre))

        assert response.status_code == status.HTTP_404_NOT_FOUND
