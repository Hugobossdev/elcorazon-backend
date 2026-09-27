"""Back-office des promotions.

C'est **l'écran qui existe pour l'exploitation** : créer « −500 F, dix premiers
clients, ce week-end » sans développement était toute la raison de porter les
conditions en données plutôt qu'en code.

`used_count` est en lecture seule. Le corriger à la main rouvrirait le quota
d'une campagne épuisée sans laisser de trace, ce qui est exactement le genre de
geste qu'on ne veut pas pouvoir faire sans y penser — les utilisations sont
consultables une par une juste en dessous.
"""

from __future__ import annotations

from django.contrib import admin

from apps.promotions.models import Promotion, PromotionRedemption
from common.admin import ReadOnlyAdmin, money_display

__all__ = ["PromotionAdmin", "PromotionRedemptionAdmin"]


class RedemptionInline(admin.TabularInline):
    model = PromotionRedemption
    extra = 0
    fields = ("user", "order_id", "discount_minor", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: object, obj: Promotion | None = None) -> bool:
        return False


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "kind",
        "valeur",
        "restaurant",
        "starts_at",
        "ends_at",
        "utilisations",
        "is_active",
    )
    list_filter = ("kind", "is_active", "restaurant")
    search_fields = ("code", "description", "owner__email")
    autocomplete_fields = ("owner",)
    list_select_related = ("restaurant",)
    inlines = (RedemptionInline,)
    readonly_fields = ("used_count", "created_at", "updated_at")

    amount_display = money_display("amount", "Montant")

    fieldsets = (
        ("Code", {"fields": ("code", "description", "is_active", "restaurant", "owner")}),
        (
            "Barème",
            {
                "fields": ("kind", "percentage", "amount_minor", "amount_currency"),
                "description": (
                    "Le pourcentage ne sert qu'au type « pourcentage », le montant "
                    "qu'au type « montant fixe ». Montants en unité mineure : "
                    "500 XOF s'écrit 500."
                ),
            },
        ),
        (
            "Conditions",
            {
                "fields": (
                    "starts_at",
                    "ends_at",
                    "min_order_amount_minor",
                    "min_order_amount_currency",
                    "max_discount_minor",
                    "max_discount_currency",
                    "usage_limit",
                    "usage_limit_per_user",
                    "used_count",
                ),
                "description": (
                    "Le plafond de remise n'est pas facultatif sur un pourcentage : "
                    "sans lui, « −20 % » sur une commande de groupe coûte ce qu'on "
                    "n'a pas prévu. Les quotas vides valent « illimité »."
                ),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Valeur")
    def valeur(self, obj: Promotion) -> str:
        if obj.kind == "percentage":
            return f"−{obj.percentage} %"
        if obj.kind == "fixed":
            return f"−{obj.amount}" if obj.amount else "—"
        return "Livraison offerte"

    @admin.display(description="Utilisations")
    def utilisations(self, obj: Promotion) -> str:
        return f"{obj.used_count} / {obj.usage_limit if obj.usage_limit is not None else '∞'}"


@admin.register(PromotionRedemption)
class PromotionRedemptionAdmin(ReadOnlyAdmin):
    """Utilisations effectives.

    `order_id` est un identifiant nu et non un lien : `promotions` est en amont
    de `orders` dans le graphe de dépendances, donc il ne connaît pas les
    commandes. Le coller dans la recherche du back-office des commandes fait le
    reste.
    """

    list_display = ("promotion", "user", "order_id", "discount_display", "created_at")
    list_filter = ("promotion",)
    search_fields = ("user__email", "order_id", "promotion__code")
    list_select_related = ("promotion", "user")
    date_hierarchy = "created_at"

    discount_display = money_display("discount", "Remise")
