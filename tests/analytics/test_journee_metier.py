"""La journée d'un rapport commence chez la cuisine, pas à minuit UTC.

## Le défaut que cette suite ferme

Les six rapports bornaient leur fenêtre par `delivered_at__date__range=(start,
end)`. Le lookup `__date` extrait la date dans le fuseau **actif du serveur**,
et il est figé à UTC (`settings.TIME_ZONE`). Une « journée de rapport »
commençait donc à minuit UTC.

Pour une cuisine de Douala (UTC+1), la journée d'exploitation commence à 23 h
UTC la veille : **tout ce qui se livrait entre minuit et une heure du matin sur
place était compté la veille**. Le chiffre d'affaires du jour était faux d'un
service de fin de soirée, chaque jour, et le total de la semaine restait juste —
ce qui rend l'erreur d'autant plus difficile à voir.

Personne ne l'avait remarqué parce que l'établissement d'origine est à Lomé, où
UTC+0 fait coïncider les deux conventions. Le défaut naît avec le deuxième pays,
et il était déjà là : le réseau de cuisines est livré.

## Et la fenêtre par défaut

Les dates étaient **obligatoires**, et l'application les calculait sur
`DateTime.now()` — l'horloge du poste du back-office. Un siège qui consulte à
minuit et demi demandait donc les chiffres d'une journée que la cuisine n'avait
pas commencée. Elles sont devenues facultatives : à défaut, le serveur retient
la journée en cours **là où l'activité a lieu**, et la republie.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.analytics.perimetre import (
    Perimetre,
    aujourd_hui_chez,
    fenetre_metier,
    resolve_perimetre,
)
from apps.analytics.reports import ReportingService
from apps.catalog.models import Category, MenuItem
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order, OrderLine, PaymentMethod
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from tests.analytics.test_analytics import deliver
from tests.fixtures import ouvert_en_permanence

pytestmark = pytest.mark.django_db

XOF = "XOF"
DOUALA = Point(9.70, 4.05, srid=4326)

#: Douala est à UTC+1 — un décalage fixe, sans heure d'été : la borne est donc
#: la même toute l'année, ce qui rend les cas ci-dessous lisibles sans tableau
#: de conversion.
FUSEAU_DOUALA = "Africa/Douala"


@pytest.fixture
def cameroun() -> Country:
    return Country.objects.create(
        iso_code="CM",
        name="Cameroun",
        currency="XAF",
        phone_prefix="+237",
        timezone=FUSEAU_DOUALA,
    )


@pytest.fixture
def douala(cameroun: Country) -> DeliveryZone:
    carre = Polygon(
        ((9.60, 3.95), (9.80, 3.95), (9.80, 4.15), (9.60, 4.15), (9.60, 3.95)), srid=4326
    )
    ville = City.objects.create(country=cameroun, name="Douala", slug="douala", centroid=DOUALA)
    return DeliveryZone.objects.create(
        city=ville,
        name="Akwa",
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(500, "XAF"),
        fee_per_km=Money(100, "XAF"),
    )


@pytest.fixture
def cuisine_douala(douala: DeliveryZone) -> Restaurant:
    return ouvert_en_permanence(
        Restaurant.objects.create(
            name="El Corazón Douala",
            slug="el-corazon-douala",
            zone=douala,
            address="Akwa",
            location=DOUALA,
            status=RestaurantStatus.ACTIVE,
        )
    )


def commande_livree_a(
    *,
    restaurant: Restaurant,
    customer: User,
    instant: dt.datetime,
    reference: str,
    plat: MenuItem,
) -> Order:
    """Une commande réellement livrée, puis **datée** à l'instant voulu.

    La livraison passe par `OrderService` — c'est ce que le reste du système
    considère comme la vérité —, et `delivered_at` est ensuite réécrit par un
    `update` direct : la machine à états pose l'horodatage à l'instant présent,
    et on ne peut pas voyager dans le temps par l'API.
    """
    devise = restaurant.zone.city.country.currency
    commande = Order.objects.create(
        reference=reference,
        restaurant=restaurant,
        customer=customer,
        status=OrderStatus.PENDING,
        subtotal=Money(4_000, devise),
        delivery_fee=Money(500, devise),
        discount=Money(0, devise),
        total=Money(4_500, devise),
        payment_method=PaymentMethod.CASH,
        delivery_address_line="Rue du Commerce",
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
        item_name="Poulet braisé",
        quantity=1,
        unit_price=Money(4_000, devise),
        line_total=Money(4_000, devise),
    )
    deliver(commande)
    Order.objects.filter(pk=commande.pk).update(placed_at=instant, delivered_at=instant)
    commande.refresh_from_db()
    return commande


@pytest.fixture
def plat_douala(cuisine_douala: Restaurant) -> MenuItem:
    """Un plat de **cette** cuisine — le classement des plats est cloisonné."""
    rayon = Category.objects.create(
        restaurant=cuisine_douala, name="Grillades", slug="grillades", emoji="🍗"
    )
    return MenuItem.objects.create(
        restaurant=cuisine_douala,
        category=rayon,
        name="Poulet braisé",
        slug="poulet-braise",
        price=Money(4_000, "XAF"),
    )


@pytest.fixture
def siege() -> User:
    """Un compte non cloisonné — `is_unscoped` n'est vrai que pour un superutilisateur."""
    return User.objects.create_user(
        "siege@elcorazon.test",
        "motdepasse",
        full_name="Siège",
        user_type=UserType.STAFF,
        is_superuser=True,
    )


@pytest.fixture
def gerant_douala(cuisine_douala: Restaurant) -> User:
    """Rattaché à Douala, et à elle seule."""
    compte = User.objects.create_user(
        "gerant.douala@elcorazon.test",
        "motdepasse",
        full_name="Gérant Douala",
        user_type=UserType.STAFF,
    )
    StaffMembership.objects.create(user=compte, restaurant=cuisine_douala)
    return compte


@pytest.fixture
def analyste_douala(cuisine_douala: Restaurant) -> APIClient:
    compte = User.objects.create_user(
        "analyste.douala@elcorazon.test",
        "motdepasse",
        full_name="Analyste Douala",
        user_type=UserType.STAFF,
    )
    compte.roles.add(Role.objects.create(name="Analytics Douala", permissions=["analytics.read"]))
    StaffMembership.objects.create(user=compte, restaurant=cuisine_douala)
    client = APIClient()
    client.force_authenticate(compte)
    return client


# ====================================================== la fenêtre elle-même


class TestFenetreMetier:
    """`fenetre_metier` seule — la conversion, sans base de données."""

    def test_une_journee_va_de_minuit_local_a_minuit_local(self) -> None:
        debut, fin = fenetre_metier(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), timezone_name=FUSEAU_DOUALA
        )

        assert debut == dt.datetime(2026, 9, 17, 0, 0, tzinfo=ZoneInfo(FUSEAU_DOUALA))
        # Minuit du **lendemain**, exclu : un `__lte` sur 23:59:59 laisserait
        # filer la dernière seconde du service.
        assert fin == dt.datetime(2026, 9, 18, 0, 0, tzinfo=ZoneInfo(FUSEAU_DOUALA))

    def test_a_douala_la_journee_commence_la_veille_en_utc(self) -> None:
        debut, _ = fenetre_metier(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), timezone_name=FUSEAU_DOUALA
        )

        # C'est tout le défaut, en une ligne : la journée du 17 à Douala
        # commence le 16 à 23 h UTC, et `__date` la faisait commencer le 17 à
        # minuit UTC — une heure de service perdue chaque jour.
        assert debut.astimezone(dt.UTC) == dt.datetime(2026, 9, 16, 23, 0, tzinfo=dt.UTC)

    def test_a_lome_les_deux_conventions_coincident(self) -> None:
        """Ce qui explique que le défaut soit resté invisible."""
        debut, fin = fenetre_metier(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), timezone_name="Africa/Lome"
        )

        assert debut.astimezone(dt.UTC) == dt.datetime(2026, 9, 17, 0, 0, tzinfo=dt.UTC)
        assert fin.astimezone(dt.UTC) == dt.datetime(2026, 9, 18, 0, 0, tzinfo=dt.UTC)

    def test_plusieurs_jours_restent_bornes_aux_extremites(self) -> None:
        debut, fin = fenetre_metier(
            start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 30), timezone_name=FUSEAU_DOUALA
        )

        assert debut.date() == dt.date(2026, 9, 1)
        assert fin.date() == dt.date(2026, 10, 1)


# ============================================== le fuseau vient du périmètre


class TestFuseauDuPerimetre:
    def test_une_seule_cuisine_donne_son_fuseau(
        self, cuisine_douala: Restaurant, gerant_douala: User
    ) -> None:
        perimetre = resolve_perimetre(user=gerant_douala, params={})

        assert perimetre.timezone_name == FUSEAU_DOUALA
        assert perimetre.timezone_est_certain is True

    def test_lome_et_douala_ensemble_n_ont_pas_de_journee_commune(
        self, restaurant: Restaurant, cuisine_douala: Restaurant, siege: User
    ) -> None:
        """Deux fuseaux, donc aucune réponse juste — et la réponse le dit.

        Retenir l'un des deux rendrait des chiffres faux pour l'autre moitié du
        réseau, sans que rien ne le signale. UTC est un repli assumé, et
        `timezone_est_certain` à faux est ce que l'écran doit afficher.
        """
        perimetre = resolve_perimetre(user=siege, params={})

        assert perimetre.timezone_name == "UTC"
        assert perimetre.timezone_est_certain is False

    def test_un_perimetre_hors_droits_reste_vide(
        self, restaurant: Restaurant, gerant_douala: User
    ) -> None:
        """Le fuseau n'est pas une porte d'entrée : le cloisonnement passe avant."""
        perimetre = resolve_perimetre(user=gerant_douala, params={"restaurant": restaurant.slug})

        assert perimetre.is_empty


# ======================================================= les rapports datés


class TestLesRapportsSuiventLaCuisine:
    def test_une_livraison_de_minuit_trente_compte_le_bon_jour(
        self, cuisine_douala: Restaurant, customer: User, siege: User, plat_douala: MenuItem
    ) -> None:
        """Le cas exact du défaut.

        Minuit trente à Douala, c'est 23 h 30 UTC la veille. L'ancienne borne
        rangeait donc cette livraison dans la journée précédente, et le service
        de fin de soirée disparaissait du chiffre du jour.
        """
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime(2026, 9, 17, 0, 30, tzinfo=ZoneInfo(FUSEAU_DOUALA)),
            plat=plat_douala,
            reference="EC900001",
        )
        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        du_jour = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )
        de_la_veille = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 16), end=dt.date(2026, 9, 16), perimetre=perimetre
        )

        assert [ligne.orders_count for ligne in du_jour] == [1]
        assert de_la_veille == []

    def test_une_livraison_de_vingt_trois_heures_trente_reste_du_jour(
        self, cuisine_douala: Restaurant, customer: User, siege: User, plat_douala: MenuItem
    ) -> None:
        """L'autre bord de la même journée, qui basculait dans le lendemain."""
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime(2026, 9, 17, 23, 30, tzinfo=ZoneInfo(FUSEAU_DOUALA)),
            plat=plat_douala,
            reference="EC900002",
        )
        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        du_jour = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )
        du_lendemain = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 18), end=dt.date(2026, 9, 18), perimetre=perimetre
        )

        assert [ligne.orders_count for ligne in du_jour] == [1]
        assert du_lendemain == []

    def test_le_jour_de_la_ligne_est_celui_de_la_cuisine(
        self, cuisine_douala: Restaurant, customer: User, siege: User, plat_douala: MenuItem
    ) -> None:
        """Les bornes et le **regroupement** doivent employer le même fuseau.

        Trancher la fenêtre chez la cuisine puis grouper les jours en UTC
        rendrait une ligne datée du 16 dans un rapport du 17 : le total serait
        juste et la date fausse, ce qui est la pire des deux erreurs — un
        graphique journalier décalé d'un jour.
        """
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime(2026, 9, 17, 0, 30, tzinfo=ZoneInfo(FUSEAU_DOUALA)),
            plat=plat_douala,
            reference="EC900003",
        )
        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        lignes = ReportingService.revenue_by_day(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )

        assert [ligne.day for ligne in lignes] == [dt.date(2026, 9, 17)]

    def test_le_top_des_plats_suit_la_meme_borne(
        self, cuisine_douala: Restaurant, customer: User, siege: User, plat_douala: MenuItem
    ) -> None:
        """Le classement des plats se datait comme le reste — donc faux comme lui.

        Et il repose sur les lignes de commande agrégées en SQL : c'est ce qui
        le distingue du classement que le back-office calculait en mémoire, sur
        des `lines` que la forme de liste ne porte pas — un classement toujours
        vide.
        """
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime(2026, 9, 17, 0, 30, tzinfo=ZoneInfo(FUSEAU_DOUALA)),
            plat=plat_douala,
            reference="EC900004",
        )
        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        du_jour = ReportingService.top_products(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )
        de_la_veille = ReportingService.top_products(
            start=dt.date(2026, 9, 16), end=dt.date(2026, 9, 16), perimetre=perimetre
        )

        assert [(ligne.item_name, ligne.quantity_sold) for ligne in du_jour] == [
            ("Poulet braisé", 1)
        ]
        assert de_la_veille == []

    def test_l_apercu_suit_la_meme_borne(
        self, cuisine_douala: Restaurant, customer: User, siege: User, plat_douala: MenuItem
    ) -> None:
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime(2026, 9, 17, 0, 30, tzinfo=ZoneInfo(FUSEAU_DOUALA)),
            plat=plat_douala,
            reference="EC900005",
        )
        perimetre = resolve_perimetre(user=siege, params={"restaurant": cuisine_douala.slug})

        apercu = ReportingService.overview(
            start=dt.date(2026, 9, 17), end=dt.date(2026, 9, 17), perimetre=perimetre
        )

        assert apercu.orders_count == 1
        assert apercu.orders_delivered == 1
        # La fenêtre est republiée, avec le fuseau qui l'a découpée : l'écran
        # n'a plus à la deviner sur l'horloge de son poste.
        assert apercu.start == dt.date(2026, 9, 17)
        assert apercu.timezone_name == FUSEAU_DOUALA
        assert apercu.timezone_certain is True


# =========================================== la fenêtre par défaut, par l'API


class TestLaFenetreParDefaut:
    def test_sans_dates_le_rapport_porte_sur_aujourd_hui_chez_la_cuisine(
        self,
        analyste_douala: APIClient,
        cuisine_douala: Restaurant,
        customer: User,
        plat_douala: MenuItem,
    ) -> None:
        """L'écran peut ne rien envoyer — c'est le serveur qui sait quel jour il est.

        Il envoyait deux dates calculées sur `DateTime.now()`, l'horloge de son
        propre poste : un siège consultant à minuit et demi demandait les
        chiffres d'une journée qui n'avait pas commencé chez la cuisine, et
        lisait un tableau de bord vide sans explication.
        """
        aujourd_hui = aujourd_hui_chez(FUSEAU_DOUALA)
        commande_livree_a(
            restaurant=cuisine_douala,
            customer=customer,
            instant=dt.datetime.combine(
                aujourd_hui, dt.time(0, 30), tzinfo=ZoneInfo(FUSEAU_DOUALA)
            ),
            plat=plat_douala,
            reference="EC900006",
        )

        reponse = analyste_douala.get(reverse("v1:analytics:report-overview"))

        assert reponse.status_code == 200, reponse.data
        assert reponse.data["start"] == aujourd_hui.isoformat()
        assert reponse.data["end"] == aujourd_hui.isoformat()
        assert reponse.data["timezone_name"] == FUSEAU_DOUALA
        assert reponse.data["orders_delivered"] == 1

    def test_une_seule_des_deux_dates_est_refusee(self, analyste_douala: APIClient) -> None:
        """« Du 12 à aujourd'hui » et « d'aujourd'hui au 12 » sont deux fenêtres.

        Compléter d'office rendrait des chiffres pour une question que personne
        n'a posée.
        """
        reponse = analyste_douala.get(
            reverse("v1:analytics:report-overview"), {"start": "2026-09-01"}
        )

        assert reponse.status_code == 400

    def test_une_fenetre_a_l_envers_reste_refusee(self, analyste_douala: APIClient) -> None:
        reponse = analyste_douala.get(
            reverse("v1:analytics:report-overview"),
            {"start": "2026-09-30", "end": "2026-09-01"},
        )

        assert reponse.status_code == 400


class TestAujourdHuiChez:
    def test_il_peut_ne_pas_etre_le_meme_jour_partout(self) -> None:
        """Deux fuseaux, deux dates possibles au même instant.

        C'est la raison d'être de la fonction : la date du jour n'est pas une
        propriété du serveur, ni du poste qui consulte.
        """
        assert aujourd_hui_chez("Pacific/Kiritimati") >= aujourd_hui_chez("Pacific/Midway")

    def test_un_fuseau_connu_rend_une_date(self) -> None:
        assert isinstance(aujourd_hui_chez(FUSEAU_DOUALA), dt.date)


class TestPerimetreConstruit:
    def test_le_fuseau_fait_partie_de_l_identite_d_un_perimetre(self) -> None:
        """Il est **obligatoire** à la construction, et c'est délibéré.

        Un défaut à « UTC » réintroduirait le défaut en silence dans le
        prochain appelant qui l'oublierait.
        """
        perimetre = Perimetre(
            restaurant_ids=None, timezone_name=FUSEAU_DOUALA, timezone_est_certain=True
        )

        assert perimetre.timezone_name == FUSEAU_DOUALA
        assert perimetre.is_global
