"""Back-office du panier collaboratif.

En lecture seule, pour la même raison que le panier personnel : c'est un état
éphémère que ses participants réécrivent en permanence, et le corriger depuis le
back-office reviendrait à choisir à leur place ce qu'ils vont manger.

L'écran sert au diagnostic — « le groupe dit que le code ne marche pas », « la
commande n'est jamais partie » — et l'information utile y est le statut, l'hôte,
l'échéance et la commande produite. Les montants n'y figurent pas : ils
**n'existent pas en base** (C1).
"""

from __future__ import annotations

from django.contrib import admin

from apps.groupcarts.models import GroupCart, GroupCartLine, GroupCartMember
from common.admin import ReadOnlyAdmin

__all__ = ["GroupCartAdmin"]


class GroupCartMemberInline(admin.TabularInline):
    model = GroupCartMember
    extra = 0
    fields = ("user", "joined_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: object, obj: GroupCart | None = None) -> bool:
        return False


class GroupCartLineInline(admin.TabularInline):
    model = GroupCartLine
    extra = 0
    fields = ("member", "menu_item", "quantity", "notes", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: object, obj: GroupCart | None = None) -> bool:
        return False


@admin.register(GroupCart)
class GroupCartAdmin(ReadOnlyAdmin):
    list_display = ("code", "title", "restaurant", "host", "status", "closes_at", "order")
    list_filter = ("status", "restaurant")
    search_fields = ("code", "title", "host__email", "host__full_name")
    list_select_related = ("restaurant", "host", "order")
    inlines = (GroupCartMemberInline, GroupCartLineInline)
