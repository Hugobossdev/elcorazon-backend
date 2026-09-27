"""Duplication d'un établissement.

Ouvrir El Corazón Abidjan en repartant d'El Corazón Lomé, c'est recopier une
carte, ses options et sept plages horaires. Ces tests décrivent trois choses,
et la troisième est la plus importante :

1. ce qui est copié, et copié **complètement** — une carte à moitié arrivée
   serait pire que pas de duplication du tout, parce qu'elle a l'air complète ;
2. ce que la copie **rattache correctement** — un article de la cible pointant
   vers la catégorie de la source disparaîtrait de sa propre carte ;
3. ce qui n'est **jamais** copié : commandes, clients, livreurs, historiques.
   Il n'existe pas de section pour eux, et le test le vérifie en le demandant.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.delivery.models import CourierProfile, VehicleType
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order
from apps.restaurants.models import OpeningHours, Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"
NGN = "NGN"
ABIDJAN = Point(-4.0083, 5.3600, srid=4326)


def _zone(pays_iso: str, devise: str, ville_slug: str, centre: Point) -> DeliveryZone:
    pays = Country.objects.create(
        iso_code=pays_iso,
        name=f"Pays {pays_iso}",
        currency=devise,
        phone_prefix="+225",
        timezone="Africa/Abidjan",
    )
    ville = City.objects.create(
        country=pays, name=ville_slug.title(), slug=ville_slug, centroid=centre
    )
    lon, lat = centre.x, centre.y
    carre = Polygon(
        (
            (lon - 0.1, lat - 0.1),
            (lon + 0.1, lat - 0.1),
            (lon + 0.1, lat + 0.1),
            (lon - 0.1, lat + 0.1),
            (lon - 0.1, lat - 0.1),
        ),
        srid=4326,
    )
    return DeliveryZone.objects.create(
        city=ville,
        name=f"Centre {ville_slug}",
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(600, devise),
        fee_per_km=Money(120, devise),
    )


@pytest.fixture
def zone_abidjan() -> DeliveryZone:
    """Zone d'arrivée, même devise que la source (UEMOA)."""
    return _zone("CI", XOF, "abidjan", ABIDJAN)


@pytest.fixture
def as_siege() -> APIClient:
    compte = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
    client = APIClient()
    client.force_authenticate(compte)
    return client


@pytest.fixture
def source_garnie(restaurant: Restaurant) -> Restaurant:
    """Établissement de départ : carte, options, horaires, description.

    Deux articles dans deux catégories, dont un porte un groupe d'options à
    deux options. C'est le minimum qui rend visible une erreur de
    rattachement : avec une seule catégorie, un article mal rattaché
    atterrirait quand même au bon endroit.
    """
    restaurant.description = "Grillades au feu de bois"
    restaurant.default_preparation_minutes = 35
    restaurant.save()

    burgers = Category.objects.create(
        restaurant=restaurant, name="Burgers", slug="burgers", sort_order=1
    )
    desserts = Category.objects.create(
        restaurant=restaurant, name="Desserts", slug="desserts", sort_order=2
    )

    burger = MenuItem.objects.create(
        restaurant=restaurant,
        category=burgers,
        name="Burger Corazón",
        slug="burger-corazon",
        price=Money(3_500, XOF),
        ingredients=["boeuf", "cheddar"],
    )
    MenuItem.objects.create(
        restaurant=restaurant,
        category=desserts,
        name="Donut",
        slug="donut",
        price=Money(1_000, XOF),
    )
    # Un article retiré de la carte : il ne doit pas suivre.
    retire = MenuItem.objects.create(
        restaurant=restaurant,
        category=desserts,
        name="Ancien gâteau",
        slug="ancien-gateau",
        price=Money(2_000, XOF),
    )
    retire.delete()

    cuisson = OptionGroup.objects.create(
        menu_item=burger, name="Cuisson", min_select=1, max_select=1
    )
    Option.objects.create(group=cuisson, name="À point", price_delta=Money(0, XOF))
    Option.objects.create(group=cuisson, name="Bien cuit", price_delta=Money(0, XOF))

    # Horaires propres à ce décor, à la place de l'ouverture permanente de la
    # fixture commune : ce qu'on vérifie, c'est que ces sept plages-là suivent.
    restaurant.opening_hours.all().delete()
    for jour in range(7):
        OpeningHours.objects.create(
            restaurant=restaurant, weekday=jour, opens_at="11:00", closes_at="23:00"
        )

    return restaurant


def _corps(zone: DeliveryZone, **overrides: object) -> dict[str, object]:
    corps: dict[str, object] = {
        "name": "El Corazón Abidjan",
        "slug": "el-corazon-abidjan",
        "zone": str(zone.pk),
        "address": "Plateau, Abidjan",
        "location": {"lat": ABIDJAN.y, "lon": ABIDJAN.x},
        "phone": "+22507000000",
        "sections": ["general", "opening_hours", "catalog"],
    }
    corps.update(overrides)
    return corps


def _url(source: Restaurant) -> str:
    return reverse("v1:restaurants:managed-restaurant-duplicate", args=[source.slug])


class TestCeQuiEstCopie:
    def test_la_carte_arrive_entiere(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        reponse = as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        assert reponse.status_code == 201
        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert Category.objects.filter(restaurant=cible).count() == 2
        assert MenuItem.objects.alive().filter(restaurant=cible).count() == 2
        assert reponse.data["copied"]["catalog"] == 2

    def test_un_article_retire_ne_suit_pas(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Un établissement neuf ne naît pas avec la corbeille d'un autre."""
        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert not MenuItem.objects.filter(restaurant=cible, slug="ancien-gateau").exists()

    def test_les_articles_pointent_vers_les_categories_de_la_cible(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Le rattachement croisé est l'erreur que la duplication doit exclure.

        Un article de la cible rattaché à une catégorie de la source
        disparaîtrait de sa propre carte tout en apparaissant dans celle du
        voisin — et les deux écrans paraîtraient corrects, chacun de son côté.
        """
        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        categories_cible = set(
            Category.objects.filter(restaurant=cible).values_list("pk", flat=True)
        )
        rattachements = set(
            MenuItem.objects.filter(restaurant=cible).values_list("category_id", flat=True)
        )

        assert rattachements <= categories_cible
        assert len(rattachements) == 2

    def test_les_options_suivent_leurs_articles(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        burger = MenuItem.objects.get(restaurant=cible, slug="burger-corazon")
        groupe = OptionGroup.objects.get(menu_item=burger)

        assert groupe.name == "Cuisson"
        assert Option.objects.filter(group=groupe).count() == 2

    def test_les_horaires_ne_sont_pas_reconvertis(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """11 h à Abidjan, pas 10 h parce que Lomé ouvrait à 11 h.

        « Ouvert de 11 h à 23 h » est une décision d'exploitation locale, pas un
        instant absolu.
        """
        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        plages = OpeningHours.objects.filter(restaurant=cible)

        assert plages.count() == 7
        assert str(plages.first().opens_at) == "11:00:00"

    def test_les_informations_generales_suivent_quand_on_les_demande(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert cible.description == "Grillades au feu de bois"
        assert cible.default_preparation_minutes == 35

    def test_dupliquer_la_seule_fiche_est_valable(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Ouvrir une succursale qui aura son propre menu.

        Ce n'est pas un cas dégénéré : c'est le raccourci « repartir de la même
        identité de marque ».
        """
        as_siege.post(_url(source_garnie), _corps(zone_abidjan, sections=[]), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert MenuItem.objects.filter(restaurant=cible).count() == 0
        assert OpeningHours.objects.filter(restaurant=cible).count() == 0
        assert cible.description == ""


class TestCeQuiNEstJamaisCopie:
    """Le cœur de la garantie : aucun chemin de code ne mène là."""

    def test_les_commandes_ne_suivent_pas(
        self,
        as_siege: APIClient,
        source_garnie: Restaurant,
        zone_abidjan: DeliveryZone,
        customer: User,
    ) -> None:
        build_order(source_garnie, customer, reference="EC000001")

        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert Order.objects.filter(restaurant=cible).count() == 0

    def test_les_livreurs_ne_suivent_pas(
        self,
        as_siege: APIClient,
        source_garnie: Restaurant,
        zone_abidjan: DeliveryZone,
        courier_user: User,
    ) -> None:
        CourierProfile.objects.create(
            user=courier_user, restaurant=source_garnie, vehicle_type=VehicleType.MOTORCYCLE
        )

        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert CourierProfile.objects.filter(restaurant=cible).count() == 0

    def test_le_personnel_ne_suit_pas(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Le rattachement dit sur quoi quelqu'un travaille.

        Recopié, il donnerait à toute l'équipe de Lomé l'accès à un
        établissement d'un autre pays, sans que personne ne l'ait décidé.
        """
        gerant = User.objects.create_user(
            "gerant@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        StaffMembership.objects.create(user=gerant, restaurant=source_garnie)

        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert StaffMembership.objects.filter(restaurant=cible).count() == 0

    def test_demander_une_section_inventee_est_refuse(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Une section inconnue est refusée, jamais ignorée.

        Ignorée, une faute de frappe dans `["catalogue"]` produirait un
        établissement vide dont personne ne comprendrait pourquoi la carte n'a
        pas suivi.
        """
        reponse = as_siege.post(
            _url(source_garnie), _corps(zone_abidjan, sections=["orders"]), format="json"
        )

        assert reponse.status_code == 400


class TestGardeFous:
    def test_la_cible_nait_en_brouillon(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """La source est en service ; la copie ne l'est pas.

        Hériter d'« en service » publierait une fiche dont personne n'a vérifié
        l'adresse — et la carte recopiée lui donnerait justement l'air complète.
        """
        assert source_garnie.status == RestaurantStatus.ACTIVE

        as_siege.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        cible = Restaurant.objects.get(slug="el-corazon-abidjan")
        assert cible.status == RestaurantStatus.DRAFT
        assert cible.is_active is False

    def test_une_carte_ne_se_recopie_pas_dans_une_autre_devise(
        self, as_siege: APIClient, source_garnie: Restaurant
    ) -> None:
        """2 500 XOF recopiés en NGN seraient un prix faux d'un facteur cinq.

        Plausible, donc invisible. Convertir automatiquement serait pire : le
        taux du jour n'a pas à décider d'une politique tarifaire.
        """
        lagos = _zone("NG", NGN, "lagos", Point(3.3792, 6.5244, srid=4326))

        reponse = as_siege.post(_url(source_garnie), _corps(lagos), format="json")

        assert reponse.status_code == 409
        assert not Restaurant.objects.filter(slug="el-corazon-abidjan").exists()

    def test_la_fiche_seule_traverse_les_devises(
        self, as_siege: APIClient, source_garnie: Restaurant
    ) -> None:
        """Le refus porte sur la carte, pas sur la duplication.

        C'est ce que le message d'erreur propose, et il faut que ce soit vrai.
        """
        lagos = _zone("NG", NGN, "lagos", Point(3.3792, 6.5244, srid=4326))

        reponse = as_siege.post(
            _url(source_garnie), _corps(lagos, sections=["general"]), format="json"
        )

        assert reponse.status_code == 201

    def test_un_slug_deja_pris_est_refuse_avant_toute_ecriture(
        self, as_siege: APIClient, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        reponse = as_siege.post(
            _url(source_garnie), _corps(zone_abidjan, slug=source_garnie.slug), format="json"
        )

        assert reponse.status_code == 400
        assert Restaurant.objects.filter(slug=source_garnie.slug).count() == 1

    def test_un_gerant_ne_duplique_pas(
        self, source_garnie: Restaurant, zone_abidjan: DeliveryZone
    ) -> None:
        """Dupliquer, c'est créer : un compte cloisonné s'attribuerait un
        second périmètre en une requête."""
        gerant = User.objects.create_user(
            "chef@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        gerant.roles.add(
            Role.objects.create(
                name="Gérant", permissions=["restaurants.read", "restaurants.write"]
            )
        )
        StaffMembership.objects.create(user=gerant, restaurant=source_garnie)
        client = APIClient()
        client.force_authenticate(gerant)

        reponse = client.post(_url(source_garnie), _corps(zone_abidjan), format="json")

        assert reponse.status_code == 403
        assert not Restaurant.objects.filter(slug="el-corazon-abidjan").exists()
