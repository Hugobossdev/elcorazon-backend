"""Rapports du tableau de bord — statuts, catégories, chiffres de tête.

Ils existent pour une raison précise : l'écran d'administration précédent
téléchargeait *toutes* les commandes, tous les comptes, tous les articles et
tous les livreurs pour les compter dans le navigateur. Le tableau de bord
ralentissait à mesure que la plateforme grandissait, et les totaux ne portaient
que sur ce que la pagination avait rendu.

Chaque test construit une commande **réellement livrée** (via `OrderService`,
pas un `update` direct) : le rapport doit lire ce que le reste du système tient
pour la vérité.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem
from apps.delivery.models import CourierProfile
from apps.orders.models import Order, OrderLine
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
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


def fenetre() -> dict[str, str]:
    aujourd_hui = dt.date.today()
    return {
        "start": (aujourd_hui - dt.timedelta(days=1)).isoformat(),
        "end": (aujourd_hui + dt.timedelta(days=1)).isoformat(),
    }


@pytest.fixture
def as_analyst() -> APIClient:
    analyste = User.objects.create_user(
        "tableau@elcorazon.test", "motdepasse", full_name="Analyste", user_type=UserType.STAFF
    )
    analyste.roles.add(Role.objects.create(name="Tableau", permissions=["analytics.read"]))
    client = APIClient()
    client.force_authenticate(analyste)
    return client


class TestStatuts:
    def test_les_commandes_se_repartissent_par_statut(
        self, as_analyst: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        deliver(build_order(restaurant, customer, reference="EC000001"))
        annulee = build_order(restaurant, customer, reference="EC000002")
        OrderService.transition_to(order=annulee, target=OrderStatus.CANCELLED)

        response = as_analyst.get(reverse("v1:analytics:report-orders"), fenetre())

        assert response.status_code == status.HTTP_200_OK
        par_statut = {row["status"]: row["orders_count"] for row in response.data}
        assert par_statut == {OrderStatus.DELIVERED: 1, OrderStatus.CANCELLED: 1}

    def test_les_annulations_figurent_dans_le_decompte(
        self, as_analyst: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Le rapport date sur la commande, pas sur la livraison : une commande
        annulée n'est jamais livrée, et c'est précisément ce qu'on vient voir."""
        annulee = build_order(restaurant, customer, reference="EC000003")
        OrderService.transition_to(order=annulee, target=OrderStatus.CANCELLED)

        response = as_analyst.get(reverse("v1:analytics:report-orders"), fenetre())

        assert [row["status"] for row in response.data] == [OrderStatus.CANCELLED]


class TestCategories:
    def test_les_ventes_s_agregent_par_categorie(
        self,
        as_analyst: APIClient,
        restaurant: Restaurant,
        customer: User,
        menu_item: MenuItem,
        category: Category,
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000004")
        OrderLine.objects.create(  # type: ignore[misc]
            order=commande,
            menu_item=menu_item,
            item_name=menu_item.name,
            quantity=3,
            unit_price=Money(1_000, XOF),
            line_total=Money(3_000, XOF),
        )
        deliver(commande)

        response = as_analyst.get(reverse("v1:analytics:report-categories"), fenetre())

        assert len(response.data) == 1
        ligne = response.data[0]
        assert ligne["category_name"] == category.name
        assert ligne["quantity_sold"] == 3
        assert ligne["revenue_minor"] == 3_000


class TestChiffresDeTete:
    def test_l_apercu_rend_les_compteurs_du_tableau_de_bord(
        self,
        as_analyst: APIClient,
        restaurant: Restaurant,
        customer: User,
        menu_item: MenuItem,
        courier: CourierProfile,
    ) -> None:
        deliver(build_order(restaurant, customer, reference="EC000005"))

        response = as_analyst.get(reverse("v1:analytics:report-overview"), fenetre())

        assert response.data["orders_delivered"] == 1
        assert response.data["revenue_minor"] == 4_000
        assert response.data["average_basket_minor"] == 4_000
        assert response.data["customers_count"] == 1
        assert response.data["menu_items_total"] == 1
        assert response.data["menu_items_available"] == 1

    def test_le_panier_moyen_ignore_ce_qui_n_a_rien_encaisse(
        self, as_analyst: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        deliver(build_order(restaurant, customer, reference="EC000006"))
        build_order(restaurant, customer, reference="EC000007", total=Money(50_000, XOF))

        response = as_analyst.get(reverse("v1:analytics:report-overview"), fenetre())

        assert response.data["orders_count"] == 2
        assert response.data["average_basket_minor"] == 4_000

    def test_un_apercu_sans_activite_ne_divise_pas_par_zero(self, as_analyst: APIClient) -> None:
        response = as_analyst.get(reverse("v1:analytics:report-overview"), fenetre())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["average_basket_minor"] == 0


class TestAcces:
    def test_sans_analytics_read_les_rapports_sont_refuses(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        for route in ("report-orders", "report-categories", "report-overview"):
            response = client.get(reverse(f"v1:analytics:{route}"), fenetre())
            assert response.status_code == status.HTTP_403_FORBIDDEN, route

    def test_une_fenetre_a_l_envers_est_refusee(self, as_analyst: APIClient) -> None:
        response = as_analyst.get(
            reverse("v1:analytics:report-overview"),
            {"start": "2026-07-31", "end": "2026-07-01"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
