"""Compteurs d'articles et compteurs d'onglets — `/orders/manage/`.

Ces deux mécanismes corrigent le défaut le plus visible du back-office : la
carte d'une commande annonçait « 0 article » puis « Aucun article trouvé dans
cette commande », **sur des commandes qui en contenaient**. La cause n'était ni
l'écran ni le parsing : `OrderSerializer` ne rend pas `lines` — et ne doit pas
les rendre, renvoyer les lignes de vingt commandes pour n'en afficher que le
nombre multiplierait par dix le poids de chaque page.

Les tests de `TestGroupBy` méritent d'exister à part. Django ajoute
silencieusement au `GROUP BY` toute annotation posée avant un `values()`, **et
l'ordre par défaut du modèle**. Les deux ont été rencontrés ici : le compte de
« Prêtes » est tombé à 1 sur deux commandes parce que leurs `items_count`
différaient, puis la somme des compteurs est tombée à 4 sur six commandes à
cause de `order_by("-placed_at")`. Aucune requête n'échoue : le nombre est
simplement faux.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem
from apps.orders.models import Order, OrderLine
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LISTE = "v1:orders:managed-order-list"
COMPTES = "v1:orders:managed-order-counts"
XOF = "XOF"


@pytest.fixture
def superviseur(restaurant: Restaurant) -> APIClient:
    membre = User.objects.create_user(
        "compteurs@elcorazon.test", "motdepasse", full_name="Kossi", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name="Supervision", permissions=["orders.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


@pytest.fixture
def carte(restaurant: Restaurant) -> list[MenuItem]:
    categorie = Category.objects.create(restaurant=restaurant, name="Plats", slug="plats-compteurs")
    return [
        MenuItem.objects.create(
            restaurant=restaurant,
            category=categorie,
            name=nom,
            slug=f"compteurs-{nom.lower()}",
            price=Money(2_000, XOF),
        )
        for nom in ("Burger", "Pizza", "Donuts")
    ]


def _ligne(order: Order, article: MenuItem, quantite: int) -> OrderLine:
    return OrderLine.objects.create(
        order=order,
        menu_item=article,
        item_name=article.name,
        unit_price=Money(2_000, XOF),
        quantity=quantite,
        line_total=Money(2_000 * quantite, XOF),
    )


class TestCompteurDArticles:
    def test_la_forme_liste_porte_les_deux_compteurs(
        self, superviseur: APIClient, order: Order, carte: list[MenuItem]
    ) -> None:
        """Le point de départ : sans eux, la carte lit `lines` — absent — et
        affiche zéro."""
        _ligne(order, carte[0], 1)

        reponse = superviseur.get(reverse(LISTE))

        ligne = reponse.data["results"][0]
        assert "lines_count" in ligne
        assert "items_count" in ligne
        # Et toujours pas `lines` : c'est ce qui garde la page légère.
        assert "lines" not in ligne

    def test_les_articles_sont_la_somme_des_quantites(
        self, superviseur: APIClient, order: Order, carte: list[MenuItem]
    ) -> None:
        """Deux burgers, une pizza et trois donuts font **six** articles."""
        _ligne(order, carte[0], 2)
        _ligne(order, carte[1], 1)
        _ligne(order, carte[2], 3)

        ligne = superviseur.get(reverse(LISTE)).data["results"][0]

        assert ligne["items_count"] == 6

    def test_les_lignes_sont_le_nombre_de_produits_distincts(
        self, superviseur: APIClient, order: Order, carte: list[MenuItem]
    ) -> None:
        """…et **trois** lignes. Les deux chiffres sont rendus parce que les
        deux questions se posent : la cuisine compte les articles, la facture
        compte les lignes."""
        _ligne(order, carte[0], 2)
        _ligne(order, carte[1], 1)
        _ligne(order, carte[2], 3)

        ligne = superviseur.get(reverse(LISTE)).data["results"][0]

        assert ligne["lines_count"] == 3

    def test_une_commande_sans_ligne_rend_zero_et_non_null(
        self, superviseur: APIClient, order: Order
    ) -> None:
        """Zéro plutôt que `null` : l'appelant n'a pas à distinguer les deux
        pour afficher « 0 article »."""
        ligne = superviseur.get(reverse(LISTE)).data["results"][0]

        assert ligne["items_count"] == 0
        assert ligne["lines_count"] == 0

    def test_la_liste_et_le_detail_disent_la_meme_chose(
        self, superviseur: APIClient, order: Order, carte: list[MenuItem]
    ) -> None:
        """Deux sources pour un même nombre finissent par diverger. Celle-ci
        est vérifiée."""
        _ligne(order, carte[0], 4)
        _ligne(order, carte[1], 2)

        liste = superviseur.get(reverse(LISTE)).data["results"][0]
        detail = superviseur.get(reverse("v1:orders:managed-order-detail", args=[order.pk])).data

        assert liste["lines_count"] == len(detail["lines"])
        assert liste["items_count"] == sum(ligne["quantity"] for ligne in detail["lines"])

    def test_le_compteur_ne_melange_pas_deux_commandes(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, carte: list[MenuItem]
    ) -> None:
        """La jointure des lignes est le point où un compte déborde sur la
        commande voisine."""
        a = build_order(restaurant, customer, reference="EC000001")
        b = build_order(restaurant, customer, reference="EC000002")
        _ligne(a, carte[0], 5)
        _ligne(b, carte[0], 1)

        par_reference = {
            ligne["reference"]: ligne for ligne in superviseur.get(reverse(LISTE)).data["results"]
        }

        assert par_reference["EC000001"]["items_count"] == 5
        assert par_reference["EC000002"]["items_count"] == 1


class TestCompteursParStatut:
    def test_chaque_statut_est_compte(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        for index in range(3):
            build_order(
                restaurant, customer, reference=f"EC00000{index}", status=OrderStatus.PENDING
            )
        build_order(restaurant, customer, reference="EC000010", status=OrderStatus.READY)

        comptes = superviseur.get(reverse(COMPTES)).data

        assert comptes[OrderStatus.PENDING] == 3
        assert comptes[OrderStatus.READY] == 1

    def test_les_statuts_absents_valent_zero(self, superviseur: APIClient, order: Order) -> None:
        """Tous les statuts sont présents dans la réponse : l'écran n'a pas à
        distinguer une clé manquante d'un zéro."""
        comptes = superviseur.get(reverse(COMPTES)).data

        assert set(comptes) == set(OrderStatus.values)
        assert comptes[OrderStatus.CANCELLED] == 0

    def test_les_compteurs_suivent_la_recherche(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Sinon un onglet annonce douze commandes et en affiche trois."""
        build_order(restaurant, customer, reference="EC000001", recipient_name="Ama Konaté")
        build_order(restaurant, customer, reference="EC000002", recipient_name="Yao Mensah")

        comptes = superviseur.get(reverse(COMPTES), {"search": "Konaté"}).data

        assert sum(comptes.values()) == 1

    def test_le_cloisonnement_s_applique(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            phone="+22890000006",
            location=restaurant.location,
        )
        build_order(ailleurs, customer, reference="EC999999", status=OrderStatus.PENDING)
        build_order(restaurant, customer, reference="EC000001", status=OrderStatus.PENDING)

        comptes = superviseur.get(reverse(COMPTES)).data

        assert comptes[OrderStatus.PENDING] == 1

    def test_sans_orders_read_la_route_est_fermee(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        sans_droit = User.objects.create_user(
            "sansdroit@elcorazon.test", "motdepasse", full_name="X", user_type=UserType.STAFF
        )
        StaffMembership.objects.create(user=sans_droit, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(sans_droit)

        assert client.get(reverse(COMPTES)).status_code == status.HTTP_403_FORBIDDEN


class TestGroupBy:
    """Les deux pièges silencieux de `values().annotate()` sous Django.

    Aucun des deux ne fait échouer une requête : le nombre est simplement
    faux. Ils ont tous les deux été rencontrés en écrivant `counts`.
    """

    def test_les_compteurs_d_articles_ne_scindent_pas_les_statuts(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, carte: list[MenuItem]
    ) -> None:
        """Piège n°1 — une annotation posée avant `values()` entre dans le
        `GROUP BY`.

        Deux commandes du même statut mais de quantités **différentes** : si
        `items_count` participe au regroupement, elles forment deux lignes et
        le compte affiché n'en retient qu'une.
        """
        a = build_order(restaurant, customer, reference="EC000001", status=OrderStatus.READY)
        b = build_order(restaurant, customer, reference="EC000002", status=OrderStatus.READY)
        _ligne(a, carte[0], 1)
        _ligne(b, carte[0], 7)  # quantité différente : c'est tout le sujet

        comptes = superviseur.get(reverse(COMPTES)).data

        assert comptes[OrderStatus.READY] == 2

    def test_l_ordre_par_defaut_ne_scinde_pas_les_statuts(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Piège n°2 — `order_by("-placed_at")` est ajouté au `GROUP BY`.

        Deux commandes du même statut ont forcément des `placed_at` distincts :
        sans `.order_by()` vide, chacune forme sa propre ligne et le compte
        tombe à un.
        """
        build_order(restaurant, customer, reference="EC000001", status=OrderStatus.CONFIRMED)
        build_order(restaurant, customer, reference="EC000002", status=OrderStatus.CONFIRMED)
        build_order(restaurant, customer, reference="EC000003", status=OrderStatus.CONFIRMED)

        comptes = superviseur.get(reverse(COMPTES)).data

        assert comptes[OrderStatus.CONFIRMED] == 3

    def test_la_somme_des_compteurs_egale_le_total_de_la_liste(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, carte: list[MenuItem]
    ) -> None:
        """Le contrôle qui attrape les deux pièges d'un coup, et ceux qu'on
        n'a pas encore rencontrés."""
        statuts = [
            OrderStatus.PENDING,
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.READY,
            OrderStatus.DELIVERED,
        ]
        for index, statut in enumerate(statuts):
            commande = build_order(
                restaurant, customer, reference=f"EC00{index:04d}", status=statut
            )
            _ligne(commande, carte[index % 3], index + 1)

        comptes = superviseur.get(reverse(COMPTES)).data
        total = superviseur.get(reverse(LISTE), {"page_size": 1}).data["count"]

        assert sum(comptes.values()) == total == len(statuts)
