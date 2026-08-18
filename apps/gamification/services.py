"""Progression et déblocage — invariant G1.

Le progrès de chaque catalogue (succès, badges, défis) est **recalculé** à
chaque commande livrée, jamais accumulé : une commande annulée puis une autre
livrée ne doit pas laisser un compteur à côté raconter une histoire différente
de celle que les commandes elles-mêmes racontent.

Le déblocage — et la récompense qui va avec — doit néanmoins n'arriver
**qu'une fois**, même si le signal qui l'a déclenché est rejoué (l'idempotence
de la livraison n'est garantie que côté commande, pas ici). D'où le motif
répété dans ce module : un `UPDATE ... WHERE is_unlocked = false` avant de
créditer quoi que ce soit. Si la ligne affectée est nulle, quelqu'un d'autre
— ou un appel précédent — l'a déjà fait basculer, et la récompense ne part
pas une seconde fois.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import User
from apps.gamification.models import (
    Achievement,
    AchievementCondition,
    Badge,
    Challenge,
    UserAchievement,
    UserBadge,
    UserChallenge,
)
from apps.loyalty.models import PointsAccount
from apps.loyalty.services import LoyaltyService
from apps.orders.models import Order
from apps.orders.states import OrderStatus

__all__ = ["GamificationService"]


class GamificationService:
    @staticmethod
    @transaction.atomic
    def on_order_delivered(*, user: User, order: Order) -> None:
        """Réévalue succès, badges et défis après une livraison.

        Une seule commande peut faire franchir un seuil de plusieurs catalogues
        à la fois — d'où les trois passes dans la même transaction, plutôt que
        trois abonnements séparés qui rejoueraient chacun le calcul des stats.
        """
        stats = GamificationService._lifetime_stats(user)
        GamificationService._check_achievements(user, stats)
        GamificationService._check_badges(user)
        GamificationService._check_challenges(user)

    @staticmethod
    def _lifetime_stats(user: User) -> dict[str, int]:
        """Statistiques dérivées des commandes livrées, sur toute la durée de vie.

        Recalculées et non maintenues : un compteur séparé dérive tôt ou tard
        de ce que les commandes disent réellement, et rien ici ne le rendrait
        constatable comme le journal de la fidélité rend un solde contestable.
        """
        delivered = Order.objects.filter(customer=user, status=OrderStatus.DELIVERED)
        total_minor = delivered.aggregate(total=Sum("total_minor"))["total"] or 0
        return {
            AchievementCondition.ORDERS_COUNT: delivered.count(),
            AchievementCondition.TOTAL_SPENT_MINOR: total_minor,
        }

    @staticmethod
    def _check_achievements(user: User, stats: dict[str, int]) -> None:
        for achievement in Achievement.objects.filter(is_active=True):
            current = stats.get(achievement.condition_type, 0)
            progress = min(current, achievement.condition_value)

            entry, _ = UserAchievement.objects.get_or_create(user=user, achievement=achievement)
            if progress != entry.progress:
                UserAchievement.objects.filter(pk=entry.pk).update(progress=progress)

            if progress < achievement.condition_value:
                continue

            affected = UserAchievement.objects.filter(pk=entry.pk, is_unlocked=False).update(
                is_unlocked=True, unlocked_at=timezone.now()
            )
            if affected and achievement.points_reward > 0:
                LoyaltyService.adjust(
                    user=user,
                    points=achievement.points_reward,
                    description=f"Succès débloqué : {achievement.name}",
                )

    @staticmethod
    def _check_badges(user: User) -> None:
        """Seuil sur les points **gagnés à vie** — voir `Badge`.

        Lit le compte **sans le créer** : `LoyaltyService.account_for`
        ouvrirait une ligne dès la première commande, même pour un client dont
        aucun badge n'est encore à portée — recréant, ici, l'erreur que le
        compte de fidélité évite déjà en ne s'ouvrant qu'au premier mouvement.
        """
        account = PointsAccount.objects.filter(user=user).first()
        lifetime_earned = account.lifetime_earned if account else 0
        if lifetime_earned == 0:
            return

        for badge in Badge.objects.filter(is_active=True, points_required__lte=lifetime_earned):
            entry, _ = UserBadge.objects.get_or_create(user=user, badge=badge)
            UserBadge.objects.filter(pk=entry.pk, is_unlocked=False).update(
                is_unlocked=True, unlocked_at=timezone.now()
            )

    @staticmethod
    def _check_challenges(user: User) -> None:
        """Même mécanique qu'un succès, bornée à la fenêtre du défi.

        La progression ne compte que les commandes livrées **dans** la
        fenêtre : un défi hebdomadaire ne doit pas se solder par les repas
        d'une semaine précédente.
        """
        now = timezone.now()
        for challenge in Challenge.objects.filter(
            is_active=True, starts_at__lte=now, ends_at__gte=now
        ):
            current = GamificationService._stats_in_window(
                user, challenge.condition_type, challenge.starts_at, now
            )
            progress = min(current, challenge.target_value)

            entry, _ = UserChallenge.objects.get_or_create(user=user, challenge=challenge)
            if progress != entry.progress:
                UserChallenge.objects.filter(pk=entry.pk).update(progress=progress)

            if progress < challenge.target_value:
                continue

            affected = UserChallenge.objects.filter(pk=entry.pk, is_completed=False).update(
                is_completed=True, completed_at=now
            )
            if affected and challenge.reward_points > 0:
                LoyaltyService.adjust(
                    user=user,
                    points=challenge.reward_points,
                    description=f"Défi complété : {challenge.title}",
                )

    @staticmethod
    def _stats_in_window(
        user: User, condition_type: str, start: dt.datetime, end: dt.datetime
    ) -> int:
        delivered = Order.objects.filter(
            customer=user,
            status=OrderStatus.DELIVERED,
            created_at__gte=start,
            created_at__lte=end,
        )
        if condition_type == AchievementCondition.TOTAL_SPENT_MINOR:
            return delivered.aggregate(total=Sum("total_minor"))["total"] or 0
        return delivered.count()
