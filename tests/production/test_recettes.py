"""Recettes — ce qu'une commande sort réellement de la chambre froide.

Ce que cette suite verrouille, dans l'ordre de gravité :

* **le plancher par ligne** — « sans oignon » sur un plat ne peut pas effacer
  l'oignon d'un autre plat du même panier. C'est le seul défaut de ce module
  qui produirait un stock faux sans qu'aucune ligne ne soit fausse ;
* **la matière qu'on ne crée pas** — une option de retrait ne rend rien au
  stock ;
* **la cible exclusive** — une recette décrit un plat ou une option, jamais les
  deux, jamais aucun ;
* **les dimensions** — 200 ml de farine ne s'écrit pas ;
* **le coût d'accès** — un panier de quinze articles ne fait pas trente
  requêtes.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.inventory.models import Ingredient
from apps.production.models import Recipe, RecipeIngredient
from apps.production.services import ProducedLine, ProductionService
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.quantities import Dimension, DimensionMismatch, Quantity

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
def huile() -> Ingredient:
    return Ingredient.objects.create(name="Huile", slug="huile", dimension=Dimension.VOLUME)


@pytest.fixture
def burger(menu_item: MenuItem, oignon: Ingredient) -> MenuItem:
    """Un plat dont la recette est saisie : 20 g d'oignon."""
    recette = Recipe.objects.create(menu_item=menu_item)
    RecipeIngredient.objects.create(recipe=recette, ingredient=oignon, quantity=g("20"))
    return menu_item


@pytest.fixture
def sans_oignon(option_group: OptionGroup, oignon: Ingredient) -> Option:
    """« Sans oignon » — retire les 20 g de la recette de base."""
    option = Option.objects.create(
        group=option_group, name="Sans oignon", price_delta=Money(0, "XOF")
    )
    recette = Recipe.objects.create(option=option)
    RecipeIngredient.objects.create(recipe=recette, ingredient=oignon, quantity=g("-20"))
    return option


@pytest.fixture
def double_fromage(option_group: OptionGroup, fromage: Ingredient) -> Option:
    """Un supplément, qui ajoute 30 g de matière."""
    option = Option.objects.create(
        group=option_group, name="Double fromage", price_delta=Money(500, "XOF")
    )
    recette = Recipe.objects.create(option=option)
    RecipeIngredient.objects.create(recipe=recette, ingredient=fromage, quantity=g("30"))
    return option


class TestLeBesoinDUnPlat:
    def test_une_portion_sort_la_quantite_de_la_recette(
        self, burger: MenuItem, oignon: Ingredient
    ) -> None:
        besoin = ProductionService.requirements([ProducedLine(burger.id, 1)])

        assert besoin == {oignon.id: g("20")}

    def test_trois_portions_multiplient_la_recette(
        self, burger: MenuItem, oignon: Ingredient
    ) -> None:
        besoin = ProductionService.requirements([ProducedLine(burger.id, 3)])

        assert besoin == {oignon.id: g("60")}

    def test_un_plat_sans_recette_ne_consomme_rien(self, menu_item: MenuItem) -> None:
        """La bascule se fait plat par plat : sans recette, le plat passe comme avant.

        C'est ce qui permet de déployer la production sans avoir saisi la carte
        entière le jour même. `missing_recipes` rend l'écart mesurable.
        """
        assert ProductionService.requirements([ProducedLine(menu_item.id, 2)]) == {}

    def test_deux_lignes_du_meme_plat_s_additionnent(
        self, burger: MenuItem, oignon: Ingredient
    ) -> None:
        """Un burger saignant et un burger à point sont deux lignes, un ingrédient."""
        besoin = ProductionService.requirements(
            [ProducedLine(burger.id, 2), ProducedLine(burger.id, 1)]
        )

        assert besoin == {oignon.id: g("60")}

    def test_sans_ligne_le_besoin_est_vide(self) -> None:
        assert ProductionService.requirements([]) == {}


class TestLesOptions:
    def test_un_supplement_ajoute_sa_matiere(
        self,
        burger: MenuItem,
        double_fromage: Option,
        oignon: Ingredient,
        fromage: Ingredient,
    ) -> None:
        besoin = ProductionService.requirements([ProducedLine(burger.id, 1, (double_fromage.id,))])

        assert besoin == {oignon.id: g("20"), fromage.id: g("30")}

    def test_un_retrait_retranche_de_la_recette_de_base(
        self, burger: MenuItem, sans_oignon: Option, oignon: Ingredient
    ) -> None:
        """20 g moins 20 g : l'ingrédient disparaît du besoin, sans devenir négatif."""
        besoin = ProductionService.requirements([ProducedLine(burger.id, 1, (sans_oignon.id,))])

        assert besoin == {}

    def test_un_retrait_plus_grand_que_la_recette_ne_cree_pas_de_matiere(
        self, menu_item: MenuItem, option_group: OptionGroup, oignon: Ingredient
    ) -> None:
        """Le cas qui compte : décommander ne remet rien en chambre froide.

        Sans plancher, cette ligne rendrait −30 g, et l'appelant crédulement
        *crédité* trente grammes d'oignon au stock — une matière née d'une
        commande.
        """
        recette_plat = Recipe.objects.create(menu_item=menu_item)
        RecipeIngredient.objects.create(recipe=recette_plat, ingredient=oignon, quantity=g("20"))

        option = Option.objects.create(
            group=option_group, name="Vraiment sans oignon", price_delta=Money(0, "XOF")
        )
        recette_option = Recipe.objects.create(option=option)
        RecipeIngredient.objects.create(recipe=recette_option, ingredient=oignon, quantity=g("-50"))

        besoin = ProductionService.requirements([ProducedLine(menu_item.id, 1, (option.id,))])

        assert besoin == {}

    def test_le_plancher_est_par_ligne_et_non_sur_le_total(
        self, burger: MenuItem, sans_oignon: Option, oignon: Ingredient
    ) -> None:
        """Le défaut que ce module doit rendre impossible.

        Deux clients, un panier : l'un sans oignon, l'autre avec. Si le plancher
        était posé sur le total, le −20 g du premier annulerait le +20 g du
        second, et la cuisine sortirait zéro oignon pour un burger qui en
        demande. Le stock resterait alors juste sur le papier et faux dans la
        chambre froide — la dérive exacte que ce module existe pour empêcher.
        """
        besoin = ProductionService.requirements(
            [
                ProducedLine(burger.id, 1, (sans_oignon.id,)),
                ProducedLine(burger.id, 1),
            ]
        )

        assert besoin == {oignon.id: g("20")}

    def test_le_retrait_s_applique_a_chaque_portion_de_la_ligne(
        self, burger: MenuItem, sans_oignon: Option, oignon: Ingredient
    ) -> None:
        """Trois burgers sans oignon, c'est trois fois rien — pas une fois rien."""
        besoin = ProductionService.requirements([ProducedLine(burger.id, 3, (sans_oignon.id,))])

        assert besoin == {}


class TestLaCibleExclusive:
    def test_une_recette_sans_cible_est_refusee_par_la_base(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Recipe.objects.create()

    def test_une_recette_a_deux_cibles_est_refusee_par_la_base(
        self, menu_item: MenuItem, option: Option
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Recipe.objects.create(menu_item=menu_item, option=option)

    def test_un_plat_n_a_qu_une_recette(self, burger: MenuItem) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Recipe.objects.create(menu_item=burger)


class TestLesLignesDeRecette:
    def test_un_ingredient_ne_figure_qu_une_fois(
        self, burger: MenuItem, oignon: Ingredient
    ) -> None:
        """Deux lignes se liraient comme un remplacement alors qu'elles s'ajoutent."""
        recette = Recipe.objects.get(menu_item=burger)

        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeIngredient.objects.create(recipe=recette, ingredient=oignon, quantity=g("5"))

    def test_une_quantite_nulle_est_refusee_par_la_base(
        self, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        recette = Recipe.objects.create(menu_item=menu_item)

        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeIngredient.objects.create(recipe=recette, ingredient=oignon, quantity=g("0"))


class TestLEcritureParLeService:
    def test_la_dimension_doit_etre_celle_de_l_ingredient(
        self, menu_item: MenuItem, huile: Ingredient
    ) -> None:
        """« 200 g » d'une matière tenue en volume : la faute qu'aucun CHECK ne voit.

        La dimension de l'ingrédient vit dans une autre table, hors de portée
        d'une contrainte de table. C'est donc le service qui la tient.
        """
        recette = Recipe.objects.create(menu_item=menu_item)

        with pytest.raises(DimensionMismatch):
            ProductionService.set_ingredient(
                recipe=recette, ingredient_id=huile.id, quantity=g("200")
            )

        assert not RecipeIngredient.objects.filter(recipe=recette).exists()

    def test_la_quantite_nulle_est_refusee_avec_un_motif_lisible(
        self, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        recette = Recipe.objects.create(menu_item=menu_item)

        with pytest.raises(BusinessRuleViolation) as leve:
            ProductionService.set_ingredient(
                recipe=recette, ingredient_id=oignon.id, quantity=g("0")
            )

        assert "Oignon" in str(leve.value)

    def test_reposer_le_meme_ingredient_remplace_la_quantite(
        self, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        """Corriger une recette ne crée pas une seconde ligne — l'unicité l'interdit."""
        recette = Recipe.objects.create(menu_item=menu_item)

        ProductionService.set_ingredient(recipe=recette, ingredient_id=oignon.id, quantity=g("20"))
        ProductionService.set_ingredient(recipe=recette, ingredient_id=oignon.id, quantity=g("35"))

        lignes = RecipeIngredient.objects.filter(recipe=recette)
        assert lignes.count() == 1
        assert lignes.get().quantity == g("35")


class TestLesRecettesManquantes:
    def test_un_plat_sans_recette_est_signale(
        self, burger: MenuItem, restaurant: Restaurant, category: Category
    ) -> None:
        """« Rien à sortir » et « je ne sais pas quoi sortir » ne se confondent pas."""
        orphelin = MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Jus pressé",
            slug="jus-presse",
            price=Money(1_000, "XOF"),
        )

        manquantes = ProductionService.missing_recipes([burger.id, orphelin.id])

        assert manquantes == {orphelin.id}

    def test_sans_plat_demande_la_reponse_est_vide(self) -> None:
        assert ProductionService.missing_recipes([]) == set()


class TestLeCoutDAcces:
    def test_le_besoin_se_calcule_en_une_requete(
        self,
        burger: MenuItem,
        double_fromage: Option,
        sans_oignon: Option,
        django_assert_num_queries: pytest.FixtureRequest,
    ) -> None:
        """Quinze articles ne font pas trente requêtes.

        Ce calcul arrive pendant la confirmation de commande, c'est-à-dire dans
        une transaction qui tient déjà des verrous. Y charger les recettes une à
        une allongerait la fenêtre pendant laquelle les autres commandes
        attendent — au moment précis du coup de feu.
        """
        lignes = [
            ProducedLine(burger.id, 2, (double_fromage.id,)),
            ProducedLine(burger.id, 1, (sans_oignon.id,)),
            ProducedLine(burger.id, 3),
        ]

        with django_assert_num_queries(1):  # type: ignore[operator]
            ProductionService.requirements(lignes)


class TestCeQueLAdministrationLit:
    """Les libellés du back-office : lus par un humain, sur un écran de gestion."""

    def test_une_recette_de_plat_se_nomme_par_son_plat(self, burger: MenuItem) -> None:
        assert str(Recipe.objects.get(menu_item=burger)) == f"Recette — {burger.name}"

    def test_une_recette_d_option_se_nomme_par_son_option(self, sans_oignon: Option) -> None:
        assert str(Recipe.objects.get(option=sans_oignon)) == "Recette — Sans oignon"

    def test_une_ligne_dit_son_ingredient_et_sa_quantite(self, burger: MenuItem) -> None:
        ligne = RecipeIngredient.objects.get(recipe__menu_item=burger)

        assert str(ligne) == "Oignon — 20 g"
