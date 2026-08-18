"""Fidélité : points et récompenses — invariants F1, F2, F3, F5.

Le domaine où l'implémentation précédente avait une course critique prouvée :
deux échanges concurrents lisaient le même solde, le trouvaient suffisant, et
retiraient chacun leur dû. Résultat : solde négatif, deux récompenses pour le
prix d'une.

La correction ne tient pas à une vérification supplémentaire — elle serait
soumise à la même course — mais à trois choses structurelles :

* **F3** — le solde est un `PositiveIntegerField` : PostgreSQL refuse une
  valeur négative. Ce n'est pas une garde applicative qu'on peut contourner,
  c'est le type de la colonne ;
* **F1** — le débit est un `UPDATE ... WHERE balance >= coût`, en une seule
  opération. Il n'y a pas d'instant entre la vérification et le retrait, donc
  rien à intercaler ;
* **F5** — chaque mouvement laisse une ligne de journal portant le solde
  d'après. Le solde est ainsi **dérivable** du journal, ce qui permet de
  constater un écart au lieu de le supposer absent.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from apps.orders.models import Order
from apps.payments.models import Transaction
from apps.restaurants.models import Restaurant
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel, state_check_constraint
from common.money import Money
from common.state_machine import StateMachine

__all__ = [
    "SUBSCRIPTION_MACHINE",
    "EntryKind",
    "PointsAccount",
    "PointsEntry",
    "Reward",
    "RewardKind",
    "RewardRedemption",
    "Subscription",
    "SubscriptionPayment",
    "SubscriptionPlan",
    "SubscriptionStatus",
]


class PointsAccount(UUIDModel, TimeStampedModel):
    """Solde de points d'un client.

    Le solde est **dénormalisé** : il pourrait se recalculer depuis le journal
    à chaque lecture, mais il est lu à chaque ouverture d'écran et modifié
    rarement. Le garder ici permet surtout le débit conditionnel atomique de
    F1, qu'une somme d'agrégat ne saurait pas exprimer.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="points_account")

    # F3 — `PositiveIntegerField` pose un `CHECK >= 0` en base. Le solde ne peut
    # donc pas devenir négatif, quel que soit le code qui l'écrit : ni un
    # service futur, ni un script d'exploitation, ni une migration hâtive.
    balance = models.PositiveIntegerField(default=0)

    lifetime_earned = models.PositiveIntegerField(default=0)
    lifetime_spent = models.PositiveIntegerField(default=0)

    # Sert à l'expiration : les points s'éteignent après une période sans
    # mouvement, pas à date fixe.
    last_activity_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "compte de fidélité"
        verbose_name_plural = "comptes de fidélité"

    def __str__(self) -> str:
        return f"{self.user.email} — {self.balance} points"


class EntryKind(models.TextChoices):
    EARNED = "earned", "Gagnés sur une commande"
    SPENT = "spent", "Dépensés en récompense"
    EXPIRED = "expired", "Expirés"
    ADJUSTED = "adjusted", "Ajustement manuel"


class PointsEntry(UUIDModel):
    """Mouvement de points — **journal immuable** (F5).

    Aucune mise à jour, aucune suppression : un mouvement erroné se corrige par
    un mouvement inverse, comme en comptabilité. Réécrire l'historique ferait
    perdre la seule trace permettant d'expliquer un solde à un client qui le
    conteste.

    `balance_after` est enregistré plutôt que recalculé : il fige ce que valait
    le compte à cet instant, et rend l'écart visible si le solde dénormalisé
    dérivait un jour.
    """

    account = models.ForeignKey(PointsAccount, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=16, choices=EntryKind.choices, db_index=True)

    # Signé : positif au crédit, négatif au débit. Un champ unique plutôt que
    # deux colonnes « débit » et « crédit » — la somme du journal doit tomber
    # sur le solde en une addition.
    delta = models.IntegerField()
    balance_after = models.PositiveIntegerField()

    # La commande qui a produit le mouvement, s'il y en a une. `loyalty` réagit
    # aux commandes — c'est le sens autorisé par le graphe (ADR-002) — donc la
    # clé étrangère est possible ici, contrairement à `promotions`.
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="points_entries"
    )
    description = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "mouvement de points"
        verbose_name_plural = "mouvements de points"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(delta=0), name="points_entry_not_empty"),
            # Idempotence du gain : une commande ne crédite qu'une fois, même
            # si l'événement de livraison est rejoué.
            models.UniqueConstraint(
                fields=["account", "order"],
                condition=models.Q(kind=EntryKind.EARNED),
                name="one_earning_per_order",
            ),
        ]
        indexes = [models.Index(fields=["account", "-created_at"])]

    def __str__(self) -> str:
        signe = "+" if self.delta > 0 else ""
        return f"{signe}{self.delta} — {self.get_kind_display()}"


class RewardKind(models.TextChoices):
    DISCOUNT = "discount", "Remise sur une commande"
    FREE_DELIVERY = "free_delivery", "Livraison offerte"


class Reward(UUIDModel, TimeStampedModel):
    """Récompense échangeable contre des points."""

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=16, choices=RewardKind.choices)

    # F2 — coût strictement positif. Un coût négatif transformerait le débit en
    # crédit : chaque échange rapporterait des points au lieu d'en coûter, et
    # c'est exactement ce que l'implémentation précédente permettait.
    points_cost = models.PositiveIntegerField()

    # Valeur de la remise obtenue, pour `discount`. En unité mineure, comme
    # partout ailleurs.
    discount_minor = models.PositiveIntegerField(default=0)
    discount_currency = models.CharField(max_length=3, default="XOF")

    # Durée de validité du code obtenu. Un code sans fin encombrerait le
    # catalogue de promotions pour toujours.
    validity_days = models.PositiveSmallIntegerField(default=30)

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name="rewards"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "récompense"
        ordering = ["points_cost"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(points_cost__gt=0), name="reward_cost_is_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(kind=RewardKind.DISCOUNT) | models.Q(discount_minor__gt=0),
                name="reward_discount_is_set",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.points_cost} points)"

    @property
    def discount(self) -> Money:
        """La remise obtenue, recomposée en `Money`.

        Une propriété plutôt que le `MoneyField` de `common.fields` : ce dernier
        matérialise un `BigIntegerField`, là où le montant doit ici être
        **positif** — la contrainte `reward_discount_is_set` s'appuie sur
        `discount_minor__gt=0`. Les deux colonnes sont donc déclarées à la main,
        et recomposées ici pour que la sérialisation reste celle de l'ADR-007
        partout, sans exception locale.
        """
        return Money(self.discount_minor, self.discount_currency)


class RewardRedemption(UUIDModel):
    """Échange effectué : des points contre un code.

    Le code frappé est une `Promotion` nominative : la fidélité ne réinvente
    pas la remise, elle s'appuie sur le mécanisme qui la porte déjà, avec ses
    cinq conditions (F4). Un code de fidélité est une promotion comme une
    autre, simplement attribuée à une personne.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reward_redemptions")
    reward = models.ForeignKey(Reward, on_delete=models.PROTECT, related_name="redemptions")

    points_spent = models.PositiveIntegerField()
    entry = models.OneToOneField(
        PointsEntry,
        on_delete=models.PROTECT,
        related_name="redemption",
        help_text="Le débit qui a payé cet échange. Sans lui, l'échange n'a pas été payé.",
    )
    promotion_code = models.CharField(max_length=32)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "échange de récompense"
        verbose_name_plural = "échanges de récompense"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.reward.name}"


class SubscriptionStatus(models.TextChoices):
    PENDING = "pending", "En attente du premier paiement"
    ACTIVE = "active", "Active"
    CANCELLED = "cancelled", "Résiliée"
    EXPIRED = "expired", "Expirée"


SUBSCRIPTION_TRANSITIONS: dict[str, set[str]] = {
    SubscriptionStatus.PENDING: {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED},
    SubscriptionStatus.ACTIVE: {SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED},
    SubscriptionStatus.CANCELLED: set(),
    SubscriptionStatus.EXPIRED: set(),
}

# Le renouvellement ne transite pas par la machine : il reconduit le même état
# `active` en repoussant `current_period_end`, ce qu'un graphe acyclique ne
# peut pas représenter comme une transition (ce serait un cycle sur un seul
# état). Seuls l'activation, la résiliation et l'expiration changent l'état.
SUBSCRIPTION_MACHINE = StateMachine(SUBSCRIPTION_TRANSITIONS, name="abonnement")


class SubscriptionPlan(UUIDModel, TimeStampedModel):
    """Plan tarifé — catalogue serveur (P4).

    Le prix vient d'ici, jamais du client : l'implémentation précédente
    acceptait `monthly_price` dans la requête d'inscription, ce qui permettait
    de s'abonner au tarif de son choix. Souscrire ne prend donc qu'un
    identifiant de plan ; le montant facturé est relu depuis `price`.
    """

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    price = MoneyField()
    billing_period_days = models.PositiveSmallIntegerField(default=30)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "plan d'abonnement"
        ordering = ["price_minor"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price_minor__gt=0), name="plan_price_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(billing_period_days__gt=0), name="plan_period_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.price})"


class Subscription(UUIDModel, TimeStampedModel):
    """Abonnement d'un client à un plan.

    Un seul abonnement **ouvert** (`pending` ou `active`) à la fois par
    client — la contrainte ci-dessous le porte en base, pas seulement le
    service : reprendre un abonnement pendant qu'un autre attend son premier
    paiement produirait deux facturations concurrentes pour la même personne.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(
        SubscriptionPlan, on_delete=models.PROTECT, related_name="subscriptions"
    )
    status = models.CharField(
        max_length=16,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.PENDING,
        db_index=True,
    )
    auto_renew = models.BooleanField(default=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "abonnement"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(SUBSCRIPTION_MACHINE, "status", "subscription_status_in_enum"),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(
                    status__in=[SubscriptionStatus.PENDING, SubscriptionStatus.ACTIVE]
                ),
                name="one_open_subscription_per_user",
            ),
        ]
        indexes = [models.Index(fields=["status", "current_period_end"])]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.plan.name} ({self.get_status_display()})"


class SubscriptionPayment(UUIDModel, TimeStampedModel):
    """Lien entre un abonnement et la transaction qui règle une échéance.

    Vit dans `loyalty` et non dans `payments` : le graphe de dépendances
    n'autorise la flèche que dans un sens (`loyalty` → `payments`), et
    `payments` ne doit rien savoir des abonnements pour rester réutilisable
    par le prochain domaine qui encaissera hors commande.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="payments"
    )
    transaction = models.OneToOneField(
        Transaction, on_delete=models.PROTECT, related_name="subscription_payment"
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()

    class Meta:
        verbose_name = "paiement d'abonnement"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.subscription} — {self.transaction.provider_reference}"
