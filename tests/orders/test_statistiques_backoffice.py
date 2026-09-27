"""`GET /orders/manage/statistics/` — les chiffres que le back-office calculait seul.

Le back-office téléchargeait un an de commandes pour ces chiffres ; la carte
temps réel relançait ce téléchargement toutes les dix secondes. Les règles de
mesure sont celles que le client appliquait, et que
`apps/admin/test/statistiques_livraison_test.dart` fixait — reprises ici cas
pour cas, maintenant qu'elles s'exécutent en SQL :

* la durée de livraison est le réel (`delivered_at − placed_at`), jamais la
  promesse ; une livraison sans horodatage n'entre dans aucun calcul ;
* la ponctualité ne se juge que sur les commandes qui portaient une heure
  annoncée, et arriver à l'heure annoncée, c'est la tenir ;
* le chiffre d'affaires est par devise.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

STATISTIQUES = "v1:orders:managed-order-statistics"


@pytest.fixture
def superviseur(restaurant: Restaurant) -> APIClient:
    membre = User.objects.create_user(
        "supervision@elcorazon.test", "x", full_name="Supervision", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name="Lecture", permissions=["orders.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


PASSEE = timezone.now() - dt.timedelta(hours=3)


def commande(
    restaurant: Restaurant,
    customer: User,
    reference: str,
    statut: str = OrderStatus.PENDING,
    *,
    livree_apres: int | None = None,
    promise_apres: int | None = None,
) -> None:
    cree = build_order(
        restaurant,
        customer,
        reference=reference,
        status=statut,
        delivered_at=None if livree_apres is None else PASSEE + dt.timedelta(minutes=livree_apres),
        estimated_delivery_at=(
            None if promise_apres is None else PASSEE + dt.timedelta(minutes=promise_apres)
        ),
    )
    # `placed_at` est posé à la création (`auto_now_add`) : on le recule après
    # coup, faute de quoi la durée mesurée partirait de maintenant.
    Order.objects.filter(pk=cree.pk).update(placed_at=PASSEE)


def lire(client: APIClient) -> dict[str, object]:
    reponse = client.get(reverse(STATISTIQUES))
    assert reponse.status_code == status.HTTP_200_OK
    return dict(reponse.data)


class TestDureeDeLivraison:
    def test_se_mesure_sur_le_reel_pas_sur_la_promesse(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        # Promise en 30 minutes, livrée en 75.
        commande(
            restaurant,
            customer,
            "EC600001",
            OrderStatus.DELIVERED,
            livree_apres=75,
            promise_apres=30,
        )

        livraison = lire(superviseur)["delivery"]

        assert livraison["average_minutes"] == 75.0  # type: ignore[index]

    def test_une_livraison_sans_horodatage_n_entre_dans_aucun_calcul(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600002", OrderStatus.DELIVERED, livree_apres=40)
        commande(restaurant, customer, "EC600003", OrderStatus.DELIVERED)

        livraison = lire(superviseur)["delivery"]

        assert livraison["measured_orders"] == 1  # type: ignore[index]
        assert livraison["average_minutes"] == 40.0  # type: ignore[index]

    def test_rend_le_plus_rapide_et_le_plus_lent(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600004", OrderStatus.DELIVERED, livree_apres=18)
        commande(restaurant, customer, "EC600005", OrderStatus.DELIVERED, livree_apres=92)

        livraison = lire(superviseur)["delivery"]

        assert livraison["fastest_minutes"] == 18.0  # type: ignore[index]
        assert livraison["slowest_minutes"] == 92.0  # type: ignore[index]
        assert livraison["average_minutes"] == 55.0  # type: ignore[index]

    def test_sans_livraison_rien_n_est_invente(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600006")

        livraison = lire(superviseur)["delivery"]

        assert livraison["measured_orders"] == 0  # type: ignore[index]
        # Nul, et non zéro : « 0 min » affirmerait une livraison instantanée.
        assert livraison["average_minutes"] is None  # type: ignore[index]
        assert livraison["on_time_rate"] is None  # type: ignore[index]


class TestPonctualite:
    def test_compare_la_livraison_a_l_heure_annoncee(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(
            restaurant,
            customer,
            "EC600007",
            OrderStatus.DELIVERED,
            livree_apres=50,
            promise_apres=60,
        )
        commande(
            restaurant,
            customer,
            "EC600008",
            OrderStatus.DELIVERED,
            livree_apres=80,
            promise_apres=60,
        )

        assert lire(superviseur)["delivery"]["on_time_rate"] == 50.0  # type: ignore[index]

    def test_arriver_a_l_heure_annoncee_c_est_la_tenir(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(
            restaurant,
            customer,
            "EC600009",
            OrderStatus.DELIVERED,
            livree_apres=45,
            promise_apres=45,
        )

        assert lire(superviseur)["delivery"]["on_time_rate"] == 100.0  # type: ignore[index]

    def test_ne_se_juge_que_sur_les_commandes_promises(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(
            restaurant,
            customer,
            "EC600010",
            OrderStatus.DELIVERED,
            livree_apres=50,
            promise_apres=60,
        )
        commande(restaurant, customer, "EC600011", OrderStatus.DELIVERED, livree_apres=200)

        livraison = lire(superviseur)["delivery"]

        assert livraison["on_time_measured"] == 1  # type: ignore[index]
        assert livraison["on_time_rate"] == 100.0  # type: ignore[index]
        assert livraison["measured_orders"] == 2  # type: ignore[index]


class TestComptesEtChiffre:
    def test_taux_d_annulation_et_comptes_par_statut(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600012", OrderStatus.DELIVERED)
        commande(restaurant, customer, "EC600013", OrderStatus.CANCELLED)
        commande(restaurant, customer, "EC600014")
        commande(restaurant, customer, "EC600015")

        donnees = lire(superviseur)

        assert donnees["orders_count"] == 4
        assert donnees["cancellation_rate"] == 25.0
        assert donnees["by_status"][OrderStatus.PENDING] == 2  # type: ignore[index]
        assert donnees["by_status"][OrderStatus.ON_THE_WAY] == 0  # type: ignore[index]

    def test_le_chiffre_d_affaires_porte_sa_devise(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600016", OrderStatus.DELIVERED, livree_apres=30)
        commande(restaurant, customer, "EC600017", OrderStatus.CANCELLED)

        revenus = lire(superviseur)["revenues"]

        assert revenus == [
            {
                "currency": "XOF",
                "orders_delivered": 1,
                "revenue_minor": 4_000,
                "average_basket_minor": 4_000,
            }
        ]

    def test_les_filtres_de_la_liste_s_appliquent(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600018", OrderStatus.DELIVERED)
        commande(restaurant, customer, "EC600019", OrderStatus.CANCELLED)

        reponse = superviseur.get(reverse(STATISTIQUES), {"status": OrderStatus.CANCELLED})

        assert reponse.data["orders_count"] == 1

    def test_une_serie_quotidienne_dans_le_fuseau_de_la_cuisine(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        commande(restaurant, customer, "EC600020")

        donnees = lire(superviseur)

        assert donnees["timezone_name"] == "Africa/Lome"
        assert sum(ligne["orders_count"] for ligne in donnees["per_day"]) == 1  # type: ignore[attr-defined]


class TestCloisonnement:
    def test_hors_perimetre_rien_n_est_compte(self, restaurant: Restaurant, customer: User) -> None:
        commande(restaurant, customer, "EC600021", OrderStatus.DELIVERED)
        ailleurs = User.objects.create_user(
            "ailleurs@elcorazon.test", "x", full_name="Ailleurs", user_type=UserType.STAFF
        )
        ailleurs.roles.add(Role.objects.create(name="Lecture seule", permissions=["orders.read"]))
        client = APIClient()
        client.force_authenticate(ailleurs)

        assert lire(client)["orders_count"] == 0

    def test_sans_orders_read_la_route_est_refusee(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        assert client.get(reverse(STATISTIQUES)).status_code == status.HTTP_403_FORBIDDEN
