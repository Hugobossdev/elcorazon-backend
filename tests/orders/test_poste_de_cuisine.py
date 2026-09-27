"""La file de production d'une cuisine — `GET /orders/manage/kitchen/`.

## Ce que cette route remplace

Le poste de cuisine lisait la liste de supervision : un an de commandes, tous
établissements confondus, sans les lignes. Trois défauts en découlaient, et
chacun a son test ici :

* **on ne voyait pas ce qu'il fallait cuisiner.** `OrderSerializer` ne porte pas
  `lines` ; chaque carte affichait « 3 article(s) » ;
* **on voyait les commandes des autres cuisines.** Un compte non cloisonné — le
  siège — avait sous les yeux, sur l'écran « Cuisine — Lomé », les commandes
  d'Abidjan ;
* **on relisait un an d'historique** à chaque événement du service.

Le filtre d'établissement est ici, côté serveur : l'interface peut filtrer à
son tour, elle n'est pas ce qui garantit l'isolement.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import MenuItem
from apps.geography.models import City, DeliveryZone
from apps.orders.models import Order, OrderLine
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from apps.restaurants.states import RestaurantStatus
from common.money import Money
from tests.fixtures import XOF, build_order, ouvert_en_permanence

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def membre(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    user = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=user, restaurant=restaurant)
    return user


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def poste(client: APIClient, slug: str | None = "el-corazon-lome") -> object:
    url = reverse("v1:orders:managed-order-kitchen")
    return client.get(url if slug is None else f"{url}?restaurant={slug}")


@pytest.fixture
def cuisinier(restaurant: Restaurant) -> APIClient:
    """Le poste : il lit le service et le fait avancer, rien de plus."""
    return connecte(
        membre("cuisine@elcorazon.test", restaurant, "orders.read", "orders.update_status")
    )


@pytest.fixture
def siege() -> APIClient:
    """Un compte non cloisonné — celui qui voyait les deux villes à la fois.

    Le superutilisateur, seul à l'être (`common.permissions.is_unscoped`) : un
    membre du personnel sans rattachement ne voit **rien**, et c'est voulu — un
    oubli de configuration ne doit pas ouvrir l'enseigne entière.
    """
    compte = membre("siege@elcorazon.test", None, "orders.read")
    User.objects.filter(pk=compte.pk).update(is_superuser=True)
    return connecte(User.objects.get(pk=compte.pk))


@pytest.fixture
def abidjan(zone: DeliveryZone) -> Restaurant:
    """Une seconde cuisine, dans une autre ville du même pays."""
    ville = City.objects.create(
        name="Abidjan",
        slug="abidjan",
        country=zone.city.country,
        centroid=Point(-4.007, 5.361, srid=4326),
    )
    carre = Polygon(
        ((-4.10, 5.28), (-3.90, 5.28), (-3.90, 5.44), (-4.10, 5.44), (-4.10, 5.28)), srid=4326
    )
    autre_zone = DeliveryZone.objects.create(
        city=ville,
        name="Cocody",
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(500, XOF),
        fee_per_km=Money(100, XOF),
    )
    return ouvert_en_permanence(
        Restaurant.objects.create(
            name="El Corazón Abidjan",
            slug="el-corazon-abidjan",
            zone=autre_zone,
            address="Cocody",
            location=Point(-4.007, 5.361),
            phone="+22500000000",
            status=RestaurantStatus.ACTIVE,
        )
    )


def commande_en_cuisine(
    restaurant: Restaurant, customer: User, reference: str, statut: str = OrderStatus.PREPARING
) -> Order:
    return build_order(
        restaurant,
        customer,
        reference=reference,
        status=statut,
        delivery_instructions="Sans couverts",
    )


class TestCeQueLaCuisineVoit:
    def test_les_plats_les_options_et_les_remarques(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User, menu_item: MenuItem
    ) -> None:
        """Le cœur du sujet : un poste doit dire quoi préparer."""
        commande = commande_en_cuisine(restaurant, customer, "EC500001")
        OrderLine.objects.create(
            order=commande,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=2,
            line_total=Money(7_000, XOF),
            options=[{"group": "Cuisson", "option": "À point"}],
            notes="Sans oignons",
        )

        reponse = poste(cuisinier)

        assert reponse.status_code == status.HTTP_200_OK
        carte = reponse.data["results"][0]
        assert carte["reference"] == "EC500001"
        assert carte["items_count"] == 2
        assert carte["delivery_instructions"] == "Sans couverts"
        ligne = carte["lines"][0]
        assert ligne["item_name"] == "Burger Corazón"
        assert ligne["quantity"] == 2
        assert ligne["options"] == [{"group": "Cuisson", "option": "À point"}]
        assert ligne["notes"] == "Sans oignons"

    def test_aucun_montant_ne_traverse(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User, menu_item: MenuItem
    ) -> None:
        """Le poste ne facture pas : ni prix de ligne, ni total, ni client."""
        commande = commande_en_cuisine(restaurant, customer, "EC500002")
        OrderLine.objects.create(
            order=commande,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=1,
            line_total=Money(3_500, XOF),
        )

        carte = poste(cuisinier).data["results"][0]

        assert "total" not in carte
        assert "recipient_phone" not in carte
        assert "delivery_address_line" not in carte
        assert set(carte["lines"][0]) == {"id", "item_name", "quantity", "options", "notes"}

    def test_les_boutons_viennent_du_serveur(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        commande_en_cuisine(restaurant, customer, "EC500003", OrderStatus.CONFIRMED)

        carte = poste(cuisinier).data["results"][0]

        assert carte["allowed_transitions"] == ["cancelled", "preparing"]


class TestLaFenetreDuService:
    @pytest.mark.parametrize(
        "statut",
        [
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
        ],
    )
    def test_ce_qui_occupe_la_cuisine_y_est(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User, statut: str
    ) -> None:
        commande_en_cuisine(restaurant, customer, f"EC5001{statut[:2]}", statut)

        assert poste(cuisinier).data["count"] == 1

    @pytest.mark.parametrize(
        "statut", [OrderStatus.PENDING, OrderStatus.DELIVERED, OrderStatus.CANCELLED]
    )
    def test_ce_qui_n_y_a_rien_a_faire_en_sort(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User, statut: str
    ) -> None:
        """Ni l'attente de confirmation, ni ce qui est joué.

        C'est ce qui remplace la fenêtre d'un an : la liste est bornée par le
        service en cours, pas par une profondeur d'historique.
        """
        commande_en_cuisine(restaurant, customer, f"EC5002{statut[:2]}", statut)

        assert poste(cuisinier).data["count"] == 0

    def test_la_plus_ancienne_d_abord(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Le service se fait dans l'ordre d'arrivée."""
        premiere = commande_en_cuisine(restaurant, customer, "EC500301")
        seconde = commande_en_cuisine(restaurant, customer, "EC500302")
        Order.objects.filter(pk=seconde.pk).update(placed_at=premiere.placed_at.replace(year=2030))

        references = [carte["reference"] for carte in poste(cuisinier).data["results"]]

        assert references == ["EC500301", "EC500302"]

    def test_la_liste_est_paginee(
        self, cuisinier: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        for numero in range(3):
            commande_en_cuisine(restaurant, customer, f"EC50040{numero}")

        reponse = poste(cuisinier)

        assert reponse.data["count"] == 3
        assert "next" in reponse.data


class TestLIsolementDesCuisines:
    def test_le_siege_ne_voit_que_la_cuisine_demandee(
        self,
        siege: APIClient,
        restaurant: Restaurant,
        abidjan: Restaurant,
        customer: User,
    ) -> None:
        """Le défaut, en un test : un compte qui voit tout, un écran qui n'en tient qu'une."""
        commande_en_cuisine(restaurant, customer, "EC500501")
        commande_en_cuisine(abidjan, customer, "EC500502")

        lome = poste(siege, "el-corazon-lome").data
        cocody = poste(siege, "el-corazon-abidjan").data

        assert [c["reference"] for c in lome["results"]] == ["EC500501"]
        assert [c["reference"] for c in cocody["results"]] == ["EC500502"]

    def test_le_cloisonnement_du_compte_s_applique_d_abord(
        self, cuisinier: APIClient, abidjan: Restaurant, customer: User
    ) -> None:
        """Demander la cuisine d'autrui ne la rend pas : le périmètre prime."""
        commande_en_cuisine(abidjan, customer, "EC500601")

        assert poste(cuisinier, "el-corazon-abidjan").data["count"] == 0

    def test_l_etablissement_est_obligatoire(self, cuisinier: APIClient) -> None:
        """Sans lui, la réponse serait « toutes les cuisines » — le défaut d'origine."""
        reponse = poste(cuisinier, slug=None)

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert "indiquez lequel" in reponse.data["detail"]


class TestLaPermission:
    def test_sans_orders_read_le_poste_est_fermé(self, restaurant: Restaurant) -> None:
        client = connecte(membre("sans-droit@elcorazon.test", restaurant, "catalog.read"))

        assert poste(client).status_code == status.HTTP_403_FORBIDDEN

    def test_un_client_n_ouvre_pas_le_poste(self, customer: User) -> None:
        assert poste(connecte(customer)).status_code == status.HTTP_403_FORBIDDEN
