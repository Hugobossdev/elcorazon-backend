"""Mouvements de points et échanges — invariants F1, F2, F3, F5.

**F1 est la raison d'être de ce module.** L'implémentation précédente lisait le
solde, le comparait au coût, puis retirait. Entre la lecture et le retrait, une
seconde requête faisait la même chose : les deux trouvaient le solde suffisant,
les deux retiraient, et le compte finissait négatif avec deux récompenses
délivrées pour le prix d'une. C'est une course TOCTOU classique, et elle a été
reproduite.

Ajouter une vérification ne corrige rien — elle subirait la même course. La
correction est de **ne pas avoir d'instant entre la vérification et le
retrait** :

    UPDATE loyalty_pointsaccount
       SET balance = balance - %(cost)s
     WHERE id = %(id)s AND balance >= %(cost)s

Une seule opération, atomique par construction. Si elle n'affecte aucune ligne,
c'est que le solde ne suffisait pas — ou qu'une autre requête est passée avant.
Les deux cas se traitent pareil : refus.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import User
from apps.loyalty.models import (
    EntryKind,
    PointsAccount,
    PointsEntry,
    Reward,
    RewardKind,
    RewardRedemption,
)
from apps.orders.models import Order
from apps.promotions.models import DiscountKind, Promotion
from common.exceptions import BusinessRuleViolation, InsufficientBalance
from common.money import Money

__all__ = ["LoyaltyService", "RewardResult", "points_for"]


@dataclass(frozen=True, slots=True)
class RewardResult:
    redemption: RewardRedemption
    promotion: Promotion
    balance: int


def points_for(amount: Money) -> int:
    """Points gagnés sur un montant.

    Un diviseur en unité mineure plutôt qu'un taux flottant : à 1 point pour
    100 F, une commande de 4 000 F rapporte 40 points, exactement. Un taux en
    virgule flottante donnerait 39,999… et une troncature qui dépend de
    l'arrondi de la machine.

    La division entière fait le reste : on ne crédite jamais plus que ce qui
    est acquis.
    """
    diviseur = settings.LOYALTY_MINOR_UNITS_PER_POINT
    return max(amount.amount_minor // diviseur, 0)


class LoyaltyService:
    @staticmethod
    def account_for(user: User) -> PointsAccount:
        """Compte du client, créé à la demande.

        Le créer à l'inscription obligerait à traiter les comptes antérieurs à
        la fidélité ; le créer ici règle les deux cas d'un coup.
        """
        account, _ = PointsAccount.objects.get_or_create(user=user)
        return account

    # ------------------------------------------------------------- crédit

    @staticmethod
    @transaction.atomic
    def earn(*, user: User, order: Order, points: int | None = None) -> PointsEntry | None:
        """Crédite les points d'une commande livrée.

        **Idempotent par contrainte** : l'unicité de `(account, order)` sur les
        gains fait qu'un événement de livraison rejoué ne crédite pas deux
        fois. Ce n'est pas un `if déjà_crédité` — deux workers le franchiraient
        tous les deux.

        Renvoie `None` quand il n'y a rien à créditer : une commande dont le
        montant ne franchit pas le seuil d'un point, ou déjà créditée.
        """
        montant = points if points is not None else points_for(order.total)
        if montant <= 0:
            return None

        account = PointsAccount.objects.select_for_update().get_or_create(user=user)[0]

        if PointsEntry.objects.filter(account=account, order=order, kind=EntryKind.EARNED).exists():
            return None

        return LoyaltyService._move(
            account=account,
            delta=montant,
            kind=EntryKind.EARNED,
            order=order,
            description=f"Commande {order.reference}",
        )

    # -------------------------------------------------------------- débit

    @staticmethod
    @transaction.atomic
    def redeem(*, user: User, reward: Reward) -> RewardResult:
        """Échange des points contre une récompense.

        Le débit passe par `_spend`, qui vérifie et retire en une opération
        (F1). Tout ce qui suit — le mouvement de journal, le code frappé,
        l'échange enregistré — est dans la même transaction : soit le client a
        payé **et** reçu son code, soit rien ne s'est passé.
        """
        if not reward.is_active:
            raise BusinessRuleViolation("Cette récompense n'est plus disponible.")

        account = LoyaltyService.account_for(user)
        entry = LoyaltyService._spend(
            account=account,
            cost=reward.points_cost,
            description=f"Échange : {reward.name}",
        )

        promotion = LoyaltyService._mint_code(user=user, reward=reward)
        redemption = RewardRedemption.objects.create(
            user=user,
            reward=reward,
            points_spent=reward.points_cost,
            entry=entry,
            promotion_code=promotion.code,
        )

        account.refresh_from_db()
        return RewardResult(redemption=redemption, promotion=promotion, balance=account.balance)

    @staticmethod
    def _spend(*, account: PointsAccount, cost: int, description: str) -> PointsEntry:
        """Retrait conditionnel atomique — **le cœur de F1**.

        `filter(balance__gte=cost).update(...)` produit un unique
        `UPDATE ... WHERE balance >= cost`. Il n'existe aucun instant entre la
        condition et l'écriture, donc rien à intercaler. Zéro ligne affectée
        signifie que le solde ne suffisait pas, ou qu'une requête concurrente
        est passée avant — et dans les deux cas la réponse est la même.
        """
        if cost <= 0:  # pragma: no cover - refusé par contrainte de base (F2)
            raise BusinessRuleViolation("Le coût d'une récompense doit être positif.")

        affectees = PointsAccount.objects.filter(pk=account.pk, balance__gte=cost).update(
            balance=F("balance") - cost,
            lifetime_spent=F("lifetime_spent") + cost,
            last_activity_at=timezone.now(),
        )
        if affectees == 0:
            account.refresh_from_db()
            raise InsufficientBalance(
                f"Solde insuffisant : {cost} points demandés, {account.balance} disponibles.",
                required=cost,
                available=account.balance,
            )

        account.refresh_from_db()
        return PointsEntry.objects.create(
            account=account,
            kind=EntryKind.SPENT,
            delta=-cost,
            balance_after=account.balance,
            description=description,
        )

    # ------------------------------------------------------------ ajustement

    @staticmethod
    @transaction.atomic
    def adjust(*, user: User, points: int, description: str) -> PointsEntry:
        """Crédite un ajustement hors commande — succès débloqué, geste commercial.

        Passe par `_move`, comme `earn` : même mise à jour du solde, même ligne
        de journal, dans la même transaction. Un second chemin qui recopierait
        cette logique pour la gamification finirait par en diverger.

        `points` doit être strictement positif : un ajustement négatif
        reprendrait des points déjà dépensés en récompense sans que le client
        ait rien fait de mal. Un retrait disciplinaire reste un geste du
        back-office, pas un point d'entrée partagé.
        """
        if points <= 0:
            raise BusinessRuleViolation("Un ajustement crédité doit être strictement positif.")

        account = PointsAccount.objects.select_for_update().get_or_create(user=user)[0]
        return LoyaltyService._move(
            account=account, delta=points, kind=EntryKind.ADJUSTED, description=description
        )

    # ----------------------------------------------------------- expiration

    @staticmethod
    @transaction.atomic
    def expire_inactive(*, account: PointsAccount, moment: dt.datetime) -> PointsEntry | None:
        """Éteint le solde d'un compte resté sans mouvement.

        **Expiration par inactivité, et non par lot.** Faire expirer chaque
        crédit à sa date demanderait de suivre quel crédit a payé quel débit,
        c'est-à-dire un second journal de consommation. La politique retenue se
        dit en une phrase — « les points s'éteignent après douze mois sans
        activité » — ce qui compte autant pour le client que pour le code : il
        peut la comprendre, donc la contester.
        """
        if account.balance == 0:
            return None

        return LoyaltyService._move(
            account=account,
            delta=-account.balance,
            kind=EntryKind.EXPIRED,
            description=f"Expiration après inactivité depuis le {moment:%d/%m/%Y}",
            touch_activity=False,
        )

    # ---------------------------------------------------------------- outils

    @staticmethod
    def _move(
        *,
        account: PointsAccount,
        delta: int,
        kind: str,
        order: Order | None = None,
        description: str = "",
        touch_activity: bool = True,
    ) -> PointsEntry:
        """Applique un mouvement et journalise (F5).

        Réservé aux mouvements dont le signe est déjà décidé. Un **débit** de
        client passe par `_spend`, jamais par ici : ce chemin n'a pas la
        condition atomique, et l'emprunter pour retirer des points rouvrirait
        la course que F1 ferme.
        """
        nouveau = account.balance + delta
        if nouveau < 0:  # pragma: no cover - la base le refuserait aussi (F3)
            raise InsufficientBalance("Solde insuffisant.")

        account.balance = nouveau
        touched = ["balance"]
        if delta > 0:
            account.lifetime_earned += delta
            touched.append("lifetime_earned")
        if touch_activity:
            account.last_activity_at = timezone.now()
            touched.append("last_activity_at")

        account.save(update_fields=[*touched, "updated_at"])

        return PointsEntry.objects.create(
            account=account,
            kind=kind,
            delta=delta,
            balance_after=account.balance,
            order=order,
            description=description,
        )

    @staticmethod
    def _mint_code(*, user: User, reward: Reward) -> Promotion:
        """Frappe le code nominatif obtenu par l'échange.

        Une `Promotion` et non un mécanisme parallèle : la fidélité ne
        réinvente pas la remise, elle s'appuie sur celui qui porte déjà les
        cinq conditions de F4. Un code de fidélité est une promotion comme une
        autre, simplement attribuée à une personne — d'où `owner`, sans lequel
        un code court finirait par circuler.
        """
        maintenant = timezone.now()
        code = f"FID-{secrets.token_hex(4).upper()}"

        commun = {
            "code": code,
            "description": f"Récompense fidélité : {reward.name}",
            "owner": user,
            "restaurant": reward.restaurant,
            "starts_at": maintenant,
            "ends_at": maintenant + dt.timedelta(days=reward.validity_days),
            "usage_limit": 1,
            "usage_limit_per_user": 1,
        }

        if reward.kind == RewardKind.FREE_DELIVERY:
            return Promotion.objects.create(kind=DiscountKind.FREE_DELIVERY, **commun)

        return Promotion.objects.create(  # type: ignore[misc]
            kind=DiscountKind.FIXED,
            amount=Money(reward.discount_minor, reward.discount_currency),
            **commun,
        )
