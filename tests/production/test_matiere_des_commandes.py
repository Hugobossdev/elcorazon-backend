"""La matière d'une commande — promise, sortie, rendue.

C'est ici que « préparer » cesse d'être une écriture de colonne. Ce que cette
suite verrouille :

* **la promesse** — une commande créée immobilise la matière, sans la sortir ;
* **la rupture** — un ingrédient manquant refuse la commande, et n'en laisse
  aucune trace ;
* **le feu** — le passage en préparation sort la matière *et* libère
  l'engagement, faute de quoi elle serait comptée deux fois ;
* **l'asymétrie de l'annulation** — avant le feu on rend, après on ne rend plus.
  On ne décuisine pas un oignon ;
* **la bascule** — un ingrédient non suivi, un plat sans recette et une commande
  antérieure à `option_id` traversent tous sans rien casser.
"""

from __future__ import annotations

import pytest

from apps.carts.services import CartService
from apps.catalog.models import MenuItem, Option, OptionGroup
from apps.inventory.models import Ingredient, MovementKind, StockItem, StockMovement
from apps.inventory.services import InventoryService
from apps.orders.models import Order, OrderLine, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.production.models import Recipe, RecipeIngredient
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation, InsufficientStock
from common.money import Money
from common.quantities import Dimension, Quantity

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def g(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "g")


@pytest.fixture
def oignon() -> Ingredient:
    return Ingredient.objects.create(name="Oignon", slug="oignon", dimension=Dimension.MASS)


@pytest.fixture
def fromage() -> Ingredient:
    return Ingredient.objects.create(name="Fromage", slug="fromage", dimension=Dimension.MASS)


@pytest.fixture
def stock_oignon(restaurant: Restaurant, oignon: Ingredient) -> StockItem:
    """Un kilo d'oignon en chambre froide."""
    item = InventoryService.open_item(restaurant=restaurant, ingredient=oignon)
    InventoryService.receive(item=item, quantity=g("1000"), unit_cost=Money(2, "XOF"))
    item.refresh_from_db()
    return item


@pytest.fixture
def stock_fromage(restaurant: Restaurant, fromage: Ingredient) -> StockItem:
    item = InventoryService.open_item(restaurant=restaurant, ingredient=fromage)
    InventoryService.receive(item=item, quantity=g("500"), unit_cost=Money(8, "XOF"))
    item.refresh_from_db()
    return item


@pytest.fixture
def recette_du_plat(menu_item: MenuItem, oignon: Ingredient) -> Recipe:
    """20 g d'oignon par portion."""
    recette = Recipe.objects.create(menu_item=menu_item)
    RecipeIngredient.objects.create(recipe=recette, ingredient=oignon, quantity=g("20"))
    return recette


@pytest.fixture
def supplement_fromage(menu_item: MenuItem, fromage: Ingredient) -> Option:
    """Un supplément qui consomme 30 g de fromage."""
    groupe = OptionGroup.objects.create(
        menu_item=menu_item, name="Suppléments", min_select=0, max_select=2
    )
    option = Option.objects.create(group=groupe, name="Fromage", price_delta=Money(500, "XOF"))
    recette = Recipe.objects.create(option=option)
    RecipeIngredient.objects.create(recipe=recette, ingredient=fromage, quantity=g("30"))
    return option


def commander(
    customer: object,
    restaurant: Restaurant,
    address: Address,
    menu_item: MenuItem,
    *,
    quantity: int = 1,
    options: list[Option] | None = None,
) -> Order:
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=menu_item, quantity=quantity, options=options or [])
    return OrderService.create_from_cart(
        user=customer,
        cart=cart,
        address=address,
        payment_method=PaymentMethod.MOBILE_MONEY,
    )


class TestLaPromesse:
    def test_creer_une_commande_reserve_la_matiere_sans_la_sortir(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """Entre la commande et le feu, la matière est due sans être partie."""
        commander(customer, restaurant, address, menu_item, quantity=2)

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("1000")
        assert stock_oignon.reserved == g("40")
        assert stock_oignon.available == g("960")

    def test_le_mouvement_porte_la_reference_de_la_commande(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """« Pourquoi 20 g sont-ils immobilisés ? » doit avoir une réponse."""
        order = commander(customer, restaurant, address, menu_item)

        mouvement = StockMovement.objects.get(
            stock_item=stock_oignon, kind=MovementKind.RESERVATION
        )
        assert mouvement.reference == order.reference

    def test_les_options_consomment_aussi_leur_matiere(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_fromage: StockItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
        supplement_fromage: Option,
    ) -> None:
        """Le supplément n'est pas qu'un écart de prix : il déplace de la matière."""
        commander(customer, restaurant, address, menu_item, options=[supplement_fromage])

        stock_fromage.refresh_from_db()
        assert stock_fromage.reserved == g("30")


class TestLaRupture:
    def test_un_ingredient_manquant_refuse_la_commande(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        oignon: Ingredient,
        recette_du_plat: Recipe,
    ) -> None:
        """La rupture doit se voir — c'est tout l'objet de ce lot.

        Dix grammes en stock, vingt demandés : la commande n'a pas lieu, et rien
        n'en subsiste. L'atomicité s'en charge, et ce test la verrouille.

        Le refus vient du **juge de disponibilité**, avant toute écriture : il
        nomme le plat et son motif, là où `InsufficientStock` nommait
        l'ingrédient — une information de cuisine. Le refus sous verrou, lui,
        reste celui de `reserve` : voir le test suivant.
        """
        item = InventoryService.open_item(restaurant=restaurant, ingredient=oignon)
        InventoryService.receive(item=item, quantity=g("10"))

        with pytest.raises(BusinessRuleViolation) as refus:
            commander(customer, restaurant, address, menu_item)

        assert refus.value.extra["unavailable"] == [menu_item.name]
        assert refus.value.extra["unavailable_codes"] == ["ingredient_shortage"]
        assert not Order.objects.exists()
        item.refresh_from_db()
        assert item.reserved == g("0")

    def test_la_lecture_informe_la_reservation_tranche(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        oignon: Ingredient,
        recette_du_plat: Recipe,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Le juge lit le stock **sans verrou** ; seule la réservation fait foi.

        Entre la lecture du juge et l'écriture de la commande, une autre
        commande peut emporter le dernier oignon. On le simule en rendant la
        lecture aveugle : elle annonce que rien ne manque, et c'est alors
        `reserve`, sous verrou, qui doit refuser — sans rien laisser derrière.

        Sans ce test, la lecture anticipée aurait masqué le seul refus qui
        tienne face à deux commandes simultanées : il ne serait plus jamais
        exercé au niveau de la commande.
        """
        from apps.production.services import MaterialService

        item = InventoryService.open_item(restaurant=restaurant, ingredient=oignon)
        InventoryService.receive(item=item, quantity=g("10"))
        monkeypatch.setattr(
            MaterialService,
            "shortages",
            staticmethod(lambda *, restaurant_id, lines, independent=False: [None] * len(lines)),
        )

        with pytest.raises(InsufficientStock):
            commander(customer, restaurant, address, menu_item)

        assert not Order.objects.exists()
        item.refresh_from_db()
        assert item.reserved == g("0")

    def test_un_ingredient_non_suivi_ne_bloque_rien(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        recette_du_plat: Recipe,
    ) -> None:
        """Aucune ligne de stock pour cet oignon : on ne décompte pas ce qu'on ne compte pas.

        C'est la règle que `MenuItem.tracks_stock` applique déjà aux plats
        finis. La bascule se fait par étapes, et aucune ne doit fermer la
        boutique.
        """
        order = commander(customer, restaurant, address, menu_item)

        assert order.status == OrderStatus.PENDING
        assert not StockMovement.objects.exists()


class TestLeFeu:
    def test_la_preparation_sort_la_matiere_et_libere_la_promesse(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """Sans la libération, la matière serait comptée deux fois — due et partie."""
        order = commander(customer, restaurant, address, menu_item)
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("980")
        assert stock_oignon.reserved == g("0")
        assert stock_oignon.available == g("980")

    def test_le_journal_garde_les_deux_ecritures(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """Un journal en ajout seul : la libération et la sortie s'y lisent toutes deux."""
        order = commander(customer, restaurant, address, menu_item)
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)

        genres = list(
            StockMovement.objects.filter(stock_item=stock_oignon)
            .order_by("created_at")
            .values_list("kind", flat=True)
        )
        assert genres == [
            MovementKind.RECEIPT,
            MovementKind.RESERVATION,
            MovementKind.RELEASE,
            MovementKind.CONSUMPTION,
        ]


class TestLAnnulation:
    def test_avant_le_feu_la_matiere_est_rendue(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        order = commander(customer, restaurant, address, menu_item)
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="client")

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("1000")
        assert stock_oignon.reserved == g("0")

    def test_apres_le_feu_la_matiere_n_est_plus_rendue(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """On ne décuisine pas un oignon.

        Le recréditer inventerait de la matière que l'inventaire physique
        démentirait — la même asymétrie que le remboursement connaît déjà : on
        rend l'argent, jamais le travail.
        """
        order = commander(customer, restaurant, address, menu_item)
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="incident")

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("980")
        assert stock_oignon.reserved == g("0")

    def test_un_rejeu_d_annulation_ne_rend_pas_deux_fois(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
    ) -> None:
        """Le garde-fou est déjà en place — `is_noop` — et ce test l'atteste ici."""
        order = commander(customer, restaurant, address, menu_item)
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="client")
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="client")

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("1000")
        assert stock_oignon.reserved == g("0")


class TestLInstantane:
    def test_la_ligne_retient_l_identifiant_de_l_option(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        supplement_fromage: Option,
    ) -> None:
        """Sans lui, l'annulation ne saurait pas quelle recette d'option rendre."""
        order = commander(customer, restaurant, address, menu_item, options=[supplement_fromage])

        ligne = order.lines.get()
        assert ligne.options[0]["option_id"] == str(supplement_fromage.pk)

    def test_une_commande_anterieure_s_annule_sans_ses_options(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
        supplement_fromage: Option,
    ) -> None:
        """Les commandes ouvertes le jour du déploiement n'ont pas d'`option_id`.

        Elles rendent la matière de leur recette de base, pas celle de leurs
        suppléments. C'est inexact et volontaire : refuser l'annulation
        bloquerait l'exploitation, et deviner l'option par son libellé ferait
        dépendre un mouvement de stock d'une chaîne que le catalogue peut
        renommer.
        """
        order = commander(customer, restaurant, address, menu_item, options=[supplement_fromage])

        # On rejoue l'instantané d'avant la migration : les libellés, sans les
        # identifiants.
        ligne = order.lines.get()
        ligne.options = [
            {clef: valeur for clef, valeur in option.items() if clef != "option_id"}
            for option in ligne.options
        ]
        ligne.save(update_fields=["options"])

        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="client")

        stock_oignon.refresh_from_db()
        assert stock_oignon.reserved == g("0")


class TestLesPlatsSansRecette:
    def test_un_plat_sans_recette_n_engage_aucune_matiere(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
    ) -> None:
        """La carte existe, ses recettes non : la bascule se fait plat par plat."""
        order = commander(customer, restaurant, address, menu_item)

        stock_oignon.refresh_from_db()
        assert stock_oignon.reserved == g("0")
        assert order.status == OrderStatus.PENDING
        assert isinstance(order.lines.get(), OrderLine)


class TestLaBasculeEnCoursDeRoute:
    """Ce qui arrive aux commandes déjà en cours le jour du déploiement.

    Le refus appartient à la commande, jamais au feu : une commande payée doit
    pouvoir avancer même si son engagement de matière n'existe pas.
    """

    def test_preparer_sans_reservation_prealable_sort_quand_meme_la_matiere(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        oignon: Ingredient,
        recette_du_plat: Recipe,
    ) -> None:
        """La ligne de stock est ouverte **après** la commande.

        Rien n'a donc été promis. Libérer aveuglément la quantité consommée
        ferait échouer une libération sans contrepartie, et bloquerait la
        cuisine sur une commande déjà réglée.
        """
        order = commander(customer, restaurant, address, menu_item)
        assert not StockMovement.objects.exists()

        item = InventoryService.open_item(restaurant=restaurant, ingredient=oignon)
        InventoryService.receive(item=item, quantity=g("100"))

        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)

        item.refresh_from_db()
        assert item.on_hand == g("80")
        assert item.reserved == g("0")

    def test_une_reservation_partielle_se_libere_a_hauteur_du_promis(
        self,
        customer: object,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        stock_oignon: StockItem,
        recette_du_plat: Recipe,
        oignon: Ingredient,
    ) -> None:
        """La recette grossit entre la commande et le feu.

        Vingt grammes promis, trente sortis : on ne libère que ce qui l'était,
        sans quoi l'engagement passerait sous zéro.
        """
        order = commander(customer, restaurant, address, menu_item)
        stock_oignon.refresh_from_db()
        assert stock_oignon.reserved == g("20")

        ligne = RecipeIngredient.objects.get(recipe__menu_item=menu_item, ingredient=oignon)
        ligne.quantity = g("30")
        ligne.save(update_fields=["quantity_base", "quantity_dimension"])

        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)

        stock_oignon.refresh_from_db()
        assert stock_oignon.on_hand == g("970")
        assert stock_oignon.reserved == g("0")
