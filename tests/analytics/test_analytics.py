"""Analytics — journal immuable, rapports agrégés depuis la source de vérité.

Les rapports ne sont pas testés sur leur formule d'agrégation seule : chaque
test construit une commande **livrée** réellement (via `OrderService`, pas un
`update` direct), pour vérifier que le rapport lit ce que le reste du système
considère comme la vérité, et non une copie qui pourrait diverger.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.analytics.models import AnalyticsEvent
from apps.analytics.reports import ReportingService
from apps.analytics.services import AnalyticsService
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"
TODAY = dt.date(2026, 1, 15)


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


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def as_analyst() -> APIClient:
    analyste = User.objects.create_user(
        "analyste@elcorazon.test", "motdepasse", full_name="Analyste", user_type=UserType.STAFF
    )
    analyste.roles.add(Role.objects.create(name="Analytics", permissions=["analytics.read"]))
    client = APIClient()
    client.force_authenticate(analyste)
    return client


class TestEvenements:
    def test_consigner_un_evenement(self, customer: User) -> None:
        event = AnalyticsService.record(
            user=customer, event_type="catalog.viewed", data={"menu_item_id": "abc"}
        )

        assert event.event_type == "catalog.viewed"
        assert event.event_data == {"menu_item_id": "abc"}

    def test_un_evenement_anonyme_est_accepte(self) -> None:
        event = AnalyticsService.record(user=None, event_type="app.opened", session_id="s-1")

        assert event.user is None
        assert event.session_id == "s-1"

    def test_la_livraison_consigne_un_evenement(self, customer: User, order: Order) -> None:
        """Le câblage réel : `apps.py` abonne le récepteur aux commandes."""
        deliver(order)

        assert AnalyticsEvent.objects.filter(event_type="order.delivered", user=customer).exists()

    def test_consigner_par_l_api(self, as_customer: APIClient) -> None:
        response = as_customer.post(
            reverse("v1:analytics:event"),
            {"event_type": "search.performed", "event_data": {"query": "burger"}},
            format="json",
        )

        assert response.status_code == 201
        assert AnalyticsEvent.objects.filter(event_type="search.performed").exists()

    def test_un_anonyme_ne_peut_pas_consigner(self) -> None:
        response = APIClient().post(
            reverse("v1:analytics:event"), {"event_type": "x"}, format="json"
        )

        assert response.status_code in (401, 403)


class TestRapports:
    def test_le_chiffre_d_affaires_compte_les_commandes_livrees(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        commande = build_order(
            restaurant,
            customer,
            reference="EC000010",
            subtotal=Money(4_000, XOF),
            delivery_fee=Money(500, XOF),
            total=Money(4_500, XOF),
        )
        deliver(commande)

        aujourdhui = commande.delivered_at.date()
        rows = ReportingService.revenue_by_day(start=aujourdhui, end=aujourdhui)

        assert len(rows) == 1
        assert rows[0].orders_count == 1
        assert rows[0].revenue_minor == 4_500

    def test_une_commande_non_livree_n_entre_pas_dans_le_chiffre_d_affaires(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        build_order(restaurant, customer, reference="EC000011")

        rows = ReportingService.revenue_by_day(start=TODAY, end=TODAY + dt.timedelta(days=1))

        assert rows == []

    def test_le_produit_le_plus_vendu_agrege_les_quantites(
        self, customer: User, restaurant: Restaurant, menu_item: object
    ) -> None:
        from apps.orders.models import OrderLine

        commande = build_order(restaurant, customer, reference="EC000012")
        OrderLine.objects.create(
            order=commande,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=2,
            line_total=Money(7_000, XOF),
        )
        deliver(commande)

        aujourdhui = commande.delivered_at.date()
        rows = ReportingService.top_products(start=aujourdhui, end=aujourdhui)

        assert rows[0].item_name == "Burger Corazón"
        assert rows[0].quantity_sold == 2
        assert rows[0].revenue_minor == 7_000

    def test_la_performance_livreur_compte_les_courses_livrees(
        self, courier: CourierProfile, restaurant: Restaurant, customer: User
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000013")
        maintenant = timezone.now()
        Assignment.objects.create(
            order=commande,
            courier=courier,
            status=DeliveryStatus.DELIVERED,
            delivered_at=maintenant,
            courier_fee=Money(600, XOF),
        )

        rows = ReportingService.courier_performance(start=maintenant.date(), end=maintenant.date())

        assert rows[0].courier_id == str(courier.pk)
        assert rows[0].deliveries == 1
        assert rows[0].earnings_minor == 600

    def test_lire_le_rapport_par_l_api(
        self, as_analyst: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000014")
        deliver(commande)
        aujourdhui = commande.delivered_at.date()

        response = as_analyst.get(
            reverse("v1:analytics:report-revenue"),
            {"start": aujourdhui.isoformat(), "end": aujourdhui.isoformat()},
        )

        assert response.status_code == 200
        assert response.data[0]["orders_count"] == 1

    def test_lire_le_rapport_des_produits_par_l_api(
        self, as_analyst: APIClient, customer: User, restaurant: Restaurant, menu_item: object
    ) -> None:
        from apps.orders.models import OrderLine

        commande = build_order(restaurant, customer, reference="EC000015")
        OrderLine.objects.create(
            order=commande,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=1,
            line_total=Money(3_500, XOF),
        )
        deliver(commande)
        aujourdhui = commande.delivered_at.date()

        response = as_analyst.get(
            reverse("v1:analytics:report-top-products"),
            {"start": aujourdhui.isoformat(), "end": aujourdhui.isoformat()},
        )

        assert response.status_code == 200
        assert response.data[0]["item_name"] == "Burger Corazón"

    def test_lire_le_rapport_livreurs_par_l_api(
        self, as_analyst: APIClient, courier: CourierProfile, restaurant: Restaurant, customer: User
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000016")
        maintenant = timezone.now()
        Assignment.objects.create(
            order=commande,
            courier=courier,
            status=DeliveryStatus.DELIVERED,
            delivered_at=maintenant,
            courier_fee=Money(600, XOF),
        )

        response = as_analyst.get(
            reverse("v1:analytics:report-couriers"),
            {"start": maintenant.date().isoformat(), "end": maintenant.date().isoformat()},
        )

        assert response.status_code == 200
        assert response.data[0]["deliveries"] == 1

    def test_un_client_ne_peut_pas_lire_les_rapports(self, as_customer: APIClient) -> None:
        response = as_customer.get(
            reverse("v1:analytics:report-revenue"),
            {"start": TODAY.isoformat(), "end": TODAY.isoformat()},
        )

        assert response.status_code == 403

    def test_une_fenetre_inversee_est_refusee(self, as_analyst: APIClient) -> None:
        response = as_analyst.get(
            reverse("v1:analytics:report-revenue"),
            {"start": TODAY.isoformat(), "end": (TODAY - dt.timedelta(days=1)).isoformat()},
        )

        assert response.status_code == 400


class TestExportCsv:
    """L'export part du **même sérialiseur** que le JSON.

    Deux listes de colonnes entretenues séparément divergent, et l'export finit
    par omettre celle qu'on a ajoutée trois mois plus tôt — sans que rien ne le
    signale, puisqu'un fichier reste produit.
    """

    def test_le_csv_porte_les_memes_colonnes_que_le_json(
        self, as_analyst: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000031")
        deliver(commande)
        jour = commande.delivered_at.date().isoformat()

        json_reponse = as_analyst.get(
            reverse("v1:analytics:report-revenue"), {"start": jour, "end": jour}
        )
        csv_reponse = as_analyst.get(
            reverse("v1:analytics:report-revenue"), {"start": jour, "end": jour, "export": "csv"}
        )

        contenu = csv_reponse.content.decode("utf-8-sig").splitlines()

        assert csv_reponse["Content-Type"].startswith("text/csv")
        assert contenu[0].split(",") == list(json_reponse.data[0])
        assert contenu[1].startswith(jour)

    def test_le_csv_se_telecharge_au_lieu_de_s_afficher(self, as_analyst: APIClient) -> None:
        response = as_analyst.get(
            reverse("v1:analytics:report-couriers"),
            {"start": TODAY.isoformat(), "end": TODAY.isoformat(), "export": "csv"},
        )

        assert "attachment" in response["Content-Disposition"]
        assert "livreurs" in response["Content-Disposition"]

    def test_le_csv_commence_par_une_marque_d_ordre(self, as_analyst: APIClient) -> None:
        """Sans elle, Excel lit un CSV UTF-8 en codage local et affiche
        « CorazÃ³n ». Le tableur est la destination de cet export."""
        response = as_analyst.get(
            reverse("v1:analytics:report-top-products"),
            {"start": TODAY.isoformat(), "end": TODAY.isoformat(), "export": "csv"},
        )

        assert response.content.startswith("﻿".encode())

    def test_l_export_exige_la_meme_permission_que_le_rapport(self, as_customer: APIClient) -> None:
        response = as_customer.get(
            reverse("v1:analytics:report-revenue"),
            {"start": TODAY.isoformat(), "end": TODAY.isoformat(), "export": "csv"},
        )

        assert response.status_code == 403
