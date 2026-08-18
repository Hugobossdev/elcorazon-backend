"""Fidélité — invariants F1, F2, F3, F5.

La suite est organisée autour de **la course qui a été prouvée sur
l'implémentation précédente** : deux échanges concurrents lisaient le même
solde, le trouvaient suffisant, et retiraient chacun leur dû. Solde négatif,
deux récompenses pour le prix d'une.

`TestF1Concurrence` la rejoue pour de vrai — deux connexions, deux transactions,
un seul solde — parce qu'un test qui appellerait deux fois le service en séquence
passerait aussi sur le code fautif et ne prouverait rien.

Les autres classes couvrent ce qui tient autour : le type de la colonne (F3), le
coût positif (F2), et le journal dont le solde est dérivable (F5).
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from django.db import connections
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.loyalty.models import (
    EntryKind,
    PointsAccount,
    PointsEntry,
    Reward,
    RewardKind,
    RewardRedemption,
)
from apps.loyalty.services import LoyaltyService, points_for
from apps.loyalty.tasks import expire_points
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.promotions.models import DiscountKind, Promotion
from apps.promotions.services import PromotionRefused, PromotionService
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation, InsufficientBalance
from common.money import Money
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def reward(**overrides: Any) -> Reward:
    defaults: dict[str, Any] = {
        "name": "500 F de remise",
        "kind": RewardKind.DISCOUNT,
        "points_cost": 100,
        "discount_minor": 500,
        "discount_currency": XOF,
    }
    return Reward.objects.create(**{**defaults, **overrides})


def crediter(user: User, points: int) -> PointsAccount:
    """Amorce un solde sans passer par une commande.

    Écrit directement plutôt que d'appeler `earn` : les tests de débit n'ont pas
    à dépendre du barème de gain, qui est réglable.
    """
    account = LoyaltyService.account_for(user)
    PointsAccount.objects.filter(pk=account.pk).update(
        balance=points, lifetime_earned=points, last_activity_at=timezone.now()
    )
    account.refresh_from_db()
    return account


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


class TestBaremeDeGain:
    def test_un_diviseur_entier_ne_perd_rien(self) -> None:
        """4 000 F à 1 point pour 100 F font 40 points, exactement. Un taux
        flottant donnerait 39,999… et une troncature dépendante de la
        machine."""
        assert points_for(Money(4_000, XOF)) == 40

    def test_la_division_ne_credite_jamais_au_dela_de_l_acquis(self) -> None:
        assert points_for(Money(4_099, XOF)) == 40

    def test_un_montant_sous_le_seuil_ne_rapporte_rien(self) -> None:
        assert points_for(Money(99, XOF)) == 0

    def test_le_diviseur_est_reglable(self, settings: Any) -> None:
        """Politique commerciale : elle se négocie et change sans
        redéploiement."""
        settings.LOYALTY_MINOR_UNITS_PER_POINT = 50

        assert points_for(Money(4_000, XOF)) == 80


@pytest.mark.django_db(transaction=True)
class TestF1Concurrence:
    """**La course prouvée.** Le cœur de ce module.

    Deux transactions réellement concurrentes tentent de dépenser le même
    solde. L'ancienne implémentation lisait, comparait, puis retirait : les deux
    passaient. Le `UPDATE ... WHERE balance >= coût` ne laisse aucun instant
    entre la condition et l'écriture, donc une seule passe.

    `transaction=True` n'est pas un détail : sous le `django_db` ordinaire, les
    données du test ne sont jamais validées et le second fil — qui a sa propre
    connexion — ne les voit pas. Le test se bloquerait au lieu de prouver quoi
    que ce soit. C'est la raison pour laquelle cette classe est séparée : elle
    tronque les tables entre chaque test, donc coûte plus cher, et une seule
    vérification en a réellement besoin.
    """

    def test_deux_echanges_concurrents_n_en_reussissent_qu_un(self, customer: User) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 100)  # de quoi payer **un** échange, pas deux

        def echanger() -> str:
            """Un échange, dans sa propre connexion."""
            try:
                LoyaltyService.redeem(user=customer, reward=prix)
                return "ok"
            except InsufficientBalance:
                return "refusé"
            finally:
                # Chaque fil ouvre sa connexion ; la laisser ouverte retiendrait
                # le démontage de la base de test.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = sorted(f.result() for f in [pool.submit(echanger), pool.submit(echanger)])

        assert resultats == ["ok", "refusé"]

        account = PointsAccount.objects.get(user=customer)
        assert account.balance == 0
        assert RewardRedemption.objects.count() == 1
        assert Promotion.objects.filter(owner=customer).count() == 1


class TestF1DebitConditionnel:
    """Le même invariant, vérifié sans concurrence : refus, atomicité, message."""

    def test_le_solde_ne_devient_jamais_negatif(self, customer: User) -> None:
        """F3 — le `PositiveIntegerField` pose un `CHECK >= 0` en base. Ce n'est
        pas une garde applicative qu'un service futur pourrait contourner."""
        prix = reward(points_cost=100)
        crediter(customer, 50)

        with pytest.raises(InsufficientBalance):
            LoyaltyService.redeem(user=customer, reward=prix)

        assert PointsAccount.objects.get(user=customer).balance == 50

    def test_un_refus_dit_ce_qui_manque(self, customer: User) -> None:
        """« Solde insuffisant » sans les nombres oblige le client à
        deviner."""
        prix = reward(points_cost=100)
        crediter(customer, 40)

        with pytest.raises(InsufficientBalance) as leve:
            LoyaltyService.redeem(user=customer, reward=prix)

        assert leve.value.extra == {"required": 100, "available": 40}

    def test_un_echange_rate_ne_laisse_ni_code_ni_mouvement(self, customer: User) -> None:
        """Soit le client a payé **et** reçu son code, soit rien ne s'est
        passé."""
        prix = reward(points_cost=100)
        crediter(customer, 40)

        with pytest.raises(InsufficientBalance):
            LoyaltyService.redeem(user=customer, reward=prix)

        assert not PointsEntry.objects.filter(kind=EntryKind.SPENT).exists()
        assert RewardRedemption.objects.count() == 0
        assert Promotion.objects.count() == 0


class TestF2CoutPositif:
    def test_la_base_refuse_un_cout_nul(self) -> None:
        """Un coût nul rendrait la récompense gratuite et infinie."""
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            reward(points_cost=0)

    def test_la_base_refuse_une_remise_absente_sur_une_remise(self) -> None:
        """Une « remise » qui ne remise rien coûterait des points pour un code
        sans effet."""
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            reward(discount_minor=0)

    def test_une_livraison_offerte_n_a_pas_besoin_de_montant(self) -> None:
        """Sa valeur est celle des frais, connue à la commande seulement."""
        offerte = reward(kind=RewardKind.FREE_DELIVERY, discount_minor=0)

        assert offerte.pk is not None


class TestF5JournalImmuable:
    def test_chaque_mouvement_laisse_une_ligne(self, customer: User, order: Order) -> None:
        LoyaltyService.earn(user=customer, order=order)

        entry = PointsEntry.objects.get()
        assert entry.kind == EntryKind.EARNED
        assert entry.delta == 40  # 4 000 F à 1 point pour 100 F
        assert entry.balance_after == 40
        assert entry.order == order

    def test_le_solde_est_derivable_du_journal(self, customer: User, order: Order) -> None:
        """La propriété qui rend un écart **constatable** au lieu d'être supposé
        absent."""
        prix = reward(points_cost=30)
        LoyaltyService.earn(user=customer, order=order)
        LoyaltyService.redeem(user=customer, reward=prix)

        account = PointsAccount.objects.get(user=customer)
        somme = sum(e.delta for e in account.entries.all())

        assert somme == account.balance == 10

    def test_la_base_refuse_un_mouvement_vide(self, customer: User) -> None:
        """Un mouvement de zéro point n'est pas un mouvement : il encombrerait
        le journal sans rien expliquer."""
        from django.db.utils import IntegrityError

        account = LoyaltyService.account_for(customer)
        with pytest.raises(IntegrityError):
            PointsEntry.objects.create(
                account=account, kind=EntryKind.ADJUSTED, delta=0, balance_after=0
            )

    def test_un_debit_est_signe_negativement(self, customer: User) -> None:
        """Une colonne signée plutôt que deux : la somme du journal doit tomber
        sur le solde en une addition."""
        prix = reward(points_cost=100)
        crediter(customer, 100)

        LoyaltyService.redeem(user=customer, reward=prix)

        assert PointsEntry.objects.get(kind=EntryKind.SPENT).delta == -100


class TestGainSurCommande:
    def test_la_livraison_credite(self, customer: User, order: Order) -> None:
        entry = LoyaltyService.earn(user=customer, order=order)

        assert entry is not None
        assert PointsAccount.objects.get(user=customer).balance == 40

    def test_un_evenement_rejoue_ne_credite_pas_deux_fois(
        self, customer: User, order: Order
    ) -> None:
        """Idempotent **par contrainte** et non par un `if déjà_crédité`, que
        deux workers franchiraient tous les deux."""
        LoyaltyService.earn(user=customer, order=order)
        LoyaltyService.earn(user=customer, order=order)

        assert PointsEntry.objects.filter(kind=EntryKind.EARNED).count() == 1
        assert PointsAccount.objects.get(user=customer).balance == 40

    def test_une_commande_trop_petite_ne_credite_rien(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        petite = build_order(
            restaurant,
            customer,
            reference="EC000099",
            subtotal=Money(50, XOF),
            delivery_fee=Money(0, XOF),
            total=Money(50, XOF),
        )

        assert LoyaltyService.earn(user=customer, order=petite) is None
        assert not PointsEntry.objects.exists()

    def test_le_credit_arrive_a_la_livraison_et_pas_au_paiement(
        self, customer: User, order: Order, courier: Any
    ) -> None:
        """Une commande payée puis annulée rapporterait des points pour un repas
        jamais reçu.

        Le signal est branché dans `apps.py` : ce test vérifie le câblage, pas
        seulement le service.
        """
        from apps.orders.services import OrderService

        for cible in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
        ):
            OrderService.transition_to(order=order, target=cible)

        assert not PointsEntry.objects.exists()

        OrderService.transition_to(order=order, target=OrderStatus.DELIVERED)

        assert PointsAccount.objects.get(user=customer).balance == 40


class TestCodeNominatif:
    """Un code frappé par un échange n'appartient qu'à celui qui l'a payé."""

    def test_l_echange_frappe_un_code_a_son_nom(self, customer: User) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 100)

        result = LoyaltyService.redeem(user=customer, reward=prix)

        assert result.promotion.owner == customer
        assert result.promotion.code.startswith("FID-")
        assert result.promotion.usage_limit == 1
        assert result.promotion.amount == Money(500, XOF)

    def test_un_autre_client_ne_peut_pas_s_en_servir(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        """Sans `owner`, `usage_limit_per_user=1` empêcherait seulement de
        l'utiliser deux fois, pas de l'utiliser par quelqu'un d'autre — et un
        code court finit par circuler."""
        prix = reward(points_cost=100)
        crediter(customer, 100)
        result = LoyaltyService.redeem(user=customer, reward=prix)

        with pytest.raises(PromotionRefused, match="n'existe pas"):
            PromotionService.quote(
                code=result.promotion.code,
                user=courier_user,
                restaurant=restaurant,
                subtotal=Money(5_000, XOF),
                delivery_fee=Money(500, XOF),
            )

    def test_le_refus_ne_dit_pas_a_qui_il_appartient(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        """Le message serait un annuaire."""
        prix = reward(points_cost=100)
        crediter(customer, 100)
        result = LoyaltyService.redeem(user=customer, reward=prix)

        with pytest.raises(PromotionRefused) as leve:
            PromotionService.quote(
                code=result.promotion.code,
                user=courier_user,
                restaurant=restaurant,
                subtotal=Money(5_000, XOF),
                delivery_fee=Money(500, XOF),
            )

        assert customer.email not in str(leve.value)

    def test_son_titulaire_s_en_sert_normalement(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 100)
        result = LoyaltyService.redeem(user=customer, reward=prix)

        devis = PromotionService.quote(
            code=result.promotion.code,
            user=customer,
            restaurant=restaurant,
            subtotal=Money(5_000, XOF),
            delivery_fee=Money(500, XOF),
        )

        assert devis.discount == Money(500, XOF)

    def test_une_campagne_ouverte_n_a_pas_de_titulaire(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        """`owner` nul doit rester le cas ordinaire : sinon l'ajout du champ
        aurait fermé toutes les promotions existantes."""
        Promotion.objects.create(
            code="BIENVENUE",
            kind=DiscountKind.PERCENTAGE,
            percentage=10,
            starts_at=timezone.now() - dt.timedelta(days=1),
            ends_at=timezone.now() + dt.timedelta(days=7),
        )

        for quiconque in (customer, courier_user):
            devis = PromotionService.quote(
                code="BIENVENUE",
                user=quiconque,
                restaurant=restaurant,
                subtotal=Money(5_000, XOF),
                delivery_fee=Money(500, XOF),
            )
            assert devis.discount == Money(500, XOF)

    def test_une_livraison_offerte_frappe_un_code_de_livraison(self, customer: User) -> None:
        offerte = reward(kind=RewardKind.FREE_DELIVERY, discount_minor=0, points_cost=50)
        crediter(customer, 50)

        result = LoyaltyService.redeem(user=customer, reward=offerte)

        assert result.promotion.kind == DiscountKind.FREE_DELIVERY

    def test_le_code_expire_selon_la_recompense(self, customer: User) -> None:
        """Un code sans fin encombrerait le catalogue de promotions pour
        toujours."""
        prix = reward(points_cost=100, validity_days=7)
        crediter(customer, 100)

        result = LoyaltyService.redeem(user=customer, reward=prix)

        assert (result.promotion.ends_at - result.promotion.starts_at).days == 7

    def test_une_recompense_suspendue_ne_s_echange_pas(self, customer: User) -> None:
        prix = reward(is_active=False)
        crediter(customer, 100)

        with pytest.raises(BusinessRuleViolation, match="plus disponible"):
            LoyaltyService.redeem(user=customer, reward=prix)


class TestExpiration:
    def test_un_solde_inactif_s_eteint(self, customer: User) -> None:
        account = crediter(customer, 100)
        PointsAccount.objects.filter(pk=account.pk).update(
            last_activity_at=timezone.now() - dt.timedelta(days=400)
        )

        assert expire_points() == 1

        account.refresh_from_db()
        assert account.balance == 0

    def test_l_extinction_laisse_une_trace(self, customer: User) -> None:
        """Un solde qui tombe à zéro sans explication est un solde
        incontestable."""
        account = crediter(customer, 100)
        PointsAccount.objects.filter(pk=account.pk).update(
            last_activity_at=timezone.now() - dt.timedelta(days=400)
        )

        expire_points()

        entry = PointsEntry.objects.get(kind=EntryKind.EXPIRED)
        assert entry.delta == -100
        assert entry.balance_after == 0
        assert "inactivité" in entry.description

    def test_un_compte_actif_n_est_pas_touche(self, customer: User) -> None:
        account = crediter(customer, 100)

        assert expire_points() == 0

        account.refresh_from_db()
        assert account.balance == 100

    def test_un_compte_jamais_actif_n_est_pas_parcouru(self, customer: User) -> None:
        """Il n'a rien à perdre, et le parcourir chaque jour coûterait à mesure
        que la base grossit."""
        LoyaltyService.account_for(customer)

        assert expire_points() == 0

    def test_l_expiration_ne_repousse_pas_l_activite(self, customer: User) -> None:
        """Sinon l'extinction elle-même compterait comme un mouvement et le
        compte repartirait pour douze mois."""
        account = crediter(customer, 100)
        ancienne = timezone.now() - dt.timedelta(days=400)
        PointsAccount.objects.filter(pk=account.pk).update(last_activity_at=ancienne)

        expire_points()

        account.refresh_from_db()
        assert account.last_activity_at is not None
        assert account.last_activity_at < timezone.now() - dt.timedelta(days=300)

    def test_la_fenetre_est_reglable(self, customer: User) -> None:
        account = crediter(customer, 100)
        PointsAccount.objects.filter(pk=account.pk).update(
            last_activity_at=timezone.now() - dt.timedelta(days=70)
        )

        assert expire_points(months=2) == 1


class TestRoutes:
    def test_le_solde_se_consulte(self, as_customer: APIClient, customer: User) -> None:
        crediter(customer, 120)

        response = as_customer.get(reverse("v1:loyalty:account"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["balance"] == 120
        assert response.data["lifetime_earned"] == 120

    def test_un_compte_neuf_repond_zero_sans_rien_ecrire(
        self, as_customer: APIClient, customer: User
    ) -> None:
        """Une lecture d'écran ne doit pas créer de ligne : le compte s'ouvre au
        premier mouvement, c'est-à-dire quand il porte une information."""
        response = as_customer.get(reverse("v1:loyalty:account"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["balance"] == 0
        assert not PointsAccount.objects.exists()

    def test_le_catalogue_ne_montre_pas_les_recompenses_suspendues(
        self, as_customer: APIClient
    ) -> None:
        reward(name="Visible")
        reward(name="Retirée", is_active=False)

        response = as_customer.get(reverse("v1:loyalty:reward-list"))

        assert [r["name"] for r in response.data["results"]] == ["Visible"]

    def test_le_catalogue_reste_lisible_sous_le_solde(
        self, as_customer: APIClient, customer: User
    ) -> None:
        """Masquer ce qu'on ne peut pas encore s'offrir retirerait à la fidélité
        ce qui la fait fonctionner : savoir vers quoi on économise."""
        reward(name="Chère", points_cost=10_000)
        crediter(customer, 5)

        response = as_customer.get(reverse("v1:loyalty:reward-list"))

        assert [r["name"] for r in response.data["results"]] == ["Chère"]

    def test_la_remise_sort_en_montant_et_pas_en_entier_nu(self, as_customer: APIClient) -> None:
        """ADR-007 : la règle vaut pour tous les montants, sans exception
        locale."""
        reward(discount_minor=500)

        response = as_customer.get(reverse("v1:loyalty:reward-list"))

        assert response.data["results"][0]["discount"] == {"amount": "500", "currency": XOF}

    def test_l_echange_rend_le_code_et_le_nouveau_solde(
        self, as_customer: APIClient, customer: User
    ) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 250)

        response = as_customer.post(reverse("v1:loyalty:reward-redeem", args=[prix.pk]))

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["balance"] == 150
        assert response.data["promotion"]["code"].startswith("FID-")
        assert response.data["redemption"]["points_spent"] == 100

    def test_un_echange_sans_solde_sort_en_409(
        self, as_customer: APIClient, customer: User
    ) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 10)

        response = as_customer.post(reverse("v1:loyalty:reward-redeem", args=[prix.pk]))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "insufficient_balance"
        assert response.data["required"] == 100
        assert response.data["available"] == 10

    def test_l_echange_n_accepte_aucun_montant_du_client(self) -> None:
        """C1 transposé : le client désigne une récompense, jamais un coût ni un
        solde. Le champ n'existe dans aucun sérialiseur d'entrée."""
        from apps.loyalty import serializers as s

        for nom in ("PointsAccountSerializer", "PointsEntrySerializer", "RewardSerializer"):
            champs = getattr(s, nom)().fields
            assert all(champ.read_only for champ in champs.values()), nom

    def test_le_journal_se_consulte(
        self, as_customer: APIClient, customer: User, order: Order
    ) -> None:
        LoyaltyService.earn(user=customer, order=order)

        response = as_customer.get(reverse("v1:loyalty:entry-list"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["results"][0]["delta"] == 40
        assert response.data["results"][0]["balance_after"] == 40

    def test_le_journal_se_filtre_par_nature(
        self, as_customer: APIClient, customer: User, order: Order
    ) -> None:
        """« Où sont passés mes points » se répond en filtrant sur
        `expired`."""
        prix = reward(points_cost=30)
        LoyaltyService.earn(user=customer, order=order)
        LoyaltyService.redeem(user=customer, reward=prix)

        response = as_customer.get(reverse("v1:loyalty:entry-list"), {"kind": "spent"})

        assert [e["delta"] for e in response.data["results"]] == [-30]

    def test_les_echanges_passes_se_consultent(
        self, as_customer: APIClient, customer: User
    ) -> None:
        prix = reward(points_cost=100)
        crediter(customer, 100)
        LoyaltyService.redeem(user=customer, reward=prix)

        response = as_customer.get(reverse("v1:loyalty:redemption-list"))

        assert response.data["results"][0]["reward"]["name"] == "500 F de remise"
        assert response.data["results"][0]["promotion_code"].startswith("FID-")


class TestCloisonnement:
    """Le mouvement d'autrui est **introuvable**, pas interdit (ADR-005)."""

    def test_le_journal_d_autrui_est_invisible(
        self, as_customer: APIClient, courier_user: User, restaurant: Restaurant
    ) -> None:
        autre = build_order(restaurant, courier_user, reference="EC000042")
        LoyaltyService.earn(user=courier_user, order=autre)

        response = as_customer.get(reverse("v1:loyalty:entry-list"))

        assert response.data["results"] == []

    def test_les_echanges_d_autrui_sont_invisibles(
        self, as_customer: APIClient, courier_user: User
    ) -> None:
        prix = reward(points_cost=100)
        crediter(courier_user, 100)
        LoyaltyService.redeem(user=courier_user, reward=prix)

        response = as_customer.get(reverse("v1:loyalty:redemption-list"))

        assert response.data["results"] == []

    def test_le_solde_est_celui_du_jeton(
        self, as_customer: APIClient, customer: User, courier_user: User
    ) -> None:
        crediter(customer, 10)
        crediter(courier_user, 9_999)

        response = as_customer.get(reverse("v1:loyalty:account"))

        assert response.data["balance"] == 10

    def test_un_anonyme_n_a_rien(self) -> None:
        response = APIClient().get(reverse("v1:loyalty:account"))

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )
