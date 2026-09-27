"""Pertes et corrections — le plafond, le quatre-yeux, l'idempotence.

Ce que cette suite verrouille, dans l'ordre où cela coûterait :

* **ce qui passe seul** : une valeur connue, sous le plafond de la cuisine. Tout
  le reste attend une seconde personne — pas de plafond, coût inconnu, valeur
  au-delà ;
* **le quatre-yeux, tenu par la base** : celui qui a déclaré ne valide pas, ni
  par le service, ni par une écriture qui le contournerait ;
* **une demande ne touche à rien** tant qu'elle n'est pas validée ;
* **un rejeu ne compte pas deux fois** — réception comme perte, en série comme
  en concurrence ;
* **le coût se tient au kilogramme**, pas au gramme : en francs CFA, l'arrondi
  au gramme mangeait la marge.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from apps.accounts.models import User, UserType
from apps.inventory.models import (
    AdjustmentRequest,
    AdjustmentStatus,
    Ingredient,
    MovementKind,
    StockItem,
    StockMovement,
)
from apps.inventory.services import (
    DecisionAlreadyTaken,
    FourEyesRequired,
    IdempotencyKeyReused,
    InactiveIngredient,
    InventoryService,
    MotiveRequired,
)
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation, InsufficientStock
from common.money import Money
from common.quantities import Dimension, Quantity, value_minor

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def g(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "g")


def kg(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "kg")


def personnel(email: str) -> User:
    return User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )


@pytest.fixture
def commis() -> User:
    return personnel("commis@elcorazon.test")


@pytest.fixture
def gerant() -> User:
    return personnel("gerant@elcorazon.test")


@pytest.fixture
def boeuf(restaurant: Restaurant) -> StockItem:
    """Dix kilos de bœuf à 4 000 F le kilo — une ligne dont la valeur est connue."""
    ingredient = Ingredient.objects.create(name="Bœuf", slug="boeuf", dimension=Dimension.MASS)
    item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
    InventoryService.receive(item=item, quantity=kg("10"), unit_cost=Money(4_000, XOF))
    item.refresh_from_db()
    return item


def plafonner(restaurant: Restaurant, montant: int | None) -> None:
    restaurant.stock_adjustment_ceiling = Money(montant, XOF) if montant is not None else None
    restaurant.save(
        update_fields=["stock_adjustment_ceiling_minor", "stock_adjustment_ceiling_currency"]
    )


# ================================================================ le coût


class TestLeCoutSeTientAuKilogramme:
    def test_vingt_grammes_d_oignon_valent_dix_francs(self) -> None:
        """Au gramme, 500 F le kilo valaient 0,5 F — arrondi à 0 ou à 1, soit
        cent pour cent d'erreur sur la donnée qui doit rendre la marge
        calculable."""
        assert value_minor(g("20"), 500) == 10

    def test_la_valeur_suit_le_signe(self) -> None:
        assert value_minor(kg("-2.5"), 1_200) == -3_000

    def test_une_unite_se_valorise_a_l_unite(self) -> None:
        assert value_minor(Quantity.from_unit("30", "unit"), 75) == 2_250

    def test_une_livraison_facturee_dans_une_autre_devise_est_refusee(
        self, boeuf: StockItem
    ) -> None:
        with pytest.raises(BusinessRuleViolation):
            InventoryService.receive(item=boeuf, quantity=kg("1"), unit_cost=Money(8, "EUR"))


# ============================================================== le plafond


class TestCeQuiPasseSeul:
    def test_sous_le_plafond_la_perte_est_ecrite(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        """500 g à 4 000 F/kg valent 2 000 F, sous un plafond de 5 000 F."""
        plafonner(restaurant, 5_000)

        declaration = InventoryService.declare_waste(
            item=boeuf, quantity=g("500"), reason="Tombé au sol", actor=commis
        )

        assert declaration.movement is not None
        assert declaration.request is None
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("9.5")

    def test_au_plafond_exactement_la_perte_passe_encore(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        plafonner(restaurant, 2_000)

        declaration = InventoryService.declare_waste(
            item=boeuf, quantity=g("500"), reason="Tombé au sol", actor=commis
        )

        assert declaration.movement is not None

    def test_au_dela_du_plafond_la_perte_attend_et_ne_touche_a_rien(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        """Deux kilos valent 8 000 F : la demande attend, et le stock ne bouge pas.

        Une ligne « en attente » dans le journal obligerait chaque lecture à
        savoir l'écarter ; la première qui l'oublierait compterait une perte que
        personne n'a acceptée.
        """
        plafonner(restaurant, 5_000)

        declaration = InventoryService.declare_waste(
            item=boeuf, quantity=kg("2"), reason="Chaîne du froid rompue", actor=commis
        )

        assert declaration.movement is None
        assert declaration.is_pending
        assert declaration.request is not None
        assert declaration.request.estimated_value == Money(8_000, XOF)
        assert declaration.request.quantity == kg("-2")
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("10")
        assert not StockMovement.objects.filter(kind=MovementKind.WASTE).exists()

    def test_sans_plafond_fixe_rien_ne_passe_seul(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        """Nul veut dire « tout se valide ». Tant que personne n'a décidé ce qui
        peut passer seul, rien ne passe seul — un gramme compris."""
        plafonner(restaurant, None)

        declaration = InventoryService.declare_waste(
            item=boeuf, quantity=g("1"), reason="Parure", actor=commis
        )

        assert declaration.is_pending

    def test_un_cout_inconnu_ne_passe_jamais_seul(
        self, restaurant: Restaurant, commis: User
    ) -> None:
        """On ne peut pas prouver qu'une valeur inconnue est sous le plafond."""
        plafonner(restaurant, 1_000_000)
        ingredient = Ingredient.objects.create(name="Sel", slug="sel", dimension=Dimension.MASS)
        item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
        InventoryService.receive(item=item, quantity=kg("5"))

        declaration = InventoryService.declare_waste(
            item=item, quantity=g("10"), reason="Humidité", actor=commis
        )

        assert declaration.is_pending
        assert declaration.request is not None
        assert declaration.request.estimated_value is None

    def test_une_correction_a_la_hausse_obeit_au_meme_plafond(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        """Une hausse fictive est la façon ordinaire de masquer, le mois suivant,
        une sortie non déclarée : le plafond porte sur la valeur absolue."""
        plafonner(restaurant, 5_000)

        declaration = InventoryService.declare_adjustment(
            item=boeuf, delta=kg("3"), reason="Recomptage", actor=commis
        )

        assert declaration.is_pending

    def test_une_perte_plus_grande_que_le_stock_est_refusee_tout_de_suite(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        """La personne qui compte doit l'apprendre en comptant, pas au moment
        où quelqu'un d'autre voudra valider."""
        plafonner(restaurant, None)

        with pytest.raises(InsufficientStock):
            InventoryService.declare_waste(
                item=boeuf, quantity=kg("11"), reason="Inventaire", actor=commis
            )

        assert not AdjustmentRequest.objects.exists()

    def test_sans_motif_rien_n_est_declare(self, boeuf: StockItem, commis: User) -> None:
        with pytest.raises(MotiveRequired):
            InventoryService.declare_waste(item=boeuf, quantity=g("5"), reason="  ", actor=commis)


# ============================================================ le quatre-yeux


class TestLeQuatreYeux:
    @pytest.fixture
    def demande(self, restaurant: Restaurant, boeuf: StockItem, commis: User) -> AdjustmentRequest:
        plafonner(restaurant, None)
        declaration = InventoryService.declare_waste(
            item=boeuf, quantity=kg("2"), reason="Chaîne du froid rompue", actor=commis
        )
        assert declaration.request is not None
        return declaration.request

    def test_valider_ecrit_la_perte_et_la_relie_a_la_demande(
        self, demande: AdjustmentRequest, boeuf: StockItem, commis: User, gerant: User
    ) -> None:
        validee = InventoryService.approve(request=demande, actor=gerant, note="Vu le relevé")

        assert validee.status == AdjustmentStatus.APPROVED
        assert validee.decided_by == gerant
        assert validee.movement is not None
        assert validee.movement.kind == MovementKind.WASTE
        # Le journal dit qui a constaté ; la demande dit qui a accepté.
        assert validee.movement.actor == commis
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("8")

    def test_celui_qui_declare_ne_valide_pas(
        self, demande: AdjustmentRequest, boeuf: StockItem, commis: User
    ) -> None:
        with pytest.raises(FourEyesRequired):
            InventoryService.approve(request=demande, actor=commis)

        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("10")

    def test_celui_qui_declare_ne_refuse_pas_non_plus(
        self, demande: AdjustmentRequest, commis: User
    ) -> None:
        with pytest.raises(FourEyesRequired):
            InventoryService.reject(request=demande, actor=commis, note="Je me suis trompé")

    def test_la_base_refuse_une_decision_par_le_declarant(
        self, demande: AdjustmentRequest, commis: User
    ) -> None:
        """Un `shell` ou un nouveau point d'entrée contourneraient le service.
        La contrainte, non."""
        with pytest.raises(IntegrityError), transaction.atomic():
            AdjustmentRequest.objects.filter(pk=demande.pk).update(
                status=AdjustmentStatus.REJECTED,
                decided_by=commis,
                decided_at=timezone.now(),
            )

    def test_la_base_refuse_une_demande_validee_sans_mouvement(
        self, demande: AdjustmentRequest, gerant: User
    ) -> None:
        """Une validation qui n'a rien écrit serait une perte acceptée et jamais
        sortie du stock — l'état et ses traces vont ensemble."""
        with pytest.raises(IntegrityError), transaction.atomic():
            AdjustmentRequest.objects.filter(pk=demande.pk).update(
                status=AdjustmentStatus.APPROVED,
                decided_by=gerant,
                decided_at=timezone.now(),
            )

    def test_valider_deux_fois_n_ecrit_qu_une_perte(
        self, demande: AdjustmentRequest, boeuf: StockItem, gerant: User
    ) -> None:
        InventoryService.approve(request=demande, actor=gerant)
        InventoryService.approve(request=demande, actor=gerant)

        assert StockMovement.objects.filter(kind=MovementKind.WASTE).count() == 1
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("8")

    def test_refuser_ne_touche_a_rien_et_exige_un_motif(
        self, demande: AdjustmentRequest, boeuf: StockItem, gerant: User
    ) -> None:
        with pytest.raises(MotiveRequired):
            InventoryService.reject(request=demande, actor=gerant, note="")

        refusee = InventoryService.reject(request=demande, actor=gerant, note="Recompter")

        assert refusee.status == AdjustmentStatus.REJECTED
        assert refusee.movement is None
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("10")

    def test_une_demande_refusee_ne_se_valide_plus(
        self, demande: AdjustmentRequest, gerant: User
    ) -> None:
        InventoryService.reject(request=demande, actor=gerant, note="Recompter")

        with pytest.raises(DecisionAlreadyTaken):
            InventoryService.approve(request=demande, actor=gerant)

    def test_une_demande_validee_ne_se_refuse_plus(
        self, demande: AdjustmentRequest, gerant: User
    ) -> None:
        """La correction est au journal : l'annuler, c'est écrire l'inverse."""
        InventoryService.approve(request=demande, actor=gerant)

        with pytest.raises(DecisionAlreadyTaken):
            InventoryService.reject(request=demande, actor=gerant, note="Finalement non")

    def test_la_validation_echoue_si_le_stock_a_ete_consomme_entre_temps(
        self, demande: AdjustmentRequest, boeuf: StockItem, gerant: User
    ) -> None:
        """La demande reste en attente : c'est à la personne qui valide de la
        refuser en le disant, pas au système de la forcer."""
        InventoryService.consume(item=boeuf, quantity=kg("9"))

        with pytest.raises(InsufficientStock):
            InventoryService.approve(request=demande, actor=gerant)

        demande.refresh_from_db()
        assert demande.status == AdjustmentStatus.PENDING


# ============================================================ l'idempotence


class TestUnRejeuNeCompteQuUneFois:
    def test_une_reception_rejouee_ne_credite_qu_une_fois(self, boeuf: StockItem) -> None:
        premiere = InventoryService.receive(item=boeuf, quantity=kg("5"), request_key="bon-42")
        seconde = InventoryService.receive(item=boeuf, quantity=kg("5"), request_key="bon-42")

        assert premiere.pk == seconde.pk
        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("15")

    def test_la_meme_cle_pour_une_autre_reception_est_refusee(self, boeuf: StockItem) -> None:
        """Même clé, autre quantité : une erreur d'intégration, que l'accepter
        transformerait en livraison ignorée en silence."""
        InventoryService.receive(item=boeuf, quantity=kg("5"), request_key="bon-42")

        with pytest.raises(IdempotencyKeyReused):
            InventoryService.receive(item=boeuf, quantity=kg("6"), request_key="bon-42")

    def test_une_perte_rejouee_ne_sort_qu_une_fois(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        plafonner(restaurant, 10_000)
        for _ in range(2):
            InventoryService.declare_waste(
                item=boeuf, quantity=g("500"), reason="Tombé", actor=commis, request_key="perte-1"
            )

        boeuf.refresh_from_db()
        assert boeuf.on_hand == kg("9.5")

    def test_une_declaration_en_attente_rejouee_ne_cree_qu_une_demande(
        self, restaurant: Restaurant, boeuf: StockItem, commis: User
    ) -> None:
        plafonner(restaurant, None)
        premiere = InventoryService.declare_waste(
            item=boeuf, quantity=kg("2"), reason="Froid", actor=commis, request_key="perte-2"
        )
        seconde = InventoryService.declare_waste(
            item=boeuf, quantity=kg("2"), reason="Froid", actor=commis, request_key="perte-2"
        )

        assert premiere.request is not None and seconde.request is not None
        assert premiere.request.pk == seconde.request.pk
        assert AdjustmentRequest.objects.count() == 1

    def test_on_n_ouvre_ni_ne_recoit_une_reference_retiree(self, restaurant: Restaurant) -> None:
        ingredient = Ingredient.objects.create(
            name="Ancien piment", slug="ancien-piment", dimension=Dimension.MASS
        )
        item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
        Ingredient.objects.filter(pk=ingredient.pk).update(is_active=False)
        ingredient.refresh_from_db()

        with pytest.raises(InactiveIngredient):
            InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)
        with pytest.raises(InactiveIngredient):
            InventoryService.receive(item=item, quantity=kg("1"))


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgis
class TestConcurrenceDesRejeux:
    """`transaction=True` : sous le `django_db` ordinaire, les deux fils
    partageraient la transaction du test et ne se concurrenceraient pas."""

    def test_deux_receptions_simultanees_de_la_meme_livraison_ne_creditent_qu_une_fois(
        self, restaurant: Restaurant
    ) -> None:
        """Le réseau coupe, l'application renvoie ; les deux requêtes arrivent
        ensemble. La ligne est verrouillée avant de chercher la clé : la seconde
        trouve la première au lieu de créditer la chambre froide deux fois."""
        ingredient = Ingredient.objects.create(
            name="Riz", slug="riz-concurrence", dimension=Dimension.MASS
        )
        item = InventoryService.open_item(restaurant=restaurant, ingredient=ingredient)

        def recevoir() -> str:
            try:
                return str(
                    InventoryService.receive(
                        item=item, quantity=kg("25"), request_key="livraison-riz"
                    ).pk
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiants = {f.result() for f in [pool.submit(recevoir), pool.submit(recevoir)]}

        assert len(identifiants) == 1
        item.refresh_from_db()
        assert item.on_hand == kg("25")
        assert StockMovement.objects.filter(stock_item=item).count() == 1
