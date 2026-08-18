"""Fixtures partagées du domaine.

Un jeu minimal mais **complet** : chaque entité porte son rattachement
géographique, conformément à l'ADR-006. C'est volontaire — si les tests
pouvaient créer une commande sans restaurant, le chemin multi-site cesserait
d'être exercé et la première ouverture d'un second établissement révélerait des
trous.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon

from apps.accounts.models import User, UserType
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.delivery.models import CourierProfile, VehicleType
from apps.delivery.states import VerificationStatus
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order, PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.money import Money

LOME = Point(1.2255, 6.1319, srid=4326)
XOF = "XOF"


@pytest.fixture
def country() -> Country:
    return Country.objects.create(
        iso_code="TG", name="Togo", currency=XOF, phone_prefix="+228", timezone="Africa/Lome"
    )


@pytest.fixture
def city(country: Country) -> City:
    return City.objects.create(country=country, name="Lomé", slug="lome", centroid=LOME)


@pytest.fixture
def zone(city: City) -> DeliveryZone:
    square = Polygon(
        ((1.15, 6.08), (1.30, 6.08), (1.30, 6.22), (1.15, 6.22), (1.15, 6.08)), srid=4326
    )
    return DeliveryZone.objects.create(
        city=city,
        name="Centre",
        boundary=MultiPolygon(square, srid=4326),
        base_fee=Money(500, XOF),
        fee_per_km=Money(100, XOF),
    )


@pytest.fixture
def restaurant(zone: DeliveryZone) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón",
        slug="el-corazon-lome",
        zone=zone,
        address="Lomé",
        location=LOME,
        phone="+22890000000",
    )


@pytest.fixture
def customer() -> User:
    return User.objects.create_user("cliente@elcorazon.test", "motdepasse", full_name="Ama Koffi")


@pytest.fixture
def courier_user() -> User:
    return User.objects.create_user(
        "livreur@elcorazon.test",
        "motdepasse",
        full_name="Kodjo Mensah",
        user_type=UserType.COURIER,
    )


@pytest.fixture
def courier(courier_user: User, restaurant: Restaurant) -> CourierProfile:
    """Livreur **validé et en ligne** : le cas nominal.

    Les tests qui exercent L1 dégradent explicitement l'un des deux critères,
    ce qui rend visible celui qu'ils vérifient.
    """
    return CourierProfile.objects.create(
        user=courier_user,
        restaurant=restaurant,
        vehicle_type=VehicleType.MOTORCYCLE,
        verification_status=VerificationStatus.APPROVED,
        is_online=True,
    )


@pytest.fixture
def address(customer: User, city: City) -> Address:
    """Adresse dans la zone du restaurant, à environ 1,1 km de celui-ci.

    La distance est réelle et non nulle : une adresse posée sur le restaurant
    ferait passer tous les tests de frais kilométriques sans rien vérifier.
    """
    return Address.objects.create(
        user=customer,
        label="Maison",
        line1="Rue du Commerce",
        landmark="En face de la pharmacie Bel Air",
        city=city,
        location=Point(1.2355, 6.1319, srid=4326),
        recipient_phone="+22890111111",
        is_default=True,
    )


@pytest.fixture
def category(restaurant: Restaurant) -> Category:
    return Category.objects.create(
        restaurant=restaurant, name="Burgers", slug="burgers", emoji="🍔"
    )


@pytest.fixture
def menu_item(restaurant: Restaurant, category: Category) -> MenuItem:
    return MenuItem.objects.create(
        restaurant=restaurant,
        category=category,
        name="Burger Corazón",
        slug="burger-corazon",
        price=Money(3_500, XOF),
    )


@pytest.fixture
def option_group(menu_item: MenuItem) -> OptionGroup:
    return OptionGroup.objects.create(
        menu_item=menu_item, name="Cuisson", min_select=1, max_select=1
    )


@pytest.fixture
def option(option_group: OptionGroup) -> Option:
    return Option.objects.create(
        group=option_group, name="À point", price_delta=Money(0, XOF), is_default=True
    )


def build_order(restaurant: Restaurant, customer: User, **overrides: object) -> Order:
    """Commande cohérente : total = sous-total + frais − remise."""
    defaults: dict[str, object] = {
        "reference": "EC000001",
        "restaurant": restaurant,
        "customer": customer,
        "delivery_address_line": "Rue du Commerce, Lomé",
        "delivery_location": {"lat": 6.1319, "lon": 1.2255},
        "recipient_name": customer.full_name,
        "recipient_phone": "+22890111111",
        "subtotal": Money(3_500, XOF),
        "delivery_fee": Money(500, XOF),
        "discount": Money(0, XOF),
        "total": Money(4_000, XOF),
        "payment_method": PaymentMethod.MOBILE_MONEY,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


@pytest.fixture
def order(restaurant: Restaurant, customer: User) -> Order:
    return build_order(restaurant, customer)
