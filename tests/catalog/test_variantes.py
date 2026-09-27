"""Variantes de produit (lot 2, 2026-09-25) — Petite, Moyenne, Grande.

Une variante n'est pas une option : elle **remplace** le prix de base (décision
du 2026-09-25 : prix absolu), et les options s'ajoutent par-dessus. Un article
qui a des variantes actives en exige une ; un article qui n'en a pas n'en
accepte aucune. Le serveur valorise, refuse et fige : rien ne vient du client.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.carts.services import CartService
from apps.catalog.models import Category, MenuItem, Option, OptionGroup, Variant
from apps.orders.models import PaymentMethod
from apps.orders.services import OrderService
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant, StaffMembership
from apps.restaurants.states import RestaurantStatus
from common.models import AuditEntry
from common.money import Money
from tests.fixtures import XOF

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def pizza(restaurant: Restaurant, category: Category) -> MenuItem:
    return MenuItem.objects.create(
        restaurant=restaurant,
        category=category,
        name="Pizza Reine",
        slug="pizza-reine",
        price=Money(2_000, XOF),
    )


@pytest.fixture
def tailles(pizza: MenuItem) -> dict[str, Variant]:
    return {
        nom: Variant.objects.create(menu_item=pizza, name=nom, price=Money(prix, XOF), sort_order=i)
        for i, (nom, prix) in enumerate((("Petite", 2_000), ("Moyenne", 2_500), ("Grande", 3_000)))
    }


@pytest.fixture
def fromage(pizza: MenuItem) -> Option:
    groupe = OptionGroup.objects.create(
        menu_item=pizza, name="Suppléments", min_select=0, max_select=3
    )
    return Option.objects.create(group=groupe, name="Fromage", price_delta=Money(500, XOF))


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


def ajouter(client: APIClient, restaurant: Restaurant, item: MenuItem, **extra: object) -> object:
    corps: dict[str, object] = {"menu_item": str(item.pk), "quantity": 1, "options": []}
    corps.update(extra)
    return client.post(
        reverse("v1:carts:cart-add-line", args=[restaurant.slug]), corps, format="json"
    )


class TestPrix:
    def test_la_variante_remplace_le_prix_de_base_et_les_options_s_ajoutent(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        pizza: MenuItem,
        tailles: dict[str, Variant],
        fromage: Option,
    ) -> None:
        reponse = ajouter(
            as_customer,
            restaurant,
            pizza,
            variant=str(tailles["Grande"].pk),
            options=[str(fromage.pk)],
            quantity=2,
        )

        assert reponse.status_code == 201, reponse.data
        ligne = reponse.data["lines"][0]
        assert ligne["variant"] == str(tailles["Grande"].pk)
        assert ligne["variant_name"] == "Grande"
        assert ligne["unit_price"] == {"amount": "3500", "currency": "XOF"}  # 3 000 + 500
        assert reponse.data["subtotal"] == {"amount": "7000", "currency": "XOF"}

    def test_deux_tailles_font_deux_lignes_la_meme_taille_fusionne(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        pizza: MenuItem,
        tailles: dict[str, Variant],
    ) -> None:
        ajouter(as_customer, restaurant, pizza, variant=str(tailles["Petite"].pk))
        ajouter(as_customer, restaurant, pizza, variant=str(tailles["Grande"].pk))
        reponse = ajouter(as_customer, restaurant, pizza, variant=str(tailles["Grande"].pk))

        lignes = {ligne["variant_name"]: ligne["quantity"] for ligne in reponse.data["lines"]}
        assert lignes == {"Petite": 1, "Grande": 2}


class TestRegles:
    def test_un_article_a_variantes_en_exige_une(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        pizza: MenuItem,
        tailles: dict[str, Variant],
    ) -> None:
        reponse = ajouter(as_customer, restaurant, pizza)

        assert reponse.status_code == 409
        assert "choisissez" in reponse.data["detail"].lower()

    def test_la_variante_d_un_autre_article_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        tailles: dict[str, Variant],
        menu_item: MenuItem,
    ) -> None:
        Variant.objects.create(menu_item=menu_item, name="Double", price=Money(4_500, XOF))

        reponse = ajouter(as_customer, restaurant, menu_item, variant=str(tailles["Petite"].pk))

        assert reponse.status_code == 409

    def test_un_article_sans_variante_n_en_accepte_aucune(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        tailles: dict[str, Variant],
    ) -> None:
        reponse = ajouter(as_customer, restaurant, menu_item, variant=str(tailles["Petite"].pk))

        assert reponse.status_code == 409

    def test_une_variante_eteinte_rend_la_ligne_incommandable(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        pizza: MenuItem,
        tailles: dict[str, Variant],
        as_customer: APIClient,
    ) -> None:
        ajouter(as_customer, restaurant, pizza, variant=str(tailles["Moyenne"].pk))
        Variant.objects.filter(pk=tailles["Moyenne"].pk).update(is_available=False)

        panier = as_customer.get(reverse("v1:carts:cart-detail", args=[restaurant.slug]))
        assert panier.data["is_orderable"] is False
        assert panier.data["lines"][0]["unavailable_code"] == "variant_unavailable"

        with pytest.raises(Exception):  # noqa: B017 - refus métier, quel qu'en soit le type exact
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method=PaymentMethod.CASH,
            )


class TestCommande:
    def test_la_ligne_de_commande_fige_la_variante(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        pizza: MenuItem,
        tailles: dict[str, Variant],
        as_customer: APIClient,
    ) -> None:
        ajouter(as_customer, restaurant, pizza, variant=str(tailles["Moyenne"].pk))

        commande = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method=PaymentMethod.CASH,
        )
        # Le catalogue change ensuite : la commande ne doit pas bouger.
        Variant.objects.filter(pk=tailles["Moyenne"].pk).update(name="Familiale", price_minor=9_000)
        Variant.objects.filter(pk=tailles["Moyenne"].pk).delete()

        ligne = commande.lines.get()
        ligne.refresh_from_db()
        assert ligne.variant_name == "Moyenne"
        assert ligne.unit_price == Money(2_500, XOF)


class TestCatalogue:
    def test_la_fiche_publie_les_variantes_actives_dans_leur_ordre(
        self, as_customer: APIClient, pizza: MenuItem, tailles: dict[str, Variant]
    ) -> None:
        Variant.objects.filter(pk=tailles["Petite"].pk).update(is_active=False)

        fiche = as_customer.get(reverse("v1:catalog:item-detail", args=[pizza.pk]))

        assert [v["name"] for v in fiche.data["variants"]] == ["Moyenne", "Grande"]
        assert fiche.data["variants"][0]["price"] == {"amount": "2500", "currency": "XOF"}

    def test_la_liste_porte_les_tailles_sans_une_requete_par_carte(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        category: Category,
        pizza: MenuItem,
        tailles: dict[str, Variant],
    ) -> None:
        # Sans les tailles, une carte ne sait ni qu'il faut choisir avant
        # d'ajouter, ni reprendre une commande passée dans sa taille.
        url = reverse("v1:catalog:item-list")

        def plat_a_tailles(i: int) -> None:
            autre = MenuItem.objects.create(
                restaurant=restaurant,
                category=category,
                name=f"Pizza {i}",
                slug=f"pizza-{i}",
                price=Money(2_000, XOF),
            )
            Variant.objects.create(menu_item=autre, name="Grande", price=Money(3_000, XOF))

        def compter() -> tuple[int, object]:
            with CaptureQueriesContext(connection) as capture:
                reponse = as_customer.get(url)
            return len(capture.captured_queries), reponse

        plat_a_tailles(1)
        avant, liste = compter()
        for i in range(2, 7):
            plat_a_tailles(i)
        apres, _ = compter()

        cartes = {c["name"]: c for c in liste.data["results"]}  # type: ignore[attr-defined]
        assert [v["name"] for v in cartes["Pizza Reine"]["variants"]] == [
            "Petite",
            "Moyenne",
            "Grande",
        ]
        assert apres == avant


class TestGestion:
    LISTE = "v1:catalog:managed-variant-list"
    FICHE = "v1:catalog:managed-variant-detail"

    @pytest.fixture
    def siege(self) -> APIClient:
        client = APIClient()
        client.force_authenticate(User.objects.create_superuser("siege-var@elcorazon.test", "x"))
        return client

    def test_creer_journalise(self, siege: APIClient, pizza: MenuItem) -> None:
        reponse = siege.post(
            reverse(self.LISTE),
            {
                "menu_item": str(pizza.pk),
                "name": "XL",
                "price": {"amount": "4000", "currency": "XOF"},
            },
            format="json",
        )

        assert reponse.status_code == 201, reponse.data
        assert AuditEntry.objects.filter(
            action="variant.create", target_id=reponse.data["id"]
        ).exists()

    def test_un_prix_dans_une_autre_devise_est_refuse(
        self, siege: APIClient, pizza: MenuItem
    ) -> None:
        reponse = siege.post(
            reverse(self.LISTE),
            {
                "menu_item": str(pizza.pk),
                "name": "XL",
                "price": {"amount": "40", "currency": "EUR"},
            },
            format="json",
        )

        assert reponse.status_code == 400

    def test_modifier_desactiver_supprimer_journalise(
        self, siege: APIClient, tailles: dict[str, Variant]
    ) -> None:
        fiche = reverse(self.FICHE, args=[tailles["Grande"].pk])

        siege.patch(fiche, {"price": {"amount": "3200", "currency": "XOF"}}, format="json")
        siege.patch(fiche, {"is_active": False}, format="json")
        siege.delete(fiche)

        actions = list(
            AuditEntry.objects.filter(target_id=str(tailles["Grande"].pk))
            .order_by("created_at")
            .values_list("action", flat=True)
        )
        assert actions == ["variant.update", "variant.activation", "variant.delete"]

    def test_une_autre_cuisine_ne_voit_ni_ne_touche(
        self, restaurant: Restaurant, tailles: dict[str, Variant], pizza: MenuItem
    ) -> None:
        voisine = Restaurant.objects.create(
            name="Voisine",
            slug="voisine-var",
            zone=restaurant.zone,
            address="x",
            location=restaurant.location,
            phone="+22890000006",
            status=RestaurantStatus.ACTIVE,
        )
        compte = User.objects.create_user("chef-var@elcorazon.test", "x", user_type=UserType.STAFF)
        compte.roles.add(
            Role.objects.create(name="Chef var", permissions=["catalog.read", "catalog.write"])
        )
        StaffMembership.objects.create(user=compte, restaurant=voisine)
        chef = APIClient()
        chef.force_authenticate(compte)

        fiche = reverse(self.FICHE, args=[tailles["Grande"].pk])
        assert chef.get(fiche).status_code == 404
        assert chef.patch(fiche, {"is_active": False}, format="json").status_code == 404
        assert chef.delete(fiche).status_code == 404
        creation = chef.post(
            reverse(self.LISTE),
            {
                "menu_item": str(pizza.pk),
                "name": "XXL",
                "price": {"amount": "5000", "currency": "XOF"},
            },
            format="json",
        )
        assert creation.status_code in (403, 404)
