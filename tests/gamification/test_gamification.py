"""Gamification — invariant G1.

Le fil conducteur : le progrès se recalcule depuis les commandes livrées à
chaque appel, et le déblocage — donc la récompense — ne doit arriver **qu'une
fois**, même si le service est appelé plusieurs fois pour la même livraison.

Deux précautions traversent ce module :

* `mark_delivered` fait passer une commande à `delivered` **directement en
  base**, sans machine à états ni signal : les tests qui appellent le service
  de gamification à la main veulent isoler son calcul, qui interroge
  `Order.objects.filter(status=DELIVERED)` — une commande restée `pending` en
  base n'y apparaîtrait pas, quel que soit l'appel de service fait par-dessus ;
* les commandes construites pour ces tests sont **sous le seuil de gain de la
  fidélité** (`points_for` renvoie 0 en dessous de 100 F) : la fidélité et la
  gamification sont deux abonnées indépendantes du même signal de commande
  livrée, et sans cette précaution leurs crédits s'additionneraient dans le
  solde, brouillant ce que chaque test cherche à vérifier isolément.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.gamification.models import (
    Achievement,
    AchievementCondition,
    Badge,
    Challenge,
    ChallengeKind,
    UserAchievement,
    UserBadge,
    UserChallenge,
)
from apps.gamification.services import GamificationService
from apps.loyalty.models import PointsAccount
from apps.loyalty.services import LoyaltyService
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"


def mark_delivered(order: Order) -> Order:
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED)
    order.refresh_from_db()
    return order


def petite_commande(restaurant: Restaurant, customer: User, reference: str) -> Order:
    """Sous le seuil de gain de la fidélité (F1) : isole le crédit de gamification."""
    return build_order(
        restaurant,
        customer,
        reference=reference,
        subtotal=Money(50, XOF),
        delivery_fee=Money(0, XOF),
        total=Money(50, XOF),
    )


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


class TestSucces:
    def test_le_seuil_franchi_debloque_et_credite(self, customer: User, order: Order) -> None:
        succes = Achievement.objects.create(
            name="Première commande",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=50,
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        entry = UserAchievement.objects.get(user=customer, achievement=succes)
        assert entry.is_unlocked
        assert entry.progress == 1
        assert PointsAccount.objects.get(user=customer).balance == 50

    def test_le_rejeu_ne_credite_pas_deux_fois(self, customer: User, order: Order) -> None:
        """G1 — même sans concurrence réelle, l'appel répété ne doit pas
        rouvrir la récompense."""
        succes = Achievement.objects.create(
            name="Première commande",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=50,
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)
        GamificationService.on_order_delivered(user=customer, order=order)

        assert PointsAccount.objects.get(user=customer).balance == 50
        assert UserAchievement.objects.get(user=customer, achievement=succes).progress == 1

    def test_le_progres_ne_depasse_pas_le_seuil(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        succes = Achievement.objects.create(
            name="Trois commandes",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=3,
            points_reward=10,
        )

        for i in range(5):
            commande = petite_commande(restaurant, customer, reference=f"EC{i:06d}")
            mark_delivered(commande)
            GamificationService.on_order_delivered(user=customer, order=commande)

        entry = UserAchievement.objects.get(user=customer, achievement=succes)
        assert entry.progress == 3
        assert PointsAccount.objects.get(user=customer).balance == 10

    def test_une_recompense_nulle_ne_credite_rien(self, customer: User, order: Order) -> None:
        Achievement.objects.create(
            name="Sans récompense",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=0,
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert not PointsAccount.objects.filter(user=customer).exists()

    def test_un_succes_inactif_n_est_pas_evalue(self, customer: User, order: Order) -> None:
        succes = Achievement.objects.create(
            name="Retiré",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            is_active=False,
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert not UserAchievement.objects.filter(user=customer, achievement=succes).exists()

    def test_le_credit_arrive_a_la_livraison(self, customer: User, restaurant: Restaurant) -> None:
        """Le câblage réel : `apps.py` abonne le récepteur, pas seulement le
        service testé directement."""
        Achievement.objects.create(
            name="Première commande",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=20,
        )
        commande = petite_commande(restaurant, customer, reference="EC000042")

        for cible in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
        ):
            OrderService.transition_to(order=commande, target=cible)

        assert not UserAchievement.objects.filter(user=customer, is_unlocked=True).exists()

        OrderService.transition_to(order=commande, target=OrderStatus.DELIVERED)

        assert PointsAccount.objects.get(user=customer).balance == 20


class TestBadges:
    def test_le_badge_se_debloque_sur_les_points_gagnes_a_vie(
        self, customer: User, order: Order
    ) -> None:
        badge = Badge.objects.create(title="Habitué", points_required=30)
        mark_delivered(order)
        LoyaltyService.earn(user=customer, order=order)  # 40 points (4 000 F)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert UserBadge.objects.get(user=customer, badge=badge).is_unlocked

    def test_le_badge_ne_se_reprend_pas_si_le_solde_baisse(
        self, customer: User, order: Order
    ) -> None:
        """Adossé au **gagné à vie**, pas au solde courant : dépenser ses
        points ne doit pas faire perdre le badge."""
        badge = Badge.objects.create(title="Habitué", points_required=30)
        mark_delivered(order)
        LoyaltyService.earn(user=customer, order=order)
        GamificationService.on_order_delivered(user=customer, order=order)
        assert UserBadge.objects.get(user=customer, badge=badge).is_unlocked

        account = PointsAccount.objects.get(user=customer)
        PointsAccount.objects.filter(pk=account.pk).update(balance=0)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert UserBadge.objects.get(user=customer, badge=badge).is_unlocked

    def test_sous_le_seuil_rien_ne_se_debloque(self, customer: User, order: Order) -> None:
        badge = Badge.objects.create(title="Grand habitué", points_required=1_000)
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert not UserBadge.objects.filter(user=customer, badge=badge, is_unlocked=True).exists()

    def test_sans_aucun_point_gagne_le_compte_n_est_pas_cree(
        self, customer: User, order: Order
    ) -> None:
        """Comme le compte de fidélité, qui ne s'ouvre qu'au premier
        mouvement : évaluer les badges ne doit pas en créer un vide."""
        Badge.objects.create(title="Grand habitué", points_required=1_000)
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert not PointsAccount.objects.filter(user=customer).exists()


class TestDefis:
    def test_un_defi_en_cours_se_complete_et_credite(self, customer: User, order: Order) -> None:
        now = timezone.now()
        defi = Challenge.objects.create(
            title="Défi du jour",
            challenge_type=ChallengeKind.DAILY,
            condition_type=AchievementCondition.ORDERS_COUNT,
            target_value=1,
            reward_points=15,
            starts_at=now - dt.timedelta(hours=1),
            ends_at=now + dt.timedelta(hours=1),
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)

        assert UserChallenge.objects.get(user=customer, challenge=defi).is_completed
        assert PointsAccount.objects.get(user=customer).balance == 15

    def test_une_commande_hors_fenetre_ne_compte_pas(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        now = timezone.now()
        defi = Challenge.objects.create(
            title="Défi de demain",
            challenge_type=ChallengeKind.DAILY,
            condition_type=AchievementCondition.ORDERS_COUNT,
            target_value=1,
            reward_points=15,
            starts_at=now + dt.timedelta(days=1),
            ends_at=now + dt.timedelta(days=2),
        )

        commande = petite_commande(restaurant, customer, reference="EC000042")
        mark_delivered(commande)
        GamificationService.on_order_delivered(user=customer, order=commande)

        assert not UserChallenge.objects.filter(user=customer, challenge=defi).exists()

    def test_le_rejeu_ne_credite_pas_deux_fois(self, customer: User, order: Order) -> None:
        now = timezone.now()
        Challenge.objects.create(
            title="Défi du jour",
            challenge_type=ChallengeKind.DAILY,
            condition_type=AchievementCondition.ORDERS_COUNT,
            target_value=1,
            reward_points=15,
            starts_at=now - dt.timedelta(hours=1),
            ends_at=now + dt.timedelta(hours=1),
        )
        mark_delivered(order)

        GamificationService.on_order_delivered(user=customer, order=order)
        GamificationService.on_order_delivered(user=customer, order=order)

        assert PointsAccount.objects.get(user=customer).balance == 15


class TestRoutes:
    def test_le_catalogue_de_succes_porte_le_progres_du_client(
        self, as_customer: APIClient, customer: User, order: Order
    ) -> None:
        Achievement.objects.create(
            name="Première commande",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=10,
        )
        mark_delivered(order)
        GamificationService.on_order_delivered(user=customer, order=order)

        response = as_customer.get(reverse("v1:gamification:achievement-list"))

        assert response.data["results"][0]["is_unlocked"] is True
        assert response.data["results"][0]["progress"] == 1

    def test_le_progres_d_autrui_est_invisible(
        self, as_customer: APIClient, courier_user: User, restaurant: Restaurant
    ) -> None:
        Achievement.objects.create(
            name="Première commande",
            condition_type=AchievementCondition.ORDERS_COUNT,
            condition_value=1,
            points_reward=10,
        )
        commande = petite_commande(restaurant, courier_user, reference="EC000099")
        mark_delivered(commande)
        GamificationService.on_order_delivered(user=courier_user, order=commande)

        response = as_customer.get(reverse("v1:gamification:achievement-list"))

        assert response.data["results"][0]["is_unlocked"] is False
        assert response.data["results"][0]["progress"] == 0

    def test_le_catalogue_de_badges_porte_le_progres_du_client(
        self, as_customer: APIClient, customer: User, order: Order
    ) -> None:
        badge = Badge.objects.create(title="Habitué", points_required=30)
        mark_delivered(order)
        LoyaltyService.earn(user=customer, order=order)  # 40 points (4 000 F)
        GamificationService.on_order_delivered(user=customer, order=order)

        response = as_customer.get(reverse("v1:gamification:badge-list"))

        résultat = next(b for b in response.data["results"] if b["title"] == badge.title)
        assert résultat["is_unlocked"] is True

    def test_seuls_les_defis_en_cours_apparaissent(self, as_customer: APIClient) -> None:
        now = timezone.now()
        Challenge.objects.create(
            title="En cours",
            challenge_type=ChallengeKind.DAILY,
            target_value=1,
            starts_at=now - dt.timedelta(hours=1),
            ends_at=now + dt.timedelta(hours=1),
        )
        Challenge.objects.create(
            title="Terminé",
            challenge_type=ChallengeKind.DAILY,
            target_value=1,
            starts_at=now - dt.timedelta(days=2),
            ends_at=now - dt.timedelta(days=1),
        )

        response = as_customer.get(reverse("v1:gamification:challenge-list"))

        assert [c["title"] for c in response.data["results"]] == ["En cours"]

    def test_un_anonyme_n_a_rien(self) -> None:
        response = APIClient().get(reverse("v1:gamification:badge-list"))

        assert response.status_code in (401, 403)
