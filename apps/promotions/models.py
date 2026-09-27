"""Codes promotionnels — invariant F4.

Cinq conditions, et il faut les cinq : période de validité, montant minimum,
plafond de remise, quota global, quota par utilisateur. Une seule oubliée et le
code devient une fuite — un « −20 % » sans plafond appliqué à une commande de
groupe, ou sans quota par personne réutilisé mille fois par la même.

Elles vivent **en données** et non en code : l'exploitation crée « −500 F, dix
premiers clients, ce week-end » depuis le back-office, sans développement.
C'est la même raison qui avait fait porter les bornes des groupes d'options par
le modèle plutôt que par des `if`.

Le décompte des utilisations est ici et non dans `orders` : `promotions` ne peut
pas connaître les commandes — le graphe de l'ADR-002 va dans l'autre sens. La
contrepartie est écrite sur `PromotionRedemption`.
"""

from __future__ import annotations

import datetime as dt

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel

__all__ = ["DiscountKind", "Promotion", "PromotionRedemption"]


class DiscountKind(models.TextChoices):
    PERCENTAGE = "percentage", "Pourcentage du sous-total"
    FIXED = "fixed", "Montant fixe"
    FREE_DELIVERY = "free_delivery", "Livraison offerte"


class Promotion(UUIDModel, TimeStampedModel):
    """Un code, ses conditions et son barème."""

    code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Saisi par le client. Comparé sans tenir compte de la casse.",
    )
    description = models.TextField(blank=True)

    kind = models.CharField(max_length=16, choices=DiscountKind.choices)

    # Pourcentage pour `percentage`, ignoré pour les autres types. En décimal
    # exact et non en flottant : 12,5 % d'un montant doit tomber juste.
    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    # Montant pour `fixed`, ignoré pour les autres types.
    amount = MoneyField(null=True)

    # --- Conditions (F4) --------------------------------------------------
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()

    min_order_amount = MoneyField(null=True)
    # Plafond d'une remise en pourcentage. Sans lui, « −20 % » sur une commande
    # de groupe coûte ce qu'on n'avait pas prévu. `MoneyField` ne porte pas de
    # `help_text` — c'est un descripteur, pas un champ Django — donc la
    # consigne est répétée dans le back-office, là où on saisit.
    max_discount = MoneyField(null=True)

    usage_limit = models.PositiveIntegerField(
        null=True, blank=True, help_text="Quota global. Vide = illimité."
    )
    usage_limit_per_user = models.PositiveIntegerField(
        null=True, blank=True, help_text="Quota par personne. Vide = illimité."
    )
    used_count = models.PositiveIntegerField(default=0, editable=False)

    # Un code peut être national ou propre à un établissement. Nul = partout.
    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name="promotions"
    )

    # Titulaire d'un code **nominatif**. Nul pour une campagne ouverte à tous.
    #
    # Un code frappé par un échange de points n'appartient qu'à celui qui l'a
    # payé : sans ce champ, `usage_limit_per_user=1` empêcherait seulement de
    # l'utiliser deux fois, pas de l'utiliser par quelqu'un d'autre — et un
    # code court finit par circuler.
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="promotions"
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "promotion"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="promotion_period_not_empty",
            ),
            models.CheckConstraint(
                # Une remise en pourcentage sans pourcentage, ou un montant fixe
                # sans montant, sont des codes qui ne remisent rien. Les refuser
                # en base évite d'avoir à s'en apercevoir en caisse.
                condition=~models.Q(kind=DiscountKind.PERCENTAGE) | models.Q(percentage__gt=0),
                name="promotion_percentage_is_set",
            ),
            models.CheckConstraint(
                condition=~models.Q(kind=DiscountKind.FIXED) | models.Q(amount_minor__gt=0),
                name="promotion_amount_is_set",
            ),
        ]
        indexes = [
            models.Index(fields=["code"]),
            models.Index(fields=["is_active", "starts_at", "ends_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.get_kind_display()})"

    def is_open_at(self, moment: dt.datetime | None = None) -> bool:
        """Le code est-il dans sa période ?

        Séparé de `is_active` : le premier est une décision d'exploitation —
        « on suspend ce code » — le second une donnée du calendrier. Les
        confondre obligerait à désactiver à la main chaque code expiré.
        """
        instant = moment or timezone.now()
        return self.is_active and self.starts_at <= instant < self.ends_at

    @property
    def is_exhausted(self) -> bool:
        return self.usage_limit is not None and self.used_count >= self.usage_limit


class PromotionRedemption(UUIDModel):
    """Utilisation effective d'un code sur une commande.

    `order_id` est un UUID **sans clé étrangère**, et c'est un compromis
    assumé : `promotions` est en amont de `orders` dans le graphe de
    dépendances (ADR-002), donc il ne peut pas référencer une commande sans
    créer un cycle. On perd l'intégrité référentielle et la cascade ; on garde
    un graphe acyclique et une app extractible.

    L'unicité sur `(promotion, order_id)` rend l'enregistrement idempotent :
    une commande rejouée — l'idempotence de l'ADR-009 le permet — ne consomme
    pas deux fois le quota.
    """

    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name="redemptions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="promotion_uses")
    order_id = models.UUIDField(db_index=True)

    discount = MoneyField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "utilisation de promotion"
        verbose_name_plural = "utilisations de promotion"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["promotion", "order_id"], name="one_redemption_per_promotion_and_order"
            )
        ]
        indexes = [models.Index(fields=["promotion", "user"])]

    def __str__(self) -> str:
        return f"{self.promotion.code} — {self.user.email}"
