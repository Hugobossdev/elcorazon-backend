"""Un établissement dans un **autre fuseau**, du premier horaire au dernier chiffre.

## Pourquoi ce scénario manquait, et ce que son absence a coûté

Le parcours réseau de bout en bout traverse déjà deux pays — Lomé et Abidjan.
Mais le Togo et la Côte d'Ivoire sont **tous deux à UTC+0**, et le serveur est
figé en UTC : les trois conventions coïncident, et aucun décalage ne peut s'y
manifester.

C'est exactement ce qui a laissé passer deux défauts, découverts l'un après
l'autre :

* les **fermetures exceptionnelles** partaient de l'horloge du poste qui les
  saisissait — un siège à Lomé fermant Douala à minuit fermait en réalité à une
  heure du matin sur place ;
* les **journées de rapport** étaient tranchées à minuit UTC, si bien que tout
  ce qui se livrait entre minuit et une heure du matin à Douala était compté la
  veille.

Chacun a été corrigé et gardé par ses propres tests. Ce fichier est la maille
qui manquait au-dessus : un parcours où le fuseau **est** différent, du réglage
des horaires jusqu'au chiffre du jour, pour que le prochain défaut de ce genre
n'ait pas besoin d'un deuxième déploiement pour se voir.

Douala est à UTC+1, sans heure d'été : le décalage est le même toute l'année,
ce qui rend les instants ci-dessous lisibles sans tableau de conversion.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.analytics.perimetre import resolve_perimetre
from apps.analytics.reports import ReportingService
from apps.catalog.models import Category, MenuItem
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order, OrderLine, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import (
    KitchenClosure,
    OpeningHours,
    Restaurant,
    RestaurantStatus,
    StaffMembership,
    Weekday,
)
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

DOUALA = Point(9.70, 4.05, srid=4326)
FUSEAU = "Africa/Douala"
XAF = "XAF"


@pytest.fixture
def cuisine_douala() -> Restaurant:
    """Une cuisine camerounaise, ouverte de 10 h à 23 h **heure locale**."""
    pays = Country.objects.create(
        iso_code="CM", name="Cameroun", currency=XAF, phone_prefix="+237", timezone=FUSEAU
    )
    ville = City.objects.create(country=pays, name="Douala", slug="douala", centroid=DOUALA)
    carre = Polygon(
        ((9.60, 3.95), (9.80, 3.95), (9.80, 4.15), (9.60, 4.15), (9.60, 3.95)), srid=4326
    )
    zone = DeliveryZone.objects.create(
        city=ville,
        name="Akwa",
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(500, XAF),
        fee_per_km=Money(100, XAF),
    )
    restaurant = Restaurant.objects.create(
        name="El Corazón Douala",
        slug="el-corazon-douala",
        zone=zone,
        address="Akwa",
        location=DOUALA,
        status=RestaurantStatus.ACTIVE,
    )
    for jour in Weekday.values:
        OpeningHours.objects.create(
            restaurant=restaurant,
            weekday=jour,
            opens_at=dt.time(10, 0),
            closes_at=dt.time(23, 0),
        )
    return restaurant


@pytest.fixture
def plat(cuisine_douala: Restaurant) -> MenuItem:
    rayon = Category.objects.create(
        restaurant=cuisine_douala, name="Grillades", slug="grillades", emoji="🍗"
    )
    return MenuItem.objects.create(
        restaurant=cuisine_douala,
        category=rayon,
        name="Poulet braisé",
        slug="poulet-braise",
        price=Money(4_000, XAF),
    )


@pytest.fixture
def gerant(cuisine_douala: Restaurant) -> APIClient:
    compte = User.objects.create_user(
        "gerant.douala@elcorazon.test",
        "motdepasse",
        full_name="Gérant Douala",
        user_type=UserType.STAFF,
    )
    compte.roles.add(
        Role.objects.create(
            name="Gérance Douala",
            permissions=["restaurants.write", "analytics.read", "orders.read"],
        )
    )
    StaffMembership.objects.create(user=compte, restaurant=cuisine_douala)
    client = APIClient()
    client.force_authenticate(compte)
    return client


def local(annee: int, mois: int, jour: int, heure: int, minute: int = 0) -> dt.datetime:
    """Un instant écrit en **heure de Douala**, comme le personnel le dirait."""
    return dt.datetime(annee, mois, jour, heure, minute, tzinfo=ZoneInfo(FUSEAU))


class TestUneCuisineDansUnAutreFuseau:
    def test_les_horaires_s_evaluent_a_l_heure_de_la_cuisine(
        self, cuisine_douala: Restaurant
    ) -> None:
        """23 h 30 à Douala, c'est 22 h 30 UTC : le serveur ne doit pas la croire ouverte.

        Et 10 h 30 sur place, c'est 9 h 30 UTC — avant l'ouverture si on lisait
        l'heure du serveur. Les deux bords du même décalage.
        """
        assert cuisine_douala.is_open_at(local(2026, 9, 17, 10, 30)) is True
        assert cuisine_douala.is_open_at(local(2026, 9, 17, 23, 30)) is False
        # Le même instant, écrit en UTC : la réponse ne doit pas changer.
        assert cuisine_douala.is_open_at(local(2026, 9, 17, 10, 30).astimezone(dt.UTC)) is True

    def test_une_fermeture_exceptionnelle_se_saisit_en_heure_locale(
        self, gerant: APIClient, cuisine_douala: Restaurant
    ) -> None:
        """Le 25 décembre, « de minuit à minuit » — chez la cuisine, pas au siège.

        Le siège saisissait une heure murale que le serveur situait dans le
        fuseau du **poste** : fermer Douala à minuit fermait à une heure du
        matin sur place, et le service de fin de soirée du 24 restait ouvert.
        """
        reponse = gerant.post(
            reverse("v1:restaurants:managed-closure-list"),
            {
                "restaurant": str(cuisine_douala.pk),
                "starts_at_local": "2026-12-25T00:00:00",
                "ends_at_local": "2026-12-26T00:00:00",
                "reason": "Noël",
            },
            format="json",
        )

        assert reponse.status_code == 201, reponse.data
        fermeture = KitchenClosure.objects.get(restaurant=cuisine_douala)
        # Minuit à Douala, c'est 23 h UTC la veille. C'est **là** que la
        # fermeture doit commencer.
        assert fermeture.starts_at == dt.datetime(2026, 12, 24, 23, 0, tzinfo=dt.UTC)
        assert fermeture.ends_at == dt.datetime(2026, 12, 25, 23, 0, tzinfo=dt.UTC)
        # Et elle se relit telle qu'elle a été saisie, sans reconversion.
        assert reponse.data["starts_at_local"].startswith("2026-12-25T00:00")
        assert reponse.data["timezone_name"] == FUSEAU

    def test_la_journee_comptable_va_de_minuit_local_a_minuit_local(
        self, gerant: APIClient, cuisine_douala: Restaurant, customer: User, plat: MenuItem
    ) -> None:
        """Le parcours complet d'une commande de fin de soirée, et son chiffre.

        Commandée à 23 h 40 le 17, livrée à 0 h 20 le 18 — **heure de Douala**.
        C'est la situation la plus banale d'un service du soir, et celle que
        l'ancienne borne rangeait dans la journée du 17 : la commande était
        comptée un jour, sa livraison un autre, et le gérant cherchait un
        chiffre qui ne tombait nulle part.
        """
        commande = self.commander(cuisine_douala, customer, plat, "EC800001")
        self.mener_jusqu_a_livraison(commande)
        Order.objects.filter(pk=commande.pk).update(
            placed_at=local(2026, 9, 17, 23, 40),
            delivered_at=local(2026, 9, 18, 0, 20),
        )

        perimetre = resolve_perimetre(user=self.compte(gerant), params={})
        assert perimetre.timezone_name == FUSEAU
        assert perimetre.timezone_est_certain is True

        # Le chiffre d'affaires est daté sur la **livraison** : la journée du 18.
        du_18 = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 18), end=dt.date(2026, 9, 18), perimetre=perimetre
        )
        du_17 = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )
        assert [ligne.day for ligne in du_18] == [dt.date(2026, 9, 18)]
        assert du_17 == []

        # La répartition par statut, elle, est datée sur la **commande** : le 17.
        # Deux dates différentes pour la même commande, et c'est correct — la
        # question posée n'est pas la même.
        statuts_du_17 = ReportingService.orders_by_status(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )
        assert [ligne.orders_count for ligne in statuts_du_17] == [1]

    def test_le_top_des_plats_et_l_apercu_parlent_de_la_meme_journee(
        self, gerant: APIClient, cuisine_douala: Restaurant, customer: User, plat: MenuItem
    ) -> None:
        commande = self.commander(cuisine_douala, customer, plat, "EC800002")
        self.mener_jusqu_a_livraison(commande)
        Order.objects.filter(pk=commande.pk).update(
            placed_at=local(2026, 9, 18, 0, 10),
            delivered_at=local(2026, 9, 18, 0, 20),
        )
        perimetre = resolve_perimetre(user=self.compte(gerant), params={})

        plats = ReportingService.top_products(
            start=dt.date(2026, 9, 18), end=dt.date(2026, 9, 18), perimetre=perimetre
        )
        apercu = ReportingService.overview(
            start=dt.date(2026, 9, 18), end=dt.date(2026, 9, 18), perimetre=perimetre
        )

        assert [(ligne.item_name, ligne.quantity_sold) for ligne in plats] == [("Poulet braisé", 2)]
        assert apercu.orders_delivered == 1
        assert apercu.timezone_name == FUSEAU

    def test_le_gerant_de_douala_ne_voit_que_sa_cuisine(
        self,
        gerant: APIClient,
        cuisine_douala: Restaurant,
        restaurant: Restaurant,
        customer: User,
        plat: MenuItem,
    ) -> None:
        """Le cloisonnement passe avant le fuseau — et un fuseau ne l'ouvre pas.

        `restaurant` est la cuisine de Lomé : elle existe, elle a des
        commandes, et le gérant de Douala n'en voit aucune. Le fuseau qu'il
        obtient est celui de **sa** cuisine, pas un repli UTC.
        """
        from tests.fixtures import build_order

        build_order(restaurant, customer, reference="EC800003", status=OrderStatus.READY)
        commande = self.commander(cuisine_douala, customer, plat, "EC800004")
        # La file de production ne montre pas une commande encore « en
        # attente » : elle commence à la confirmation (`KITCHEN_STATUSES`).
        OrderService.transition_to(order=commande, target=OrderStatus.CONFIRMED)

        perimetre = resolve_perimetre(user=self.compte(gerant), params={})
        assert perimetre.restaurant_ids == frozenset({cuisine_douala.pk})
        assert perimetre.timezone_name == FUSEAU

        file_de_production = gerant.get(
            reverse("v1:orders:managed-order-kitchen") + f"?restaurant={cuisine_douala.slug}"
        )
        assert file_de_production.status_code == 200, file_de_production.data
        assert [carte["reference"] for carte in file_de_production.data["results"]] == [
            commande.reference
        ]

    # ------------------------------------------------------------- outillage

    def compte(self, client: APIClient) -> User:
        compte = client.handler._force_user  # type: ignore[attr-defined]
        assert isinstance(compte, User)
        return compte

    def commander(
        self, restaurant: Restaurant, customer: User, plat: MenuItem, reference: str
    ) -> Order:
        commande = Order.objects.create(
            reference=reference,
            restaurant=restaurant,
            customer=customer,
            status=OrderStatus.PENDING,
            subtotal=Money(8_000, XAF),
            delivery_fee=Money(500, XAF),
            discount=Money(0, XAF),
            total=Money(8_500, XAF),
            payment_method=PaymentMethod.CASH,
            delivery_address_line="Boulevard de la Liberté",
            delivery_location={"lat": 4.05, "lon": 9.70},
            recipient_name="Awa",
            recipient_phone="+237690000000",
            city=restaurant.zone.city,
            country=restaurant.zone.city.country,
            delivery_zone=restaurant.zone,
            delivery_zone_name=restaurant.zone.name,
        )
        OrderLine.objects.create(
            order=commande,
            menu_item=plat,
            item_name=plat.name,
            quantity=2,
            unit_price=Money(4_000, XAF),
            line_total=Money(8_000, XAF),
        )
        return commande

    def mener_jusqu_a_livraison(self, commande: Order) -> None:
        """Toutes les étapes, par le service : c'est la vérité du système."""
        for cible in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
            OrderStatus.DELIVERED,
        ):
            OrderService.transition_to(order=commande, target=cible)
        commande.refresh_from_db()


class TestCeQueLeFuseauNePermetPas:
    def test_deux_pays_dans_un_rapport_n_ont_pas_de_journee_commune(
        self, gerant: APIClient, cuisine_douala: Restaurant, restaurant: Restaurant
    ) -> None:
        """Un compte du siège, deux fuseaux : la réponse le dit au lieu de choisir.

        Retenir l'un des deux rendrait des chiffres faux pour l'autre moitié du
        réseau, sans que rien ne le signale. Le repli est UTC, **annoncé**.
        """
        siege = User.objects.create_user(
            "siege.contrat@elcorazon.test",
            "motdepasse",
            full_name="Siège",
            user_type=UserType.STAFF,
            is_superuser=True,
        )

        perimetre = resolve_perimetre(user=siege, params={})

        assert perimetre.is_global
        assert perimetre.timezone_name == "UTC"
        assert perimetre.timezone_est_certain is False

    def test_filtrer_sur_une_seule_cuisine_retrouve_son_fuseau(
        self, cuisine_douala: Restaurant, restaurant: Restaurant
    ) -> None:
        """La sortie de l'ambiguïté : demander une cuisine, c'est demander sa journée."""
        siege = User.objects.create_user(
            "siege.filtre@elcorazon.test",
            "motdepasse",
            full_name="Siège",
            user_type=UserType.STAFF,
            is_superuser=True,
        )

        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        assert perimetre.timezone_name == FUSEAU
        assert perimetre.timezone_est_certain is True

    def test_un_fuseau_inconnu_de_la_base_ne_casse_pas_le_rapport(
        self, cuisine_douala: Restaurant
    ) -> None:
        """Une cuisine mal configurée rend le fuseau incertain, pas le rapport indisponible.

        `ZoneInfo("")` lèverait. Le périmètre écarte donc les fuseaux vides
        plutôt que de les retenir comme une valeur : le rapport reste lisible,
        et son incertitude est déclarée.
        """
        Country.objects.filter(pk=cuisine_douala.zone.city.country_id).update(timezone="")
        siege = User.objects.create_user(
            "siege.vide@elcorazon.test",
            "motdepasse",
            full_name="Siège",
            user_type=UserType.STAFF,
            is_superuser=True,
        )

        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        assert perimetre.timezone_name == "UTC"
        assert perimetre.timezone_est_certain is False
        # Et le rapport s'exécute.
        assert (
            ReportingService.revenue_by_day(
                start=dt.date(2026, 9, 18), end=dt.date(2026, 9, 18), perimetre=perimetre
            )
            == []
        )
