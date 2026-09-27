"""L'écran des encaissements : filtres du serveur et totaux **par devise**.

L'écran chargeait tout l'historique de son périmètre — page après page — puis
filtrait en mémoire et additionnait ce qui se trouvait là. Deux conséquences :
sa recherche ne portait que sur ce qu'il avait reçu, et « Total encaissé »
additionnait des XOF et des XAF sous un même « FCFA ».
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.restaurants.models import Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from tests.fixtures import LOME, build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LISTE = "v1:payments:transaction-list"
RECAPITULATIF = "v1:payments:transaction-summary"


@pytest.fixture
def douala(zone: DeliveryZone) -> Restaurant:
    pays = Country.objects.create(
        iso_code="CM", name="Cameroun", currency="XAF", phone_prefix="+237", timezone="UTC"
    )
    ville = City.objects.create(country=pays, name="Douala", slug="douala", centroid=LOME)
    akwa = DeliveryZone.objects.create(
        city=ville,
        name="Akwa",
        boundary=zone.boundary,
        base_fee=Money(500, "XAF"),
        fee_per_km=Money(0, "XAF"),
    )
    return Restaurant.objects.create(
        name="Douala",
        slug="douala",
        zone=akwa,
        address="Akwa",
        location=LOME,
        phone="+237600000000",
        status=RestaurantStatus.ACTIVE,
    )


@pytest.fixture
def siege() -> APIClient:
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege.caisse@elcorazon.test", "x"))
    return client


@pytest.fixture
def cuisinier(restaurant: Restaurant) -> APIClient:
    """Un compte du personnel sans `orders.read` : il ne lit pas la caisse."""
    membre = User.objects.create_user(
        "cuisine.caisse@elcorazon.test", "x", full_name="Cuisine", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name="Cuisine", permissions=["catalog.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


def encaissement(
    restaurant: Restaurant,
    customer: User,
    reference: str,
    montant: int,
    *,
    statut: str = PaymentStatus.COMPLETED,
) -> Transaction:
    devise = restaurant.zone.city.country.currency
    commande: Order = build_order(
        restaurant,
        customer,
        reference=reference,
        subtotal=Money(montant - 500, devise),
        delivery_fee=Money(500, devise),
        discount=Money(0, devise),
        total=Money(montant, devise),
    )
    return Transaction.objects.create(
        order=commande,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference=f"ref-{reference}",
        amount=Money(montant, devise),
        status=statut,
    )


class TestRecapitulatif:
    def test_les_devises_ne_s_additionnent_pas(
        self, restaurant: Restaurant, douala: Restaurant, customer: User, siege: APIClient
    ) -> None:
        encaissement(restaurant, customer, "EC700001", 4_000)
        encaissement(restaurant, customer, "EC700002", 2_000)
        encaissement(douala, customer, "EC700003", 10_000)
        encaissement(restaurant, customer, "EC700004", 9_000, statut=PaymentStatus.FAILED)

        reponse = siege.get(reverse(RECAPITULATIF))

        assert reponse.status_code == status.HTTP_200_OK
        par_devise = {ligne["currency"]: ligne for ligne in reponse.data["collected"]}
        assert par_devise["XOF"]["amount_minor"] == 6_000
        assert par_devise["XOF"]["transactions"] == 2
        assert par_devise["XAF"]["amount_minor"] == 10_000
        # Les échecs comptent dans les statuts, jamais dans l'encaissé.
        assert reponse.data["by_status"][PaymentStatus.FAILED] == 1
        assert reponse.data["transactions"] == 4

    def test_le_recapitulatif_suit_les_filtres_de_la_liste(
        self, restaurant: Restaurant, douala: Restaurant, customer: User, siege: APIClient
    ) -> None:
        encaissement(restaurant, customer, "EC700005", 4_000)
        encaissement(douala, customer, "EC700006", 10_000)

        reponse = siege.get(reverse(RECAPITULATIF), {"order__restaurant__slug": douala.slug})

        assert [ligne["currency"] for ligne in reponse.data["collected"]] == ["XAF"]
        assert reponse.data["transactions"] == 1

    def test_le_perimetre_du_compte_s_applique(
        self, restaurant: Restaurant, douala: Restaurant, customer: User
    ) -> None:
        encaissement(restaurant, customer, "EC700007", 4_000)
        encaissement(douala, customer, "EC700008", 10_000)
        gerant = User.objects.create_user(
            "gerant.caisse@elcorazon.test", "x", full_name="Gérant", user_type=UserType.STAFF
        )
        gerant.roles.add(Role.objects.create(name="Caisse Lomé", permissions=["orders.read"]))
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(gerant)

        reponse = client.get(reverse(RECAPITULATIF))

        assert [ligne["currency"] for ligne in reponse.data["collected"]] == ["XOF"]
        assert reponse.data["transactions"] == 1

    def test_sans_orders_read_la_caisse_reste_fermee(self, cuisinier: APIClient) -> None:
        assert cuisinier.get(reverse(RECAPITULATIF)).status_code == status.HTTP_403_FORBIDDEN


class TestFiltres:
    def test_la_recherche_porte_sur_la_reference_du_prestataire(
        self, restaurant: Restaurant, customer: User, siege: APIClient
    ) -> None:
        encaissement(restaurant, customer, "EC700009", 4_000)
        encaissement(restaurant, customer, "EC700010", 2_000)

        reponse = siege.get(reverse(LISTE), {"search": "ref-EC700009"})

        assert reponse.data["count"] == 1
        assert reponse.data["results"][0]["provider_reference"] == "ref-EC700009"

    def test_la_periode_filtre_cote_serveur(
        self, restaurant: Restaurant, customer: User, siege: APIClient
    ) -> None:
        ancienne = encaissement(restaurant, customer, "EC700011", 4_000)
        Transaction.objects.filter(pk=ancienne.pk).update(
            created_at=timezone.now() - dt.timedelta(days=40)
        )
        encaissement(restaurant, customer, "EC700012", 2_000)

        depuis = (timezone.now() - dt.timedelta(days=7)).isoformat()
        reponse = siege.get(reverse(LISTE), {"created_at__gte": depuis})

        assert reponse.data["count"] == 1
        assert reponse.data["results"][0]["provider_reference"] == "ref-EC700012"

    def test_le_statut_annule_est_filtrable(
        self, restaurant: Restaurant, customer: User, siege: APIClient
    ) -> None:
        """L'écran ne le proposait pas : une transaction annulée n'apparaissait
        sous aucun filtre."""
        encaissement(restaurant, customer, "EC700013", 4_000, statut=PaymentStatus.CANCELLED)

        reponse = siege.get(reverse(LISTE), {"status": PaymentStatus.CANCELLED})

        assert reponse.data["count"] == 1
