"""Inventaire — les propriétés dont la violation fausse un stock.

Ce que cette suite verrouille, dans l'ordre de gravité :

* **la réconciliation** — la somme du journal égale la colonne. Sans elle, on
  aurait deux vérités et aucun moyen de savoir laquelle a tort ;
* **la concurrence** — deux réservations simultanées n'emportent pas le même
  dernier kilogramme ;
* **l'ajout seul** — un mouvement écrit ne se modifie ni ne s'efface ;
* **le sens imposé** — une perte ne peut pas créer du stock ;
* **les dimensions** — 20 g et 30 ml ne s'additionnent pas.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connections
from django.db.models import Sum

from apps.inventory.models import (
    Ingredient,
    JournalIsAppendOnly,
    MovementKind,
    StockItem,
    StockMovement,
)
from apps.inventory.services import InventoryService, MotiveRequired, WrongDirection
from apps.restaurants.models import Restaurant
from common.exceptions import InsufficientStock
from common.money import Money
from common.quantities import Dimension, DimensionMismatch, Quantity

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def boeuf() -> Ingredient:
    return Ingredient.objects.create(
        name="Bœuf haché", slug="boeuf-hache", dimension=Dimension.MASS
    )


@pytest.fixture
def sauce() -> Ingredient:
    return Ingredient.objects.create(
        name="Sauce maison", slug="sauce-maison", dimension=Dimension.VOLUME
    )


@pytest.fixture
def stock(restaurant: Restaurant, boeuf: Ingredient) -> StockItem:
    """Une ligne de stock ouverte, mais vide — l'état d'une cuisine qui ouvre."""
    return InventoryService.open_item(restaurant=restaurant, ingredient=boeuf)


def g(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "g")


class TestOuvertureDeLigne:
    def test_la_ligne_part_a_zero_dans_la_dimension_de_l_ingredient(self, stock: StockItem) -> None:
        assert stock.on_hand == Quantity.zero(Dimension.MASS)
        assert stock.reserved == Quantity.zero(Dimension.MASS)

    def test_l_ouverture_est_idempotente(
        self, restaurant: Restaurant, boeuf: Ingredient, stock: StockItem
    ) -> None:
        """Un double clic du back-office ne doit pas remettre un stock réel à zéro."""
        InventoryService.receive(item=stock, quantity=g("500"))

        encore = InventoryService.open_item(restaurant=restaurant, ingredient=boeuf)

        assert encore.pk == stock.pk
        assert encore.on_hand == g("500")

    def test_une_cuisine_par_ingredient(self, restaurant: Restaurant, boeuf: Ingredient) -> None:
        from django.db import IntegrityError

        StockItem.objects.filter(restaurant=restaurant, ingredient=boeuf).delete()
        InventoryService.open_item(restaurant=restaurant, ingredient=boeuf)
        with pytest.raises(IntegrityError):
            StockItem.objects.create(
                restaurant=restaurant,
                ingredient=boeuf,
                on_hand=g("0"),
                reserved=g("0"),
            )

    def test_un_seuil_d_une_autre_dimension_est_refuse(
        self, restaurant: Restaurant, boeuf: Ingredient
    ) -> None:
        StockItem.objects.filter(restaurant=restaurant, ingredient=boeuf).delete()
        with pytest.raises(DimensionMismatch):
            InventoryService.open_item(
                restaurant=restaurant,
                ingredient=boeuf,
                low_stock_threshold=Quantity.from_unit("1", "l"),
            )


class TestEntreesEtSorties:
    def test_une_reception_augmente_le_stock(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        stock.refresh_from_db()
        assert stock.on_hand == Quantity.from_unit("1", "kg")

    def test_une_consommation_diminue_le_stock(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.consume(item=stock, quantity=g("120"))
        stock.refresh_from_db()
        assert stock.on_hand == g("880")

    def test_l_appelant_ne_porte_pas_le_signe(self, stock: StockItem) -> None:
        """`consume(120 g)` sort 120 g, même si l'appelant passe un positif.

        Faire porter le `-` aux points d'appel déplacerait la règle vers les
        dizaines d'endroits qui consomment, dont un finirait par l'oublier.
        """
        InventoryService.receive(item=stock, quantity=g("500"))
        mouvement = InventoryService.consume(item=stock, quantity=g("120"))
        assert mouvement.quantity == g("-120")

    def test_consommer_plus_que_le_stock_est_refuse(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("100"))
        with pytest.raises(InsufficientStock):
            InventoryService.consume(item=stock, quantity=g("101"))

    def test_un_refus_ne_laisse_aucune_trace(self, stock: StockItem) -> None:
        """Le journal ne doit pas porter un mouvement que la colonne n'a pas suivi."""
        InventoryService.receive(item=stock, quantity=g("100"))
        with pytest.raises(InsufficientStock):
            InventoryService.consume(item=stock, quantity=g("500"))

        stock.refresh_from_db()
        assert stock.on_hand == g("100")
        assert stock.movements.filter(kind=MovementKind.CONSUMPTION).count() == 0

    def test_le_stock_ne_descend_jamais_sous_zero(self, stock: StockItem) -> None:
        with pytest.raises(InsufficientStock):
            InventoryService.consume(item=stock, quantity=g("1"))
        stock.refresh_from_db()
        assert stock.on_hand == g("0")

    def test_un_mouvement_de_zero_est_refuse(self, stock: StockItem) -> None:
        with pytest.raises(WrongDirection, match="zéro"):
            InventoryService.receive(item=stock, quantity=g("0"))


class TestSensImpose:
    def test_une_perte_ne_peut_pas_creer_du_stock(self, stock: StockItem) -> None:
        """Le cas qui justifie que le signe vienne du type, pas de la saisie."""
        InventoryService.receive(item=stock, quantity=g("500"))
        InventoryService.waste(item=stock, quantity=g("100"), reason="Chute au sol")
        stock.refresh_from_db()
        assert stock.on_hand == g("400")

    def test_une_perte_exige_un_motif(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("500"))
        with pytest.raises(MotiveRequired):
            InventoryService.waste(item=stock, quantity=g("100"), reason="   ")

    def test_un_ajustement_exige_un_motif(self, stock: StockItem) -> None:
        with pytest.raises(MotiveRequired):
            InventoryService.adjust(item=stock, delta=g("10"), reason="")

    def test_un_ajustement_va_dans_les_deux_sens(self, stock: StockItem) -> None:
        """Le seul mouvement où l'appelant décide du sens : un comptage corrige
        à la hausse comme à la baisse, et le service ne peut pas le deviner."""
        InventoryService.adjust(item=stock, delta=g("300"), reason="Inventaire d'ouverture")
        stock.refresh_from_db()
        assert stock.on_hand == g("300")

        InventoryService.adjust(item=stock, delta=g("-50"), reason="Comptage physique")
        stock.refresh_from_db()
        assert stock.on_hand == g("250")

    def test_receive_refuse_un_type_qui_n_est_pas_une_entree(self, stock: StockItem) -> None:
        with pytest.raises(WrongDirection):
            InventoryService.receive(item=stock, quantity=g("100"), kind=MovementKind.CONSUMPTION)


class TestDimensions:
    def test_un_mouvement_d_une_autre_dimension_est_refuse(self, stock: StockItem) -> None:
        """Le défaut que la dimension portée par la valeur rend impossible."""
        with pytest.raises(DimensionMismatch):
            InventoryService.receive(item=stock, quantity=Quantity.from_unit("1", "l"))

    def test_les_unites_d_une_meme_dimension_se_melangent(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=Quantity.from_unit("1", "kg"))
        InventoryService.receive(item=stock, quantity=Quantity.from_unit("500", "g"))
        stock.refresh_from_db()
        assert stock.on_hand.as_unit("kg") == Decimal("1.5")


class TestReservation:
    def test_reserver_n_entame_pas_le_detenu(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.reserve(item=stock, quantity=g("300"))
        stock.refresh_from_db()
        assert stock.on_hand == g("1000")
        assert stock.reserved == g("300")
        assert stock.available == g("700")

    def test_on_ne_promet_pas_ce_qu_on_n_a_pas(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("100"))
        with pytest.raises(InsufficientStock):
            InventoryService.reserve(item=stock, quantity=g("101"))

    def test_deux_reservations_epuisent_le_disponible(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("100"))
        InventoryService.reserve(item=stock, quantity=g("60"))
        with pytest.raises(InsufficientStock):
            InventoryService.reserve(item=stock, quantity=g("50"))

    def test_liberer_rend_le_disponible(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.reserve(item=stock, quantity=g("300"))
        InventoryService.release(item=stock, quantity=g("300"))
        stock.refresh_from_db()
        assert stock.reserved == g("0")
        assert stock.available == g("1000")

    def test_consommer_depuis_une_reservation_ecrit_deux_lignes(self, stock: StockItem) -> None:
        """Un mouvement, un effet — c'est ce qui rend le journal réconciliable."""
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.reserve(item=stock, quantity=g("300"))

        InventoryService.consume(item=stock, quantity=g("300"), from_reservation=True)

        stock.refresh_from_db()
        assert stock.on_hand == g("700")
        assert stock.reserved == g("0")
        assert stock.movements.filter(kind=MovementKind.RELEASE).count() == 1
        assert stock.movements.filter(kind=MovementKind.CONSUMPTION).count() == 1


class TestSeuilDAlerte:
    def test_sans_seuil_rien_n_alerte(self, stock: StockItem) -> None:
        """Ne pas définir de seuil est un choix, pas un oubli."""
        assert stock.is_low is False

    def test_le_seuil_se_mesure_sur_le_disponible_pas_sur_le_detenu(
        self, restaurant: Restaurant, boeuf: Ingredient
    ) -> None:
        """Un kilo entièrement promis n'est pas un kilo disponible."""
        StockItem.objects.filter(restaurant=restaurant, ingredient=boeuf).delete()
        item = InventoryService.open_item(
            restaurant=restaurant, ingredient=boeuf, low_stock_threshold=g("200")
        )
        InventoryService.receive(item=item, quantity=g("1000"))
        item.refresh_from_db()
        assert item.is_low is False

        InventoryService.reserve(item=item, quantity=g("900"))
        item.refresh_from_db()
        assert item.is_low is True


class TestJournalEnAjoutSeul:
    def test_un_mouvement_ne_se_modifie_pas(self, stock: StockItem) -> None:
        mouvement = InventoryService.receive(item=stock, quantity=g("100"))
        mouvement.reason = "je corrige"
        with pytest.raises(JournalIsAppendOnly):
            mouvement.save()

    def test_un_mouvement_ne_s_efface_pas(self, stock: StockItem) -> None:
        mouvement = InventoryService.receive(item=stock, quantity=g("100"))
        with pytest.raises(JournalIsAppendOnly):
            mouvement.delete()

    def test_une_erreur_se_contre_passe(self, stock: StockItem) -> None:
        """La forme de correction que ce journal impose, et pourquoi elle est bonne :
        les deux lignes restent, et l'on sait qu'il y a eu correction."""
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.waste(item=stock, quantity=g("100"), reason="Erreur de saisie")
        InventoryService.adjust(
            item=stock, delta=g("100"), reason="Contre-passation de la perte du jour"
        )

        stock.refresh_from_db()
        assert stock.on_hand == g("1000")
        assert stock.movements.count() == 3


class TestReconciliation:
    """L'invariant qui rend la colonne `on_hand` défendable.

    Elle est maintenue par `F()` plutôt que recalculée à chaque lecture, ce qui
    crée deux vérités. Ce test est la raison pour laquelle ce choix est tenable :
    il dit laquelle a tort, le jour où elles divergent.
    """

    def test_la_somme_du_journal_egale_le_detenu(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.receive(item=stock, quantity=Quantity.from_unit("2", "kg"))
        InventoryService.consume(item=stock, quantity=g("450"))
        InventoryService.waste(item=stock, quantity=g("30"), reason="Brûlé")
        InventoryService.adjust(item=stock, delta=g("-20"), reason="Comptage")

        stock.refresh_from_db()
        somme = stock.movements.exclude(
            kind__in=[MovementKind.RESERVATION, MovementKind.RELEASE]
        ).aggregate(total=Sum("quantity_base"))["total"]

        assert somme == stock.on_hand.amount_base

    def test_la_somme_des_engagements_egale_le_reserve(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"))
        InventoryService.reserve(item=stock, quantity=g("300"))
        InventoryService.reserve(item=stock, quantity=g("200"))
        InventoryService.release(item=stock, quantity=g("100"))

        stock.refresh_from_db()
        somme = stock.movements.filter(
            kind__in=[MovementKind.RESERVATION, MovementKind.RELEASE]
        ).aggregate(total=Sum("quantity_base"))["total"]

        assert somme == stock.reserved.amount_base == g("400").amount_base


class TestCoutMatiere:
    """Ce que l'inventaire débloque, et qui n'existait pas : la marge.

    Sans coût unitaire, `analytics` rend un chiffre d'affaires réel et aucune
    marge — il n'y avait aucune donnée pour la calculer.
    """

    def test_la_premiere_entree_fixe_le_cout(self, stock: StockItem) -> None:
        InventoryService.receive(item=stock, quantity=g("1000"), unit_cost=Money(3, "XOF"))
        stock.refresh_from_db()
        assert stock.unit_cost == Money(3, "XOF")

    def test_le_cout_est_moyenne_pondere_et_non_le_dernier_prix(self, stock: StockItem) -> None:
        """Sur une matière dont le cours bouge, le dernier prix ferait sauter la
        marge affichée à chaque livraison, alors que la cuisine consomme encore
        l'ancien lot."""
        InventoryService.receive(item=stock, quantity=g("1000"), unit_cost=Money(10, "XOF"))
        InventoryService.receive(item=stock, quantity=g("1000"), unit_cost=Money(20, "XOF"))

        stock.refresh_from_db()
        assert stock.unit_cost == Money(15, "XOF")

    def test_le_mouvement_fige_le_cout_du_moment(self, stock: StockItem) -> None:
        """Une consommation de mars reste valorisée au prix de mars."""
        entree = InventoryService.receive(
            item=stock, quantity=g("1000"), unit_cost=Money(10, "XOF")
        )
        InventoryService.receive(item=stock, quantity=g("1000"), unit_cost=Money(20, "XOF"))

        entree.refresh_from_db()
        assert entree.unit_cost == Money(10, "XOF")

    def test_sans_entree_facturee_le_cout_reste_inconnu(self, stock: StockItem) -> None:
        """Inventer un coût à zéro ferait afficher une marge de 100 %."""
        InventoryService.receive(item=stock, quantity=g("1000"))
        stock.refresh_from_db()
        assert stock.unit_cost is None


class TestDimensionsDesDeuxTables:
    def test_les_deux_tables_de_dimensions_concordent(self) -> None:
        """`common.quantities.Dimension` et `Dimensions` (Django) ne peuvent pas
        diverger sans qu'un test le dise."""
        from apps.inventory.models import Dimensions

        assert {d.value for d in Dimensions} == set(Dimension.ALL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgis
class TestConcurrence:
    """`transaction=True` : sous le `django_db` ordinaire, les deux fils
    partageraient la transaction du test et ne se concurrenceraient pas."""

    def test_deux_reservations_concurrentes_n_emportent_pas_le_meme_stock(
        self, restaurant: Restaurant
    ) -> None:
        ingredient = Ingredient.objects.create(
            name="Pain", slug="pain-concurrence", dimension=Dimension.COUNT
        )
        item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
        InventoryService.receive(item=item, quantity=Quantity.from_unit("1", "unit"))

        def reserver() -> str:
            try:
                InventoryService.reserve(item=item, quantity=Quantity.from_unit("1", "unit"))
                return "ok"
            except InsufficientStock:
                return "refusé"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = sorted(f.result() for f in [pool.submit(reserver), pool.submit(reserver)])

        assert resultats == ["ok", "refusé"]

        item.refresh_from_db()
        assert item.reserved == Quantity.from_unit("1", "unit")
        assert item.available.is_zero
        assert (
            StockMovement.objects.filter(stock_item=item, kind=MovementKind.RESERVATION).count()
            == 1
        )

    def test_deux_consommations_concurrentes_n_emportent_pas_la_meme_matiere(
        self, restaurant: Restaurant
    ) -> None:
        ingredient = Ingredient.objects.create(
            name="Cheddar", slug="cheddar-concurrence", dimension=Dimension.MASS
        )
        item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
        InventoryService.receive(item=item, quantity=g("100"))

        def consommer() -> str:
            try:
                InventoryService.consume(item=item, quantity=g("100"))
                return "ok"
            except InsufficientStock:
                return "refusé"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = sorted(f.result() for f in [pool.submit(consommer), pool.submit(consommer)])

        assert resultats == ["ok", "refusé"]
        item.refresh_from_db()
        assert item.on_hand.is_zero
