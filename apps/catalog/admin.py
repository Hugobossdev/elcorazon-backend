"""Back-office du catalogue — invariants C1, S1.

C'est le seul endroit où un prix se fixe. C1 dit que le prix vit dans le
catalogue et nulle part ailleurs ; cet écran en est la contrepartie — si
l'exploitation ne peut pas changer un prix ici, elle le fera ailleurs, et
« ailleurs » finit toujours par vouloir dire « dans la requête du client ».

`is_verified_purchase` n'est modifiable nulle part, pas même ici : le champ est
`editable=False` au modèle, donc l'admin ne l'affiche pas en écriture. C'est
S1 tenu par la structure et non par la discipline de qui remplit le formulaire.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.catalog.models import (
    Category,
    MenuItem,
    Option,
    OptionGroup,
    Review,
    VerifiedPurchase,
)
from common.admin import ReadOnlyAdmin, money_display

__all__ = [
    "CategoryAdmin",
    "MenuItemAdmin",
    "OptionGroupAdmin",
    "ReviewAdmin",
    "VerifiedPurchaseAdmin",
]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "emoji", "restaurant", "sort_order", "is_active")
    list_filter = ("is_active", "restaurant")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    list_select_related = ("restaurant",)
    readonly_fields = ("created_at", "updated_at")


class OptionInline(admin.TabularInline):
    model = Option
    extra = 1
    fields = (
        "name",
        "price_delta_minor",
        "price_delta_currency",
        "is_default",
        "is_available",
        "sort_order",
    )


@admin.register(OptionGroup)
class OptionGroupAdmin(admin.ModelAdmin):
    """Groupes d'options d'un article.

    `min_select` et `max_select` portent la règle de validation en **donnée** :
    créer « choisir 2 accompagnements parmi 5 » ne demande aucun développement,
    et c'est tout l'intérêt de les saisir ici.
    """

    list_display = ("name", "menu_item", "min_select", "max_select", "sort_order")
    list_filter = ("menu_item__restaurant",)
    search_fields = ("name", "menu_item__name")
    list_select_related = ("menu_item",)
    inlines = (OptionInline,)


class OptionGroupInline(admin.TabularInline):
    model = OptionGroup
    extra = 0
    fields = ("name", "min_select", "max_select", "sort_order")
    show_change_link = True


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "restaurant",
        "category",
        "price_display",
        "is_available",
        "is_popular",
        "rating_average",
        "retire",
    )
    list_filter = ("is_available", "is_popular", "vip_exclusive", "restaurant", "category")
    list_editable = ("is_available", "is_popular")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    list_select_related = ("restaurant", "category")
    inlines = (OptionGroupInline,)
    actions = ("mettre_indisponible", "remettre_disponible")

    price_display = money_display("price", "Prix")

    # La note est un agrégat recalculé à chaque avis : la saisir à la main
    # produirait un chiffre que le prochain avis effacerait.
    readonly_fields = ("rating_average", "rating_count", "deleted_at", "created_at", "updated_at")

    fieldsets = (
        ("Article", {"fields": ("restaurant", "category", "name", "slug", "description", "image")}),
        (
            "Prix",
            {
                "fields": ("price_minor", "price_currency"),
                "description": (
                    "En unité mineure : 3 500 XOF s'écrit 3500. C'est la seule source "
                    "de vérité du prix — ni le panier ni la commande ne l'acceptent du "
                    "client."
                ),
            },
        ),
        (
            "Composition",
            {
                "fields": (
                    "preparation_minutes",
                    "calories",
                    "ingredients",
                    "allergens",
                    "dietary_tags",
                )
            },
        ),
        (
            "Mise en avant",
            {"fields": ("is_available", "is_popular", "vip_exclusive", "sort_order")},
        ),
        (
            "Notes et retrait",
            {
                "fields": ("rating_average", "rating_count", "deleted_at"),
                "classes": ("collapse",),
                "description": (
                    "Le retrait du catalogue est logique : des commandes passées "
                    "renvoient à cet article, et le supprimer réellement rendrait leur "
                    "historique illisible."
                ),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[MenuItem]:
        """Les articles retirés restent visibles ici.

        L'API les masque — ils ne sont plus au menu — mais l'exploitation doit
        pouvoir les retrouver, ne serait-ce que pour comprendre une ancienne
        commande ou remettre au menu ce qu'on a retiré par erreur.
        """
        return super().get_queryset(request)

    @admin.display(description="Retiré", boolean=True)
    def retire(self, obj: MenuItem) -> bool:
        return obj.is_deleted

    @admin.action(description="Marquer indisponible (rupture)")
    def mettre_indisponible(self, request: HttpRequest, queryset: QuerySet[MenuItem]) -> None:
        """Rupture du jour, pas retrait du menu.

        Les deux se confondent facilement et ne veulent pas dire la même
        chose : l'article indisponible réapparaîtra demain, l'article retiré
        non.
        """
        updated = queryset.update(is_available=False)
        self.message_user(request, f"{updated} article(s) marqué(s) indisponible(s).")

    @admin.action(description="Remettre disponible")
    def remettre_disponible(self, request: HttpRequest, queryset: QuerySet[MenuItem]) -> None:
        updated = queryset.update(is_available=True)
        self.message_user(request, f"{updated} article(s) remis en vente.")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    """Avis clients.

    Modifiables nulle part — même ici, seule la suppression est offerte, et
    c'est un geste de modération. Corriger le texte d'un avis reviendrait à
    faire dire à un client ce qu'il n'a pas écrit.
    """

    list_display = ("menu_item", "user", "rating", "is_verified_purchase", "created_at")
    list_filter = ("rating", "is_verified_purchase", "menu_item__restaurant")
    search_fields = ("menu_item__name", "user__email", "comment")
    list_select_related = ("menu_item", "user")
    readonly_fields = (
        "menu_item",
        "user",
        "rating",
        "title",
        "comment",
        "is_verified_purchase",
        "helpful_count",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(VerifiedPurchase)
class VerifiedPurchaseAdmin(ReadOnlyAdmin):
    """Trace « cet utilisateur a bien reçu cet article » (S1).

    Alimentée par `orders` à la livraison, jamais à la main : la modifier
    reviendrait à décider qui a le droit d'être marqué « achat vérifié », ce
    que le champ existe précisément pour éviter.
    """

    list_display = ("user", "menu_item", "last_purchased_at")
    search_fields = ("user__email", "menu_item__name")
    list_select_related = ("user", "menu_item")
