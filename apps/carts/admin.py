"""Back-office du panier.

En lecture seule, et c'est presque une évidence : le panier est un état
éphémère, réécrit à chaque tapotement du client. Le modifier depuis le
back-office reviendrait à choisir à sa place ce qu'il va manger.

L'écran sert au diagnostic — « le client dit que son panier est vide » — pas à
la correction. Les montants n'y figurent pas, parce qu'ils **n'existent pas
en base** : le panier ne stocke aucun prix, il est valorisé à la lecture depuis
le catalogue (C1).
"""

from __future__ import annotations

from django.contrib import admin

from apps.carts.models import Cart, CartLine, CartLineOption
from common.admin import ReadOnlyAdmin

__all__ = ["CartAdmin"]


class CartLineOptionInline(admin.TabularInline):
    model = CartLineOption
    extra = 0
    fields = ("option",)
    readonly_fields = fields
    can_delete = False


class CartLineInline(admin.TabularInline):
    model = CartLine
    extra = 0
    fields = ("menu_item", "quantity", "notes", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: object, obj: Cart | None = None) -> bool:
        return False


@admin.register(Cart)
class CartAdmin(ReadOnlyAdmin):
    list_display = ("user", "restaurant", "line_count", "updated_at")
    list_filter = ("restaurant",)
    search_fields = ("user__email", "user__full_name")
    list_select_related = ("user", "restaurant")
    inlines = (CartLineInline,)

    @admin.display(description="Lignes")
    def line_count(self, obj: Cart) -> int:
        return obj.lines.count()
