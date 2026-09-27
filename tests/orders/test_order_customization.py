"""Ce que la commande retient de la personnalisation — C1, C2.

La cuisine ne lit ni le panier ni le catalogue : elle lit la commande. Si les
options n'y sont pas recopiées, le client a payé un supplément que personne ne
prépare, et un plat renommé au catalogue réécrit après coup ce qui a été
commandé.

`OrderLine.options` est donc du JSON figé là où `CartLineOption` est une clé
étrangère — le panier doit être revalidé contre le catalogue, la commande doit
lui survivre.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.carts.services import CartService
from apps.catalog.models import MenuItem, Option, OptionGroup
from apps.orders.models import Order, PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def supplements(menu_item: MenuItem) -> OptionGroup:
    return OptionGroup.objects.create(
        menu_item=menu_item, name="Suppléments", min_select=0, max_select=3
    )


@pytest.fixture
def fromage(supplements: OptionGroup) -> Option:
    return Option.objects.create(group=supplements, name="Fromage", price_delta=Money(500, XOF))


@pytest.fixture
def bacon(supplements: OptionGroup) -> Option:
    return Option.objects.create(group=supplements, name="Bacon", price_delta=Money(1_000, XOF))


@pytest.fixture
def panier_personnalise(
    customer: User,
    restaurant: Restaurant,
    menu_item: MenuItem,
    option_group: OptionGroup,
    fromage: Option,
    bacon: Option,
) -> None:
    """Deux burgers « À point », fromage et bacon — 3 500 + 1 500, ×2."""
    cuisson = Option.objects.create(group=option_group, name="À point", price_delta=Money(0, XOF))
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(
        cart=cart, menu_item=menu_item, quantity=2, options=[cuisson, fromage, bacon]
    )


def commander(client: APIClient, restaurant: Restaurant, address: Address) -> object:
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


class TestLaCommandeRetientLaPersonnalisation:
    def test_les_options_sont_recopiees_sur_la_ligne(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        panier_personnalise: None,
    ) -> None:
        """La cuisine doit savoir qu'il faut ajouter le fromage et le bacon."""
        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_201_CREATED
        ligne = response.data["lines"][0]
        retenues = {(option["group"], option["option"]) for option in ligne["options"]}
        assert retenues == {
            ("Cuisson", "À point"),
            ("Suppléments", "Fromage"),
            ("Suppléments", "Bacon"),
        }

    def test_l_ecart_de_prix_de_chaque_option_est_conserve(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        panier_personnalise: None,
    ) -> None:
        """Sans le montant, la commande ne se relit plus : on saurait *quoi*
        a été ajouté, pas ce qu'il a été facturé."""
        response = commander(as_customer, restaurant, address)

        ecarts = {
            option["option"]: (option["delta"], option["currency"])
            for option in response.data["lines"][0]["options"]
        }
        assert ecarts["Fromage"] == (500, XOF)
        assert ecarts["Bacon"] == (1_000, XOF)
        assert ecarts["À point"] == (0, XOF)

    def test_le_total_integre_les_options(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        panier_personnalise: None,
    ) -> None:
        """C2 — (3 500 + 500 + 1 000) × 2 = 10 000, recomposé serveur."""
        response = commander(as_customer, restaurant, address)

        ligne = response.data["lines"][0]
        assert ligne["unit_price"] == {"amount": "5000", "currency": XOF}
        assert ligne["line_total"] == {"amount": "10000", "currency": XOF}
        assert response.data["subtotal"] == {"amount": "10000", "currency": XOF}

    def test_un_renommage_au_catalogue_ne_reecrit_pas_la_commande(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        fromage: Option,
        menu_item: MenuItem,
        panier_personnalise: None,
    ) -> None:
        """Copie figée : l'exploitation renomme, la commande reste lisible
        telle qu'elle a été passée."""
        reference = commander(as_customer, restaurant, address).data["reference"]

        Option.objects.filter(pk=fromage.pk).update(name="Cheddar affiné")
        MenuItem.objects.filter(pk=menu_item.pk).update(name="Burger Signature")

        commande = Order.objects.get(reference=reference)
        ligne = commande.lines.get()
        assert ligne.item_name == "Burger Corazón"
        assert {option["option"] for option in ligne.options} == {
            "À point",
            "Fromage",
            "Bacon",
        }

    def test_une_option_supprimee_du_catalogue_laisse_la_commande_intacte(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        bacon: Option,
        panier_personnalise: None,
    ) -> None:
        """Le pire cas de la copie par référence : la commande deviendrait
        illisible au premier ménage du catalogue."""
        reference = commander(as_customer, restaurant, address).data["reference"]

        Option.objects.filter(pk=bacon.pk).delete()

        ligne = Order.objects.get(reference=reference).lines.get()
        assert "Bacon" in {option["option"] for option in ligne.options}

    def test_la_personnalisation_survit_a_la_relecture_de_la_commande(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        panier_personnalise: None,
    ) -> None:
        """Elle ne dépend d'aucun état local : le client la relit du serveur."""
        reference = commander(as_customer, restaurant, address).data["reference"]
        commande = Order.objects.get(reference=reference)

        response = as_customer.get(reverse("v1:orders:order-detail", args=[str(commande.pk)]))

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["lines"][0]["options"]) == 3

    def test_deux_personnalisations_differentes_font_deux_lignes_de_commande(
        self,
        as_customer: APIClient,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        option_group: OptionGroup,
        fromage: Option,
    ) -> None:
        """Le même burger, avec et sans fromage : la cuisine en prépare deux
        différents, la commande doit les distinguer."""
        cuisson = Option.objects.create(
            group=option_group, name="À point", price_delta=Money(0, XOF)
        )
        cart = CartService.cart_for(customer, restaurant)
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[cuisson])
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[cuisson, fromage])

        response = commander(as_customer, restaurant, address)

        assert len(response.data["lines"]) == 2
        tailles = sorted(len(ligne["options"]) for ligne in response.data["lines"])
        assert tailles == [1, 2]

    def test_un_article_sans_option_reste_commandable(
        self,
        as_customer: APIClient,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Aucun groupe sur cet article : la personnalisation ne doit pas être
        devenue un passage obligé."""
        cart = CartService.cart_for(customer, restaurant)
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])

        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["lines"][0]["options"] == []
