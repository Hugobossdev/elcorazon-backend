"""Back-office de la fidélité.

Le partage suit la nature de chaque table, et non la commodité :

* **le solde et le journal sont en lecture seule** — un solde saisi à la main
  serait un solde sans mouvement correspondant, donc inexplicable au client qui
  le conteste. Le journal, lui, est immuable par construction (F5) : le rendre
  éditable ici contredirait le modèle et, surtout, ferait perdre la seule trace
  qui permet d'expliquer un solde ;
* **le catalogue des récompenses s'édite** — c'est de la politique commerciale :
  un prix en points, une remise, une durée de validité se décident et se
  changent, comme un barème de promotion ;
* **les échanges se consultent** — un échange est une écriture appariée à un
  débit. En créer un à la main délivrerait un code que personne n'a payé.

Corriger des points se fait donc par un **mouvement d'ajustement** — le sens de
`EntryKind.ADJUSTED` — et non en réécrivant un solde. Le geste laisse une trace
signée d'un montant et d'un motif, ce qu'un champ modifié ne laisse pas.
"""

from __future__ import annotations

from django.contrib import admin

from apps.loyalty.models import (
    PointsAccount,
    PointsEntry,
    Reward,
    RewardRedemption,
    Subscription,
    SubscriptionPayment,
    SubscriptionPlan,
)
from common.admin import ReadOnlyAdmin, money_display

__all__ = [
    "PointsAccountAdmin",
    "PointsEntryAdmin",
    "RewardAdmin",
    "RewardRedemptionAdmin",
    "SubscriptionAdmin",
    "SubscriptionPaymentAdmin",
    "SubscriptionPlanAdmin",
]


class PointsEntryInline(admin.TabularInline):
    """Les derniers mouvements, sous le compte.

    C'est la vue qui sert réellement au support : « pourquoi ai-je 40 points de
    moins » se lit dans la suite des `balance_after`, pas dans le solde seul.
    """

    model = PointsEntry
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "delta", "balance_after", "description", "order")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request: object, obj: PointsAccount | None = None) -> bool:
        return False


@admin.register(PointsAccount)
class PointsAccountAdmin(ReadOnlyAdmin):
    list_display = ("user", "balance", "lifetime_earned", "lifetime_spent", "last_activity_at")
    search_fields = ("user__email", "user__full_name")
    list_select_related = ("user",)
    inlines = (PointsEntryInline,)


@admin.register(PointsEntry)
class PointsEntryAdmin(ReadOnlyAdmin):
    """Le journal complet, pour l'audit d'un écart.

    Une liste à plat en plus de l'inline du compte : chercher tous les
    mouvements d'expiration d'une nuit, ou retrouver le gain d'une commande
    donnée, ne se fait pas compte par compte.
    """

    list_display = ("created_at", "account", "kind", "delta", "balance_after", "order")
    list_filter = ("kind", "created_at")
    search_fields = ("account__user__email", "description", "order__reference")
    list_select_related = ("account__user", "order")
    date_hierarchy = "created_at"


@admin.register(Reward)
class RewardAdmin(admin.ModelAdmin):
    """Catalogue — le seul écran de ce module qui s'édite."""

    list_display = ("name", "kind", "points_cost", "remise", "validity_days", "is_active")
    list_filter = ("kind", "is_active", "restaurant")
    search_fields = ("name", "description")
    list_select_related = ("restaurant",)
    list_editable = ("is_active",)

    remise = money_display("discount", "Remise")

    fieldsets = (
        ("Récompense", {"fields": ("name", "description", "kind", "is_active")}),
        (
            "Barème",
            {
                "fields": ("points_cost", "discount_minor", "discount_currency"),
                "description": (
                    "Le coût est en points, la remise en unité mineure — 500 pour 500 F. "
                    "Une remise est exigée pour une récompense de type « remise »."
                ),
            },
        ),
        (
            "Portée",
            {
                "fields": ("restaurant", "validity_days"),
                "description": (
                    "Sans établissement, la récompense vaut partout. "
                    "La validité est celle du code obtenu, en jours."
                ),
            },
        ),
    )


@admin.register(RewardRedemption)
class RewardRedemptionAdmin(ReadOnlyAdmin):
    """Échanges effectués.

    L'écran du « je n'ai pas reçu mon code » : on y voit le code frappé, ce
    qu'il a coûté, et le mouvement qui l'a payé. Le débit et le code étant
    écrits dans la même transaction, un échange sans code ne peut pas exister —
    ce que cette liste rend vérifiable plutôt que théorique.
    """

    list_display = ("created_at", "user", "reward", "points_spent", "promotion_code")
    list_filter = ("reward", "created_at")
    search_fields = ("user__email", "promotion_code", "reward__name")
    list_select_related = ("user", "reward")
    date_hierarchy = "created_at"


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    """Catalogue des plans — le seul écran des abonnements qui s'édite (P4)."""

    list_display = ("name", "prix", "billing_period_days", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    list_editable = ("is_active",)

    prix = money_display("price", "Prix")


class SubscriptionPaymentInline(admin.TabularInline):
    """Les échéances réglées, sous l'abonnement — pour retrouver quel encaissement l'a activé."""

    model = SubscriptionPayment
    extra = 0
    can_delete = False
    fields = ("created_at", "transaction", "period_start", "period_end")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request: object, obj: Subscription | None = None) -> bool:
        return False


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyAdmin):
    """Un abonnement s'ouvre, se résilie ou se renouvelle par le service — jamais ici (ADR-010)."""

    list_display = (
        "user",
        "plan",
        "status",
        "auto_renew",
        "current_period_end",
    )
    list_filter = ("status", "auto_renew", "plan")
    search_fields = ("user__email", "user__full_name")
    list_select_related = ("user", "plan")
    date_hierarchy = "created_at"
    inlines = (SubscriptionPaymentInline,)


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(ReadOnlyAdmin):
    """Le journal des échéances, à plat — retrouver un règlement sans passer par l'abonnement."""

    list_display = ("created_at", "subscription", "transaction", "period_start", "period_end")
    search_fields = ("subscription__user__email", "transaction__provider_reference")
    list_select_related = ("subscription__user", "transaction")
    date_hierarchy = "created_at"
