"""`can_accept_order` — la règle du moment critique, et la résolution de la cuisine.

Ce que cette suite verrouille :

* **une règle, un ordre.** La cuisine (relue), la cohérence du panier, la
  desserte, les articles, les personnalisations, le barème — et le premier
  refus rendu est le plus actionnable ;
* **l'état change pendant le paiement.** Le client ouvre la carte cuisine
  ouverte ; la cuisine se met en pause avant qu'il ne valide. La commande est
  refusée — y compris quand l'appelant tient une instance lue avant la pause, et
  y compris quand la pause est déclarée *pendant* l'écriture de la commande ;
* **des refus distincts.** Aucune cuisine, adresse non desservie, cuisine
  fermée, en pause, suspendue : cinq codes, jamais une phrase à comparer ;
* **la géographie choisit une cuisine qui peut commander.** La plus proche des
  cuisines qui desservent l'adresse était retenue même fermée, pendant qu'une
  autre, ouverte, desservait la même adresse ;
* **le back-office voit ce que voit le client.** « En service » ne disait pas
  qu'une cuisine était invisible ou fermée.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.db import OperationalError, connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.availability.services import AvailabilityService, Demand
from apps.carts.services import CartService, price_cart
from apps.catalog.models import MenuItem, Option, OptionGroup
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order, PaymentMethod
from apps.orders.services import OrderService
from apps.profiles.models import Address
from apps.restaurants.availability import (
    kitchen_state,
    kitchen_unavailability,
    lock_kitchen_for_order,
)
from apps.restaurants.delivery import check_delivery
from apps.restaurants.models import OpeningHours, Restaurant, RestaurantStatus, Weekday
from common.availability import (
    AddressNotServed,
    KitchenNotOrderable,
    UnavailabilityCode,
)
from common.exceptions import BusinessRuleViolation
from common.money import Money
from tests.fixtures import ouvert_en_permanence

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
ABIDJAN = Point(-4.0083, 5.3600, srid=4326)
HORS_DE_TOUT = Point(1.60, 6.60, srid=4326)


# ================================================================ outillage


def garnir(customer: Any, restaurant: Restaurant, menu_item: MenuItem, **kwargs: Any) -> None:
    CartService.add_line(
        cart=CartService.cart_for(customer, restaurant),
        menu_item=menu_item,
        quantity=kwargs.get("quantity", 1),
        options=kwargs.get("options", []),
    )


def commander_par_l_api(client: APIClient, restaurant: Restaurant, address: Address) -> Any:
    return client.post(
        reverse("v1:orders:order-list"),
        {
            "restaurant": restaurant.slug,
            "address": str(address.pk),
            "payment_method": PaymentMethod.MOBILE_MONEY,
        },
        format="json",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def juger(restaurant: Restaurant, customer: Any, point: Point) -> Any:
    """`can_accept_order` sur le panier du client, tel que la commande l'appelle."""
    priced = price_cart(CartService.load(CartService.cart_for(customer, restaurant)))
    return AvailabilityService.can_accept_order(
        restaurant=restaurant,
        demands=[
            Demand(
                menu_item=ligne.line.menu_item, quantity=ligne.line.quantity, options=ligne.options
            )
            for ligne in priced.lines
        ],
        delivery_point=point,
        subtotal=priced.subtotal,
    )


def cuisine(zone: DeliveryZone, slug: str, location: Point, **kwargs: Any) -> Restaurant:
    return ouvert_en_permanence(
        Restaurant.objects.create(
            name=kwargs.pop("name", slug.replace("-", " ").title()),
            slug=slug,
            zone=zone,
            address="Adresse",
            location=location,
            phone="+22890000009",
            status=kwargs.pop("status", RestaurantStatus.ACTIVE),
            **kwargs,
        )
    )


def zone_carree(city: City, nom: str, centre: Point, demi_cote: float = 0.07) -> DeliveryZone:
    x, y = centre.x, centre.y
    carre = Polygon(
        (
            (x - demi_cote, y - demi_cote),
            (x + demi_cote, y - demi_cote),
            (x + demi_cote, y + demi_cote),
            (x - demi_cote, y + demi_cote),
            (x - demi_cote, y - demi_cote),
        ),
        srid=4326,
    )
    return DeliveryZone.objects.create(
        city=city,
        name=nom,
        boundary=MultiPolygon(carre, srid=4326),
        base_fee=Money(500, XOF),
        fee_per_km=Money(100, XOF),
    )


@pytest.fixture
def as_customer(customer: Any) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def as_siege() -> APIClient:
    compte = User.objects.create_superuser("siege.acceptation@elcorazon.test", "motdepasse")
    client = APIClient()
    client.force_authenticate(compte)
    return client


@pytest.fixture
def abidjan() -> City:
    pays = Country.objects.create(
        iso_code="CI",
        name="Côte d'Ivoire",
        currency=XOF,
        phone_prefix="+225",
        timezone="Africa/Abidjan",
    )
    ville = City.objects.create(country=pays, name="Abidjan", slug="abidjan", centroid=ABIDJAN)
    zone_carree(ville, "Cocody", ABIDJAN)
    return ville


# ====================================================== la règle, cas par cas


class TestUneCuisineOuverteAccepte:
    def test_active_et_ouverte_la_commande_est_creee(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        garnir(customer, restaurant, menu_item)

        reponse = commander_par_l_api(as_customer, restaurant, address)

        assert reponse.status_code == status.HTTP_201_CREATED, reponse.data
        assert Order.objects.filter(restaurant=restaurant).count() == 1

    def test_le_verdict_rend_la_cuisine_relue_et_le_devis(
        self, customer: Any, restaurant: Restaurant, address: Address, menu_item: MenuItem
    ) -> None:
        garnir(customer, restaurant, menu_item)

        verdict = juger(restaurant, customer, address.location)

        assert verdict.accepted
        assert verdict.refusal is None
        assert verdict.kitchen.pk == restaurant.pk
        assert verdict.kitchen is not restaurant
        assert verdict.quote.fee.currency == XOF


class TestLEtatDeLaCuisineRefuse:
    """Les quatre drapeaux, séparés — et un code par refus."""

    @pytest.mark.parametrize(
        ("preparer", "code"),
        [
            (
                lambda r: r.opening_hours.all().delete(),
                UnavailabilityCode.KITCHEN_CLOSED,
            ),
            (
                lambda r: Restaurant.objects.filter(pk=r.pk).update(accepts_orders=False),
                UnavailabilityCode.KITCHEN_PAUSED,
            ),
            (
                lambda r: Restaurant.objects.filter(pk=r.pk).update(
                    status=RestaurantStatus.INACTIVE, is_active=False
                ),
                UnavailabilityCode.KITCHEN_SUSPENDED,
            ),
            (
                lambda r: City.objects.filter(pk=r.zone.city_id).update(is_active=False),
                UnavailabilityCode.KITCHEN_UNPUBLISHED,
            ),
        ],
        ids=["fermee", "en-pause", "suspendue", "marche-ferme"],
    )
    def test_la_commande_est_refusee_avec_son_motif(
        self,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        preparer: Any,
        code: UnavailabilityCode,
    ) -> None:
        garnir(customer, restaurant, menu_item)
        preparer(restaurant)

        verdict = juger(restaurant, customer, address.location)

        assert not verdict.accepted
        assert isinstance(verdict.refusal, KitchenNotOrderable)
        assert verdict.refusal.unavailability.code == code

    def test_active_mais_fermee(self, restaurant: Restaurant) -> None:
        """Une cuisine peut être active et fermée : deux drapeaux, pas un."""
        restaurant.opening_hours.all().delete()

        etat = kitchen_state(Restaurant.objects.get(pk=restaurant.pk))

        assert etat.is_active
        assert not etat.is_open
        assert etat.is_accepting_orders
        assert not etat.can_accept_orders

    def test_ouverte_mais_n_acceptant_plus(self, restaurant: Restaurant) -> None:
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

        etat = kitchen_state(Restaurant.objects.get(pk=restaurant.pk))

        assert etat.is_active
        assert etat.is_open
        assert not etat.is_accepting_orders
        assert not etat.can_accept_orders

    def test_une_cuisine_suspendue_repond_409_et_non_400(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Filtré sur `is_active`, le champ `restaurant` répondait « objet
        introuvable » — une erreur de saisie, pour une cuisine que le client
        venait de voir."""
        garnir(customer, restaurant, menu_item)
        restaurant.transition_to(RestaurantStatus.INACTIVE)

        reponse = commander_par_l_api(as_customer, restaurant, address)

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["code"] == "kitchen_not_orderable"
        assert reponse.data["unavailable_code"] == UnavailabilityCode.KITCHEN_SUSPENDED
        assert not Order.objects.exists()

    def test_un_brouillon_reste_introuvable(
        self, as_customer: APIClient, zone: DeliveryZone, address: Address
    ) -> None:
        """Dire « suspendue » d'une cuisine jamais ouverte révélerait son existence."""
        brouillon = cuisine(zone, "brouillon", Point(1.24, 6.13, srid=4326), status="draft")

        reponse = commander_par_l_api(as_customer, brouillon, address)

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert "restaurant" in reponse.data["errors"]


class TestLaDesserte:
    def test_ouverte_mais_hors_zone_la_commande_est_refusee(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        garnir(customer, restaurant, menu_item)
        Address.objects.filter(pk=address.pk).update(location=HORS_DE_TOUT)

        reponse = commander_par_l_api(as_customer, restaurant, address)

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["code"] == "address_not_served"
        assert reponse.data["unavailable_code"] == UnavailabilityCode.ADDRESS_NOT_SERVED
        assert not Order.objects.exists()

    def test_hors_zone_le_stock_n_est_pas_touche(
        self, customer: Any, restaurant: Restaurant, address: Address, menu_item: MenuItem
    ) -> None:
        """La desserte était jugée **après** le décompte du stock de plats finis.

        Lire le stock après coup ne prouverait rien : la transaction annulée le
        restaure dans les deux cas. Ce qui distingue l'avant de l'après, c'est
        l'écriture elle-même — et le verrou de ligne qu'elle prend pour rien.
        """
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=3)
        garnir(customer, restaurant, menu_item)
        Address.objects.filter(pk=address.pk).update(location=HORS_DE_TOUT)
        address.refresh_from_db()

        with CaptureQueriesContext(connection) as requetes, pytest.raises(AddressNotServed):
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method=PaymentMethod.MOBILE_MONEY,
            )

        ecritures_de_stock = [
            q["sql"]
            for q in requetes.captured_queries
            if q["sql"].startswith("UPDATE") and "catalog_menuitem" in q["sql"]
        ]
        assert ecritures_de_stock == []

    def test_une_adresse_hors_zone_prime_sur_un_article_indisponible(
        self, customer: Any, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """Dire « retirez ce plat » à quelqu'un qu'on ne livre pas ne lui
        apprendrait rien."""
        garnir(customer, restaurant, menu_item)
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        verdict = juger(restaurant, customer, HORS_DE_TOUT)

        assert isinstance(verdict.refusal, AddressNotServed)


class TestLesArticles:
    def test_un_produit_indisponible_refuse_la_commande(
        self, customer: Any, restaurant: Restaurant, address: Address, menu_item: MenuItem
    ) -> None:
        garnir(customer, restaurant, menu_item)
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        verdict = juger(restaurant, customer, address.location)

        assert not verdict.accepted
        assert verdict.refusal is not None
        assert verdict.refusal.extra["unavailable_codes"] == [UnavailabilityCode.ITEM_UNAVAILABLE]

    def test_une_personnalisation_devenue_invalide_refuse_la_commande(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        option_group: OptionGroup,
        option: Option,
    ) -> None:
        """« Une cuisson » devenu « deux choix exactement » après l'ajout au
        panier : la commande passait avec une personnalisation que la cuisine
        ne sait plus préparer."""
        garnir(customer, restaurant, menu_item, options=[option])
        OptionGroup.objects.filter(pk=option_group.pk).update(min_select=2, max_select=2)

        reponse = commander_par_l_api(as_customer, restaurant, address)

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["unavailable_codes"] == [UnavailabilityCode.INVALID_CUSTOMIZATION]
        assert not Order.objects.exists()

    def test_la_personnalisation_invalide_se_refuse_encore_a_l_ajout(
        self, customer: Any, restaurant: Restaurant, menu_item: MenuItem, option_group: OptionGroup
    ) -> None:
        """La règle a changé de lieu, pas de sens : sans cuisson, l'ajout est refusé."""
        with pytest.raises(BusinessRuleViolation, match="au moins 1"):
            garnir(customer, restaurant, menu_item, options=[])

    def test_un_article_d_une_autre_cuisine_rend_le_panier_incoherent(
        self,
        customer: Any,
        restaurant: Restaurant,
        zone: DeliveryZone,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        autre = cuisine(zone, "autre-cuisine", Point(1.24, 6.13, srid=4326))

        verdict = AvailabilityService.can_accept_order(
            restaurant=autre,
            demands=[Demand(menu_item=menu_item)],
            delivery_point=address.location,
            subtotal=menu_item.price,
        )

        assert verdict.refusal is not None
        assert "autre cuisine" in verdict.refusal.detail


# ============================================= l'état change pendant le paiement


class TestLaCuisineFermePendantLePaiement:
    def test_la_carte_ouverte_puis_la_pause_puis_la_commande(
        self,
        as_customer: APIClient,
        as_siege: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Le parcours réel, route par route."""
        fiche = as_customer.get(reverse("v1:restaurants:restaurant-detail", args=[restaurant.slug]))
        carte = as_customer.get(
            reverse("v1:catalog:item-list"), {"restaurant__slug": restaurant.slug}
        )
        assert fiche.data["can_order_now"] is True
        assert carte.status_code == status.HTTP_200_OK
        garnir(customer, restaurant, menu_item)

        pause = as_siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"accepts_orders": False},
            format="json",
        )
        assert pause.status_code == status.HTTP_200_OK

        reponse = commander_par_l_api(as_customer, restaurant, address)

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["unavailable_code"] == UnavailabilityCode.KITCHEN_PAUSED
        assert not Order.objects.exists()
        assert CartService.cart_for(customer, restaurant).lines.count() == 1

    def test_une_instance_lue_avant_la_pause_ne_fait_pas_passer_la_commande(
        self, customer: Any, restaurant: Restaurant, address: Address, menu_item: MenuItem
    ) -> None:
        """La régression exacte : la commande jugeait l'instance qu'on lui passait.

        Un panier collaboratif tient son établissement depuis l'ouverture du
        panier ; lue cuisine ouverte, cette instance laissait passer la commande.
        """
        garnir(customer, restaurant, menu_item)
        lue_ouverte = (
            Restaurant.objects.select_related("zone__city__country")
            .prefetch_related("opening_hours")
            .get(pk=restaurant.pk)
        )
        assert kitchen_unavailability(lue_ouverte) is None
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

        with pytest.raises(KitchenNotOrderable) as refus:
            OrderService.create_from_selection(
                user=customer,
                restaurant=lue_ouverte,
                selection=price_cart(
                    CartService.load(CartService.cart_for(customer, restaurant))
                ).selection,
                address=address,
                payment_method=PaymentMethod.MOBILE_MONEY,
            )

        assert refus.value.unavailability.code == UnavailabilityCode.KITCHEN_PAUSED
        assert not Order.objects.exists()

    def test_la_fermeture_suit_l_horloge_et_non_la_lecture(
        self, customer: Any, restaurant: Restaurant, address: Address, menu_item: MenuItem
    ) -> None:
        """Ouverte à la lecture de la carte, fermée à l'instant de commander."""
        garnir(customer, restaurant, menu_item)
        restaurant.opening_hours.all().delete()
        OpeningHours.objects.create(
            restaurant=restaurant,
            weekday=Weekday.TUESDAY,
            opens_at=dt.time(11),
            closes_at=dt.time(14),
        )
        lome = ZoneInfo(restaurant.timezone)
        menu_consulte = dt.datetime(2026, 9, 15, 13, 55, tzinfo=lome)
        paiement = dt.datetime(2026, 9, 15, 14, 1, tzinfo=lome)
        priced = price_cart(CartService.load(CartService.cart_for(customer, restaurant)))
        demandes = [Demand(menu_item=menu_item)]

        ouvert = AvailabilityService.can_accept_order(
            restaurant=restaurant,
            demands=demandes,
            delivery_point=address.location,
            subtotal=priced.subtotal,
            at=menu_consulte,
        )
        ferme = AvailabilityService.can_accept_order(
            restaurant=restaurant,
            demands=demandes,
            delivery_point=address.location,
            subtotal=priced.subtotal,
            at=paiement,
        )

        assert ouvert.accepted
        assert isinstance(ferme.refusal, KitchenNotOrderable)
        assert ferme.refusal.unavailability.code == UnavailabilityCode.KITCHEN_CLOSED


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgis
class TestLeVerrouDeLaCuisine:
    """`transaction=True` : sous le `django_db` ordinaire, les deux connexions
    partageraient la transaction du test et ne se concurrenceraient pas."""

    def _tenir_le_verrou(
        self, restaurant: Restaurant, pris: threading.Event, liberer: threading.Event
    ) -> None:
        try:
            with transaction.atomic():
                lock_kitchen_for_order(restaurant)
                pris.set()
                liberer.wait(timeout=10)
        finally:
            connections.close_all()

    def test_une_pause_attend_la_commande_qui_tient_la_cuisine(
        self, restaurant: Restaurant
    ) -> None:
        pris, liberer = threading.Event(), threading.Event()

        with ThreadPoolExecutor(max_workers=1) as pool:
            commande = pool.submit(self._tenir_le_verrou, restaurant, pris, liberer)
            assert pris.wait(timeout=10)

            # La mise en pause ne peut pas s'écrire pendant qu'une commande juge
            # la cuisine : elle attend. `lock_timeout` rend l'attente observable.
            with pytest.raises(OperationalError), transaction.atomic():
                with connection.cursor() as curseur:
                    curseur.execute("SET LOCAL lock_timeout = '300ms'")
                Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

            liberer.set()
            commande.result(timeout=10)

        # La commande terminée, la pause passe.
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)
        verdict = kitchen_unavailability(Restaurant.objects.get(pk=restaurant.pk))
        assert verdict is not None
        assert verdict.code == UnavailabilityCode.KITCHEN_PAUSED

    def test_deux_commandes_ne_s_attendent_pas(self, restaurant: Restaurant) -> None:
        """`FOR SHARE` et non `FOR UPDATE` : au coup de feu, les commandes d'une
        même cuisine ne passent pas une par une."""
        pris, liberer = threading.Event(), threading.Event()

        with ThreadPoolExecutor(max_workers=1) as pool:
            premiere = pool.submit(self._tenir_le_verrou, restaurant, pris, liberer)
            assert pris.wait(timeout=10)

            with transaction.atomic():
                with connection.cursor() as curseur:
                    curseur.execute("SET LOCAL lock_timeout = '300ms'")
                relue = lock_kitchen_for_order(restaurant)

            liberer.set()
            premiere.result(timeout=10)

        assert relue.pk == restaurant.pk


# ====================================================== la cuisine du client


class TestLaGeographieChoisitUneCuisineQuiPeutCommander:
    def test_plusieurs_cuisines_la_plus_proche_ouverte_est_retenue(
        self, restaurant: Restaurant, zone: DeliveryZone, address: Address
    ) -> None:
        """La plus proche est fermée ; une autre, ouverte, dessert la même adresse."""
        plus_loin = cuisine(zone, "lome-nord", Point(1.2700, 6.1319, srid=4326))
        restaurant.opening_hours.all().delete()

        reponse = check_delivery(point=address.location)

        assert reponse.restaurant is not None
        assert reponse.restaurant.pk == plus_loin.pk
        assert reponse.is_available

    def test_plusieurs_cuisines_ouvertes_la_plus_proche_est_retenue(
        self, restaurant: Restaurant, zone: DeliveryZone, address: Address
    ) -> None:
        cuisine(zone, "lome-nord", Point(1.2700, 6.1319, srid=4326))

        reponse = check_delivery(point=address.location)

        assert reponse.restaurant is not None
        assert reponse.restaurant.pk == restaurant.pk

    def test_toutes_fermees_la_plus_proche_est_rendue_avec_son_motif(
        self, restaurant: Restaurant, zone: DeliveryZone, address: Address, as_customer: APIClient
    ) -> None:
        """« La cuisine de votre quartier ouvre à 11 h » est une réponse ;
        « aucune cuisine » serait faux."""
        plus_loin = cuisine(zone, "lome-nord", Point(1.2700, 6.1319, srid=4326))
        restaurant.opening_hours.all().delete()
        plus_loin.opening_hours.all().delete()

        reponse = as_customer.post(
            reverse("v1:restaurants:delivery-check"),
            {"lat": address.location.y, "lon": address.location.x},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["restaurant"]["slug"] == restaurant.slug
        assert reponse.data["restaurant"]["can_order_now"] is False
        assert reponse.data["restaurant"]["unavailable_code"] == UnavailabilityCode.KITCHEN_CLOSED
        assert reponse.data["unavailable_code"] is None

    def test_aucune_cuisine_disponible(
        self, restaurant: Restaurant, as_customer: APIClient
    ) -> None:
        reponse = as_customer.post(
            reverse("v1:restaurants:delivery-check"),
            {"lat": HORS_DE_TOUT.y, "lon": HORS_DE_TOUT.x},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["is_available"] is False
        assert reponse.data["restaurant"] is None
        assert reponse.data["unavailable_code"] == UnavailabilityCode.NO_KITCHEN_AVAILABLE

    def test_une_ville_desservie_et_une_autre_non(
        self, restaurant: Restaurant, address: Address, abidjan: City
    ) -> None:
        """Abidjan a sa zone, pas encore de cuisine : ce n'est pas « hors zone »,
        c'est « personne ici »."""
        a_lome = check_delivery(point=address.location)
        a_abidjan = check_delivery(point=ABIDJAN)

        assert a_lome.restaurant is not None
        assert a_lome.restaurant.pk == restaurant.pk
        assert a_abidjan.restaurant is None
        assert a_abidjan.unavailable_code == UnavailabilityCode.NO_KITCHEN_AVAILABLE

    def test_la_cuisine_d_une_ville_ne_dessert_pas_l_autre(
        self, restaurant: Restaurant, address: Address, abidjan: City
    ) -> None:
        cocody = cuisine(DeliveryZone.objects.get(city=abidjan), "abidjan-cocody", ABIDJAN)

        assert check_delivery(point=ABIDJAN).restaurant == cocody
        assert check_delivery(point=address.location).restaurant == restaurant
        # Demander explicitement Abidjan pour une adresse de Lomé : hors desserte.
        croisee = check_delivery(point=address.location, restaurant=cocody)
        assert croisee.unavailable_code == UnavailabilityCode.ADDRESS_NOT_SERVED

    def test_une_cuisine_suspendue_n_est_pas_choisie(
        self, restaurant: Restaurant, zone: DeliveryZone, address: Address
    ) -> None:
        plus_loin = cuisine(zone, "lome-nord", Point(1.2700, 6.1319, srid=4326))
        restaurant.transition_to(RestaurantStatus.INACTIVE)

        assert check_delivery(point=address.location).restaurant == plus_loin


# ========================================================== le back-office


class TestLeBackOfficeVoitCeQueVoitLeClient:
    def test_en_service_mais_invisible_se_voit(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        """Configurée ici, introuvable côté client : le bug fonctionnel qu'on
        ne voyait pas."""
        City.objects.filter(pk=restaurant.zone.city_id).update(is_active=False)

        fiche = as_siege.get(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug])
        )

        assert fiche.status_code == status.HTTP_200_OK
        assert fiche.data["status"] == RestaurantStatus.ACTIVE
        assert fiche.data["can_order_now"] is False
        assert fiche.data["unavailable_code"] == UnavailabilityCode.KITCHEN_UNPUBLISHED

    def test_en_service_mais_fermee_se_voit(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        restaurant.opening_hours.all().delete()

        liste = as_siege.get(reverse("v1:restaurants:managed-restaurant-list"))

        ligne = next(r for r in liste.data["results"] if r["slug"] == restaurant.slug)
        assert ligne["is_open"] is False
        assert ligne["accepts_orders"] is True
        assert ligne["unavailable_code"] == UnavailabilityCode.KITCHEN_CLOSED

    def test_une_cuisine_commandable_n_a_pas_de_motif(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        fiche = as_siege.get(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug])
        )

        assert fiche.data["can_order_now"] is True
        assert fiche.data["unavailable_code"] == ""
