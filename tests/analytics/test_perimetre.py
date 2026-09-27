"""Cloisonnement des rapports — le filtre pays / ville / établissement.

## Ce que ces tests gardent

Les rapports agrégeaient toute l'enseigne sans exception. Un gérant rattaché au
seul établissement de Lomé, muni de `analytics.read`, lisait le chiffre
d'affaires d'Abidjan, ses articles les plus vendus et la rémunération de ses
livreurs. Le défaut était silencieux : la réponse rendait des chiffres justes,
simplement pas les siens, et rien n'indiquait sur quoi elle portait.

Les tests d'ici décrivent donc deux choses distinctes, et il faut les deux :

* **la contrainte** — ce qu'un compte cloisonné ne peut pas voir, quoi qu'il
  demande ;
* **l'affinage** — ce qu'un compte du siège obtient en filtrant, qui est une
  fonctionnalité du back-office et non une protection.

Le décor monte **deux marchés réels**, Togo et Côte d'Ivoire, chacun avec sa
ville, sa zone et son établissement. Un décor à un seul restaurant ne prouverait
rien : tous les chiffres y sont, par construction, dans le périmètre.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.analytics.perimetre import Perimetre, resolve_perimetre
from apps.catalog.models import Category, MenuItem
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"
ABIDJAN = Point(-4.0083, 5.3600, srid=4326)


def fenetre() -> dict[str, str]:
    aujourd_hui = dt.date.today()
    return {
        "start": (aujourd_hui - dt.timedelta(days=1)).isoformat(),
        "end": (aujourd_hui + dt.timedelta(days=1)).isoformat(),
    }


def livrer(order: Order) -> Order:
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
def abidjan() -> Restaurant:
    """Second marché complet — pays, ville, zone, établissement.

    Il partage la devise du premier (`XOF`) volontairement : si la séparation
    des chiffres ne tenait qu'à la devise, elle ne tiendrait pas du tout, et
    c'est le cas réel de l'UEMOA.
    """
    ci = Country.objects.create(
        iso_code="CI",
        name="Côte d'Ivoire",
        currency=XOF,
        phone_prefix="+225",
        timezone="Africa/Abidjan",
    )
    ville = City.objects.create(country=ci, name="Abidjan", slug="abidjan", centroid=ABIDJAN)
    carre = Polygon(
        ((-4.10, 5.28), (-3.90, 5.28), (-3.90, 5.45), (-4.10, 5.45), (-4.10, 5.28)), srid=4326
    )
    zone = DeliveryZone.objects.create(
        city=ville,
        name="Plateau",
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(600, XOF),
        fee_per_km=Money(120, XOF),
    )
    return Restaurant.objects.create(
        name="El Corazón Abidjan",
        slug="el-corazon-abidjan",
        zone=zone,
        address="Plateau, Abidjan",
        location=ABIDJAN,
        phone="+22507000000",
        status=RestaurantStatus.ACTIVE,
    )


def analyste(email: str, *, restaurants: list[Restaurant]) -> APIClient:
    """Compte du personnel muni de `analytics.read` et de ses rattachements."""
    compte = User.objects.create_user(
        email, "motdepasse", full_name="Analyste", user_type=UserType.STAFF
    )
    compte.roles.add(Role.objects.create(name=f"Analytics {email}", permissions=["analytics.read"]))
    for etablissement in restaurants:
        StaffMembership.objects.create(user=compte, restaurant=etablissement)
    client = APIClient()
    client.force_authenticate(compte)
    return client


@pytest.fixture
def as_siege() -> APIClient:
    """Compte non cloisonné — le siège.

    Superutilisateur, parce que c'est ce que `is_unscoped` désigne dans ce
    projet. Ce que ces tests lui font vérifier est le **filtre**, jamais la
    permission : celle-ci est éprouvée ailleurs.
    """
    compte = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
    client = APIClient()
    client.force_authenticate(compte)
    return client


class TestContrainteDuPerimetre:
    """Ce qu'un compte cloisonné ne peut pas voir, quoi qu'il demande."""

    def test_un_gerant_ne_lit_pas_le_chiffre_d_affaires_d_un_autre_etablissement(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        livrer(build_order(restaurant, customer, reference="EC000001"))
        livrer(build_order(abidjan, customer, reference="EC000002", total=Money(90_000, XOF)))

        reponse = analyste("lome@elcorazon.test", restaurants=[restaurant]).get(
            reverse("v1:analytics:report-overview"), fenetre()
        )

        # 4 000 : la commande de Lomé seule. Les 90 000 d'Abidjan sont invisibles.
        assert reponse.data["revenue_minor"] == 4_000
        assert reponse.data["orders_count"] == 1

    def test_demander_l_etablissement_d_un_autre_ne_l_ouvre_pas(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        """Le filtre restreint, il n'élargit jamais.

        C'est le test qui compte le plus : sans la composition par
        intersection, `?restaurant=` serait un paramètre par lequel n'importe
        quel compte du personnel lirait n'importe quel établissement.
        """
        livrer(build_order(abidjan, customer, reference="EC000003"))

        reponse = analyste("lome2@elcorazon.test", restaurants=[restaurant]).get(
            reverse("v1:analytics:report-overview"),
            {**fenetre(), "restaurant": "el-corazon-abidjan"},
        )

        assert reponse.data["revenue_minor"] == 0
        assert reponse.data["orders_count"] == 0

    def test_un_membre_sans_rattachement_ne_voit_rien(
        self, restaurant: Restaurant, customer: User
    ) -> None:
        """Un oubli de configuration doit fermer, pas ouvrir.

        C'est l'état exact dans lequel les fixtures d'analytics se trouvaient :
        un compte à qui l'on avait donné `analytics.read` sans jamais dire sur
        quoi. Il lisait l'enseigne entière.
        """
        livrer(build_order(restaurant, customer, reference="EC000004"))

        reponse = analyste("orphelin@elcorazon.test", restaurants=[]).get(
            reverse("v1:analytics:report-overview"), fenetre()
        )

        assert reponse.data["revenue_minor"] == 0

    def test_les_livreurs_d_un_autre_etablissement_n_apparaissent_pas(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User
    ) -> None:
        livrer(build_order(abidjan, customer, reference="EC000005"))

        reponse = analyste("lome3@elcorazon.test", restaurants=[restaurant]).get(
            reverse("v1:analytics:report-couriers"), fenetre()
        )

        assert reponse.data == []

    def test_le_catalogue_compte_est_celui_du_perimetre(
        self, restaurant: Restaurant, abidjan: Restaurant
    ) -> None:
        """Les compteurs de carte suivent le périmètre comme les commandes.

        Un `menu_items_total` d'enseigne sous un chiffre d'affaires
        d'établissement serait pire qu'inutile : les deux nombres du même écran
        ne parleraient pas du même objet.
        """
        for etablissement, nom in ((restaurant, "Tacos"), (abidjan, "Attieke")):
            categorie = Category.objects.create(
                restaurant=etablissement, name=f"Plats {nom}", slug=f"plats-{nom.lower()}"
            )
            MenuItem.objects.create(
                restaurant=etablissement,
                category=categorie,
                name=nom,
                slug=nom.lower(),
                price=Money(2_000, XOF),
            )

        reponse = analyste("lome4@elcorazon.test", restaurants=[restaurant]).get(
            reverse("v1:analytics:report-overview"), fenetre()
        )

        assert reponse.data["menu_items_total"] == 1


class TestAffinageDuSiege:
    """Le filtre comme fonctionnalité : le siège découpe son réseau."""

    def test_le_siege_voit_les_deux_marches(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        livrer(build_order(restaurant, customer, reference="EC000006"))
        livrer(build_order(abidjan, customer, reference="EC000007"))

        reponse = as_siege.get(reverse("v1:analytics:report-overview"), fenetre())

        assert reponse.data["orders_count"] == 2

    def test_filtrer_par_pays_isole_le_marche(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        livrer(build_order(restaurant, customer, reference="EC000008"))
        livrer(build_order(abidjan, customer, reference="EC000009"))

        reponse = as_siege.get(
            reverse("v1:analytics:report-overview"), {**fenetre(), "country": "CI"}
        )

        assert reponse.data["orders_count"] == 1

    def test_un_code_pays_en_minuscules_est_accepte(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        """Une URL partagée ne doit pas rendre un rapport vide sur une casse.

        Les codes ISO sont stockés en majuscules ; personne ne les tape ainsi
        dans une barre d'adresse.
        """
        livrer(build_order(abidjan, customer, reference="EC000010"))

        reponse = as_siege.get(
            reverse("v1:analytics:report-overview"), {**fenetre(), "country": "ci"}
        )

        assert reponse.data["orders_count"] == 1

    def test_filtrer_par_ville_isole_la_ville(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        livrer(build_order(restaurant, customer, reference="EC000011"))
        livrer(build_order(abidjan, customer, reference="EC000012"))

        reponse = as_siege.get(
            reverse("v1:analytics:report-overview"), {**fenetre(), "city": "lome"}
        )

        assert reponse.data["orders_count"] == 1

    def test_les_filtres_se_cumulent_au_lieu_de_se_remplacer(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        """`?country=CI&city=lome` est « Lomé, si Lomé est en Côte d'Ivoire ».

        Elle ne l'est pas : la réponse est donc vide. Un filtre qui en
        écraserait un autre rendrait des chiffres justes pour une question que
        personne n'a posée.
        """
        livrer(build_order(restaurant, customer, reference="EC000013"))

        reponse = as_siege.get(
            reverse("v1:analytics:report-overview"),
            {**fenetre(), "country": "CI", "city": "lome"},
        )

        assert reponse.data["orders_count"] == 0

    def test_un_pays_inconnu_rend_un_rapport_vide_et_non_une_erreur(
        self, restaurant: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        """Le filtre est un affinage, pas une ressource.

        Une ville fermée hier doit rendre une réponse vide : la traiter en 400
        ferait échouer un tableau de bord dont l'utilisateur n'a rien fait de
        mal.
        """
        livrer(build_order(restaurant, customer, reference="EC000014"))

        reponse = as_siege.get(
            reverse("v1:analytics:report-overview"), {**fenetre(), "country": "ZZ"}
        )

        assert reponse.status_code == 200
        assert reponse.data["orders_count"] == 0

    def test_le_chiffre_d_affaires_par_jour_suit_le_filtre(
        self, restaurant: Restaurant, abidjan: Restaurant, customer: User, as_siege: APIClient
    ) -> None:
        livrer(build_order(restaurant, customer, reference="EC000015"))
        livrer(build_order(abidjan, customer, reference="EC000016", total=Money(90_000, XOF)))

        reponse = as_siege.get(
            reverse("v1:analytics:report-revenue"), {**fenetre(), "city": "abidjan"}
        )

        assert [ligne["revenue_minor"] for ligne in reponse.data] == [90_000]


class TestCompositionDuPerimetre:
    """La résolution elle-même, sans passer par HTTP."""

    def test_l_enseigne_entiere_n_est_pas_l_ensemble_vide(self) -> None:
        """Deux notions que confondre ferait fuir toute la donnée.

        `None` veut dire « aucune restriction » ; l'ensemble vide veut dire
        « rien à montrer ». Un `if not perimetre.restaurant_ids` les traiterait
        pareil et rendrait l'enseigne à un compte non rattaché.
        """
        siege = User.objects.create_superuser("racine@elcorazon.test", "motdepasse")
        orphelin = User.objects.create_user(
            "sans@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )

        enseigne = resolve_perimetre(user=siege, params={})
        vide = resolve_perimetre(user=orphelin, params={})

        assert enseigne.is_global and not enseigne.is_empty
        assert vide.is_empty and not vide.is_global

    def test_un_perimetre_global_n_ajoute_aucune_clause(self) -> None:
        """Le rapport d'enseigne ne paie pas un `IN` sur tous les établissements."""
        assert (
            Perimetre(restaurant_ids=None, timezone_name="UTC", timezone_est_certain=False).filtre(
                "restaurant_id"
            )
            == {}
        )
