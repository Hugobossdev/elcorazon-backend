"""Le juge de disponibilité — incohérence I2 de l'audit.

Ce que cette suite verrouille, du plus grave au plus fin :

* **une cuisine fermée n'encaisse plus.** Avant le juge, la création de commande
  ne consultait ni les horaires ni la suspension : un test l'a prouvé, 201 et
  une commande en base sur un établissement sans aucune plage d'ouverture ;
* **un motif par refus**, stable, du plus général au plus précis ;
* **la carte voit la matière** — un plat dont le pain manque n'est plus proposé,
  sans que l'application cliente ait eu à être mise à jour ;
* **le panier cumule** — deux plats qui se disputent le même ingrédient ;
* **un seul lieu** compose la règle de la cuisine.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.availability.services import AvailabilityService, Demand
from apps.carts.services import CartService, price_cart
from apps.catalog.availability import _JUGE, menu_unavailabilities
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.groupcarts.services import GroupCartService
from apps.inventory.models import Ingredient
from apps.inventory.services import InventoryService
from apps.orders.models import Order, PaymentMethod
from apps.orders.services import OrderService
from apps.production.models import Recipe, RecipeIngredient
from apps.production.services import MaterialService, ProducedLine
from apps.profiles.models import Address
from apps.restaurants.availability import kitchen_unavailability
from apps.restaurants.models import OpeningHours, Restaurant, Weekday
from common.availability import KitchenNotOrderable, UnavailabilityCode
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.quantities import Dimension, Quantity
from tests.architecture.graph import APPS_ROOT, iter_app_modules

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def g(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "g")


def fermer(restaurant: Restaurant) -> None:
    """Retire toutes les plages : la cuisine est fermée à toute heure."""
    restaurant.opening_hours.all().delete()


def garnir(customer: Any, restaurant: Restaurant, menu_item: MenuItem, quantity: int = 1) -> None:
    CartService.add_line(
        cart=CartService.cart_for(customer, restaurant),
        menu_item=menu_item,
        quantity=quantity,
        options=[],
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


@pytest.fixture
def as_customer(customer: Any) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def pain() -> Ingredient:
    return Ingredient.objects.create(name="Pain", slug="pain", dimension=Dimension.MASS)


@pytest.fixture
def recette(menu_item: MenuItem, pain: Ingredient) -> Recipe:
    """80 g de pain par burger."""
    recette = Recipe.objects.create(menu_item=menu_item)
    RecipeIngredient.objects.create(recipe=recette, ingredient=pain, quantity=g("80"))
    return recette


def approvisionner(restaurant: Restaurant, ingredient: Ingredient, grammes: str) -> None:
    item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
    InventoryService.receive(item=item, quantity=g(grammes))


# ============================================================== la cuisine


class TestLaCuisineFermeeNEncaissePlus:
    """Le défaut de fond, prouvé rouge avant la correction."""

    def test_une_cuisine_fermee_refuse_la_commande(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        garnir(customer, restaurant, menu_item)
        fermer(restaurant)

        response = commander_par_l_api(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "kitchen_not_orderable"
        assert response.data["unavailable_code"] == UnavailabilityCode.KITCHEN_CLOSED
        assert not Order.objects.exists()

    def test_le_panier_n_est_pas_perdu_au_refus(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Refusé parce que fermé, le client revient à l'ouverture : son panier
        doit l'attendre. La transaction de commande emporte le vidage avec elle."""
        garnir(customer, restaurant, menu_item)
        fermer(restaurant)

        commander_par_l_api(as_customer, restaurant, address)

        assert CartService.cart_for(customer, restaurant).lines.count() == 1

    def test_une_cuisine_suspendue_refuse_la_commande(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Le coup de feu : ouverte, mais la prise de commande est suspendue."""
        garnir(customer, restaurant, menu_item)
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

        response = commander_par_l_api(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["unavailable_code"] == UnavailabilityCode.KITCHEN_PAUSED
        assert not Order.objects.exists()

    def test_un_marche_ferme_refuse_la_commande(
        self,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Le pays fermé depuis le back-office : l'établissement disparaît de la
        liste publique, et un panier resté ouvert ne doit pas passer outre."""
        garnir(customer, restaurant, menu_item)
        pays = restaurant.zone.city.country
        pays.is_active = False
        pays.save(update_fields=["is_active"])
        restaurant.refresh_from_db()

        with pytest.raises(KitchenNotOrderable) as refus:
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method=PaymentMethod.MOBILE_MONEY,
            )

        assert refus.value.unavailability.code == UnavailabilityCode.KITCHEN_UNPUBLISHED

    def test_le_panier_collaboratif_passe_par_le_meme_juge(
        self,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """Un déjeuner d'équipe se compose en une heure : la cuisine a pu fermer
        entre l'ouverture du panier et sa confirmation."""
        panier = GroupCartService.open(host=customer, restaurant=restaurant)
        GroupCartService.add_line(
            group_cart=panier, member=customer, menu_item=menu_item, quantity=2, options=[]
        )
        fermer(restaurant)

        with pytest.raises(KitchenNotOrderable):
            GroupCartService.confirm(
                group_cart=panier,
                actor=customer,
                address=address,
                payment_method=PaymentMethod.MOBILE_MONEY,
            )

        assert not Order.objects.exists()


class TestLesMotifsDeLaCuisine:
    def test_une_cuisine_ouverte_qui_prend_les_commandes_n_a_pas_de_motif(
        self, restaurant: Restaurant
    ) -> None:
        assert kitchen_unavailability(restaurant) is None

    def test_sans_aucune_plage_la_cuisine_est_fermee(self, restaurant: Restaurant) -> None:
        """Et non ouverte en permanence — ce que l'écran affiche déjà, et ce
        que la mise en service exige déjà."""
        fermer(restaurant)

        verdict = kitchen_unavailability(restaurant)

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.KITCHEN_CLOSED

    def test_les_horaires_se_lisent_dans_le_fuseau_du_pays(self, restaurant: Restaurant) -> None:
        """Le juge délègue à `is_open_at`, qui convertit l'instant : le même
        instant UTC ouvre ou ferme selon le pays."""
        fermer(restaurant)
        OpeningHours.objects.create(
            restaurant=restaurant,
            weekday=Weekday.TUESDAY,
            opens_at=dt.time(11),
            closes_at=dt.time(14),
        )
        lome = ZoneInfo(restaurant.timezone)

        midi = dt.datetime(2026, 9, 15, 12, 0, tzinfo=lome)  # un mardi
        soir = dt.datetime(2026, 9, 15, 20, 0, tzinfo=lome)

        assert kitchen_unavailability(restaurant, midi) is None
        verdict = kitchen_unavailability(restaurant, soir)
        assert verdict is not None
        assert verdict.code == UnavailabilityCode.KITCHEN_CLOSED

    def test_fermee_et_suspendue_se_dit_fermee(self, restaurant: Restaurant) -> None:
        """« Réessayez dans quelques minutes » serait faux jusqu'à l'ouverture."""
        fermer(restaurant)
        restaurant.accepts_orders = False

        verdict = kitchen_unavailability(restaurant)

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.KITCHEN_CLOSED

    def test_une_zone_fermee_rend_la_cuisine_indisponible(self, restaurant: Restaurant) -> None:
        """Même cascade que la liste publique des établissements."""
        restaurant.zone.is_active = False

        verdict = kitchen_unavailability(restaurant)

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.KITCHEN_UNPUBLISHED

    def test_la_fiche_et_la_commande_disent_la_meme_chose(
        self, as_customer: APIClient, restaurant: Restaurant
    ) -> None:
        """`can_order_now` était composé dans le sérialiseur, et seulement là.
        Il lit désormais le juge que la commande consulte."""
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

        fiche = as_customer.get(reverse("v1:restaurants:restaurant-detail", args=[restaurant.slug]))

        assert fiche.status_code == status.HTTP_200_OK
        assert fiche.data["can_order_now"] is False
        assert fiche.data["is_open"] is True
        assert fiche.data["unavailable_code"] == UnavailabilityCode.KITCHEN_PAUSED
        assert fiche.data["unavailable_reason"]

    def test_une_fiche_commandable_n_a_pas_de_motif(
        self, as_customer: APIClient, restaurant: Restaurant
    ) -> None:
        fiche = as_customer.get(reverse("v1:restaurants:restaurant-detail", args=[restaurant.slug]))

        assert fiche.data["can_order_now"] is True
        assert fiche.data["unavailable_code"] == ""
        assert fiche.data["unavailable_reason"] == ""


class TestLePanierLeDitAvantLePaiement:
    def test_le_panier_porte_le_motif_de_la_cuisine(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        menu_item: MenuItem,
    ) -> None:
        garnir(customer, restaurant, menu_item)
        fermer(restaurant)

        panier = as_customer.get(reverse("v1:carts:cart-detail", args=[restaurant.slug]))

        assert panier.data["is_orderable"] is False
        assert panier.data["unavailable_code"] == UnavailabilityCode.KITCHEN_CLOSED
        # Les lignes, elles, restent commandables : c'est la cuisine qui bloque,
        # et le client ne doit pas croire qu'il lui faut retirer un article.
        assert panier.data["lines"][0]["is_orderable"] is True

    def test_le_devis_dit_pourquoi_il_ne_peut_pas_commander(
        self,
        as_customer: APIClient,
        customer: Any,
        restaurant: Restaurant,
        menu_item: MenuItem,
    ) -> None:
        garnir(customer, restaurant, menu_item)
        Restaurant.objects.filter(pk=restaurant.pk).update(accepts_orders=False)

        devis = as_customer.post(
            reverse("v1:orders:order-preview"), {"restaurant": restaurant.slug}, format="json"
        )

        assert devis.data["is_orderable"] is False
        assert devis.data["unavailable_code"] == UnavailabilityCode.KITCHEN_PAUSED
        assert devis.data["unavailable_reason"]


# =============================================================== l'article


class TestLesMotifsDeLArticle:
    def test_une_categorie_eteinte_rend_ses_articles_incommandables(
        self, customer: Any, restaurant: Restaurant, menu_item: MenuItem, category: Category
    ) -> None:
        """Le plat du petit-déjeuner, désactivé à midi, restait commandable
        depuis un panier composé le matin : le panier ne regardait pas la
        catégorie."""
        garnir(customer, restaurant, menu_item)
        Category.objects.filter(pk=category.pk).update(is_active=False)

        ligne = price_cart(CartService.load(CartService.cart_for(customer, restaurant))).lines[0]

        assert ligne.unavailable_code == UnavailabilityCode.ITEM_UNAVAILABLE

    def test_une_categorie_eteinte_refuse_l_ajout(
        self, customer: Any, restaurant: Restaurant, menu_item: MenuItem, category: Category
    ) -> None:
        Category.objects.filter(pk=category.pk).update(is_active=False)
        menu_item.refresh_from_db()

        with pytest.raises(BusinessRuleViolation):
            garnir(customer, restaurant, menu_item)

    def test_un_plat_fini_epuise_se_dit_epuise(
        self, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=0)
        menu_item.refresh_from_db()

        verdict = menu_unavailabilities([menu_item])[menu_item.pk]

        assert verdict.code == UnavailabilityCode.OUT_OF_STOCK
        assert verdict.message == "Cet article est épuisé."

    def test_un_article_retire_se_dit_plus_au_menu(self, menu_item: MenuItem) -> None:
        menu_item.delete()

        verdict = menu_unavailabilities([menu_item])[menu_item.pk]

        assert verdict.code == UnavailabilityCode.ITEM_WITHDRAWN

    def test_il_n_en_reste_que_quelques_uns(
        self, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=2)
        menu_item.refresh_from_db()

        (verdict,) = AvailabilityService.demands([Demand(menu_item=menu_item, quantity=3)])

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.OUT_OF_STOCK
        assert verdict.details == {"remaining": 2}

    def test_une_option_eteinte_apres_coup_bloque_la_ligne(
        self, menu_item: MenuItem, option: Option
    ) -> None:
        """L'option retenue au panier s'est éteinte depuis — le cas que le refus
        à l'écriture ne peut pas empêcher."""
        Option.objects.filter(pk=option.pk).update(is_available=False)
        option.refresh_from_db()

        (verdict,) = AvailabilityService.demands([Demand(menu_item=menu_item, options=[option])])

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.OPTION_UNAVAILABLE
        assert option.name in verdict.message

    def test_sans_juge_inscrit_la_carte_repond_avec_ce_qu_elle_sait(
        self,
        restaurant: Restaurant,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Application retirée, `ready()` non appelé : la carte ne voit plus la
        matière, mais continue de voir l'article. Moins précise, jamais
        permissive sur ce qui compte — la commande, elle, passe par le juge
        complet et par la réservation sous verrou."""
        approvisionner(restaurant, pain, "10")
        monkeypatch.setattr("apps.catalog.availability._JUGE", [])

        assert menu_unavailabilities([menu_item]) == {}

        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)
        menu_item.refresh_from_db()
        assert menu_unavailabilities([menu_item])[menu_item.pk].code == (
            UnavailabilityCode.ITEM_UNAVAILABLE
        )

    def test_le_refus_de_commande_porte_le_motif_de_chaque_ligne(
        self,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        """« Épuisé » et « plus au menu » n'appellent pas le même geste."""
        garnir(customer, restaurant, menu_item)
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        with pytest.raises(BusinessRuleViolation) as refus:
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method=PaymentMethod.MOBILE_MONEY,
            )

        assert refus.value.extra["unavailable_codes"] == [UnavailabilityCode.ITEM_UNAVAILABLE]


# ============================================================== la matière


class TestLaCarteVoitLaMatiere:
    def test_un_plat_dont_le_pain_manque_n_est_plus_propose(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
    ) -> None:
        """Le champ `is_available` que lisent les applications **déjà installées**
        devient faux : elles cessent de proposer le plat sans mise à jour."""
        approvisionner(restaurant, pain, "50")

        carte = as_customer.get(
            reverse("v1:catalog:item-list"), {"restaurant__slug": restaurant.slug}
        )

        article = next(a for a in carte.data["results"] if a["id"] == str(menu_item.pk))
        assert article["is_available"] is False
        assert article["unavailable_code"] == UnavailabilityCode.INGREDIENT_SHORTAGE

    def test_la_fiche_detaillee_rend_le_meme_verdict(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
    ) -> None:
        approvisionner(restaurant, pain, "50")

        fiche = as_customer.get(reverse("v1:catalog:item-detail", args=[menu_item.pk]))

        assert fiche.data["is_available"] is False
        assert fiche.data["unavailable_code"] == UnavailabilityCode.INGREDIENT_SHORTAGE

    def test_la_carte_ne_dit_pas_combien_il_en_reste(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient, recette: Recipe
    ) -> None:
        """La carte est publique ; l'état d'un stock n'a pas à s'y lire."""
        approvisionner(restaurant, pain, "50")

        verdict = AvailabilityService.menu([menu_item])[menu_item.pk]

        assert verdict.details == {}
        assert "pain" not in verdict.message.lower()

    def test_la_matiere_promise_n_est_plus_disponible(
        self,
        customer: Any,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
    ) -> None:
        """120 g en stock, 80 réservés par une commande en attente : il en reste
        40, pas assez pour un burger de plus. Juger sur `on_hand` annoncerait
        un plat que la réservation refuserait aussitôt."""
        approvisionner(restaurant, pain, "120")
        garnir(customer, restaurant, menu_item)
        OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method=PaymentMethod.MOBILE_MONEY,
        )

        assert menu_item.pk in AvailabilityService.menu([menu_item])

    def test_un_ingredient_que_la_cuisine_ne_suit_pas_ne_bloque_rien(
        self, restaurant: Restaurant, menu_item: MenuItem, recette: Recipe
    ) -> None:
        """Même règle que la réservation : on ne juge pas ce qu'on ne compte pas.
        Une lecture plus stricte que l'écriture annoncerait des ruptures que la
        commande n'aurait pas refusées."""
        assert AvailabilityService.menu([menu_item]) == {}

    def test_un_plat_sans_recette_ne_manque_de_rien(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient
    ) -> None:
        approvisionner(restaurant, pain, "0.001")

        assert AvailabilityService.menu([menu_item]) == {}

    def test_deux_plats_de_la_carte_sont_juges_chacun_seul(
        self,
        restaurant: Restaurant,
        category: Category,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
    ) -> None:
        """Personne ne commande la carte entière : cumuler vingt plats
        annoncerait des ruptures qu'aucun client ne rencontrerait."""
        sandwich = _plat(restaurant, category, "sandwich")
        _recette(sandwich, pain, "80")
        approvisionner(restaurant, pain, "100")

        assert AvailabilityService.menu([menu_item, sandwich]) == {}

    def test_la_page_de_carte_coute_le_meme_nombre_de_requetes_quelle_que_soit_sa_taille(
        self,
        restaurant: Restaurant,
        category: Category,
        pain: Ingredient,
    ) -> None:
        """Deux requêtes pour la matière, que la page compte deux plats ou huit.
        Posée article par article, la question coûterait deux requêtes par plat."""
        approvisionner(restaurant, pain, "1000")
        plats = [_plat(restaurant, category, f"plat-{rang}") for rang in range(8)]
        for plat in plats:
            _recette(plat, pain, "10")
        charges = list(
            MenuItem.objects.filter(pk__in=[p.pk for p in plats]).select_related("category")
        )

        with CaptureQueriesContext(connection) as deux:
            AvailabilityService.menu(charges[:2])
        with CaptureQueriesContext(connection) as huit:
            AvailabilityService.menu(charges)

        assert len(huit.captured_queries) == len(deux.captured_queries) == 2


class TestLePanierCumuleLaMatiere:
    def test_il_dit_combien_on_peut_en_preparer(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient, recette: Recipe
    ) -> None:
        """Trois burgers demandés, du pain pour deux : le geste attendu est
        « passer à deux », pas « tout retirer »."""
        approvisionner(restaurant, pain, "200")

        (verdict,) = AvailabilityService.demands([Demand(menu_item=menu_item, quantity=3)])

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.INGREDIENT_SHORTAGE
        assert verdict.details == {"portions_possible": 2}
        assert "2" in verdict.message

    def test_deux_plats_qui_se_disputent_le_meme_pain(
        self,
        restaurant: Restaurant,
        category: Category,
        menu_item: MenuItem,
        pain: Ingredient,
        recette: Recipe,
    ) -> None:
        """Chaque ligne tiendrait seule, le panier non. C'est le seul manque que
        la carte ne peut pas voir."""
        sandwich = _plat(restaurant, category, "sandwich")
        _recette(sandwich, pain, "80")
        approvisionner(restaurant, pain, "100")

        verdicts = AvailabilityService.demands(
            [Demand(menu_item=menu_item), Demand(menu_item=sandwich)]
        )

        assert all(
            v is not None and v.code == UnavailabilityCode.INGREDIENT_SHORTAGE for v in verdicts
        )
        assert all(v is not None and "panier" in v.message for v in verdicts)

    def test_un_retrait_d_ingredient_rend_la_ligne_commandable(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient, recette: Recipe
    ) -> None:
        """« Sans pain » retire la matière qui manque : le juge compose la
        recette de l'option, par la même nomenclature que la réservation."""
        groupe = OptionGroup.objects.create(menu_item=menu_item, name="Retirer", max_select=1)
        sans_pain = Option.objects.create(group=groupe, name="Sans pain", price_delta=Money(0, XOF))
        recette_option = Recipe.objects.create(option=sans_pain)
        RecipeIngredient.objects.create(recipe=recette_option, ingredient=pain, quantity=g("-80"))
        approvisionner(restaurant, pain, "10")

        (avec,) = AvailabilityService.demands([Demand(menu_item=menu_item)])
        (sans,) = AvailabilityService.demands([Demand(menu_item=menu_item, options=[sans_pain])])

        assert avec is not None
        assert sans is None

    def test_un_article_retire_n_est_pas_pese(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient, recette: Recipe
    ) -> None:
        """Le catalogue d'abord : « plus au menu » est le motif utile, pas
        « rupture » pour un plat qu'on ne sert de toute façon plus."""
        approvisionner(restaurant, pain, "10")
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)
        menu_item.refresh_from_db()

        (verdict,) = AvailabilityService.demands([Demand(menu_item=menu_item)])

        assert verdict is not None
        assert verdict.code == UnavailabilityCode.ITEM_UNAVAILABLE

    def test_les_manques_se_calculent_sans_verrou_et_en_deux_requetes(
        self, restaurant: Restaurant, menu_item: MenuItem, pain: Ingredient, recette: Recipe
    ) -> None:
        approvisionner(restaurant, pain, "1000")

        with CaptureQueriesContext(connection) as requetes:
            MaterialService.shortages(
                restaurant_id=restaurant.pk,
                lines=[ProducedLine(menu_item_id=menu_item.pk, quantity=2)] * 5,
            )

        assert len(requetes.captured_queries) == 2
        assert not any("FOR UPDATE" in q["sql"] for q in requetes.captured_queries)


# ============================================================ un seul lieu


class TestUnSeulJuge:
    def test_la_carte_publique_interroge_le_juge_complet(self) -> None:
        """Inscrit au `ready()` : sans lui, la carte ne verrait pas la matière."""
        assert _JUGE == [AvailabilityService.menu]

    def test_aucun_autre_lieu_ne_lit_la_suspension_des_commandes(self) -> None:
        """`accepts_orders` n'est lu que par le juge de la cuisine.

        C'est la forme exécutable du critère de fin de l'incohérence I2 :
        la règle était composée dans un sérialiseur d'affichage que la commande
        ne consultait pas. Un second lecteur rouvrirait exactement ce défaut.
        """
        lecteurs = _lecteurs_de_l_attribut("accepts_orders")

        assert lecteurs == {"restaurants/availability.py"}

    def test_l_ouverture_n_est_lue_que_par_le_juge_et_l_affichage_des_horaires(self) -> None:
        """`is_open_at` sur un établissement : le juge, et le champ `is_open` qui
        dit les horaires **seuls** — une information distincte, que le juge
        ne remplace pas (« fermé, ouvre à 11 h » n'est pas « débordé »).

        `promotions` a sa propre `is_open_at`, sur `Promotion` : même nom,
        autre objet."""
        lecteurs = _lecteurs_de_l_attribut("is_open_at")

        assert lecteurs == {
            "restaurants/availability.py",
            "restaurants/serializers.py",
            "promotions/services.py",
        }


# ================================================================ outillage


def _plat(restaurant: Restaurant, category: Category, slug: str) -> MenuItem:
    return MenuItem.objects.create(
        restaurant=restaurant,
        category=category,
        name=slug.capitalize(),
        slug=slug,
        price=Money(2_000, XOF),
    )


def _recette(plat: MenuItem, ingredient: Ingredient, grammes: str) -> Recipe:
    recette = Recipe.objects.create(menu_item=plat)
    RecipeIngredient.objects.create(recipe=recette, ingredient=ingredient, quantity=g(grammes))
    return recette


def _lecteurs_de_l_attribut(nom: str) -> set[str]:
    """Modules des applications qui **lisent** cet attribut, migrations exclues.

    Une affectation (`accepts_orders = models.BooleanField(...)`) n'est pas une
    lecture, ni une chaîne dans une liste de champs : seul un accès `obj.nom` en
    lecture compte.
    """
    lecteurs: set[str] = set()
    for module in iter_app_modules():
        arbre = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for noeud in ast.walk(arbre):
            if (
                isinstance(noeud, ast.Attribute)
                and noeud.attr == nom
                and isinstance(noeud.ctx, ast.Load)
            ):
                lecteurs.add(module.relative_to(APPS_ROOT).as_posix())
    return lecteurs
