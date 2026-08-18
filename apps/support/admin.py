"""Back-office du support.

Contrairement aux commandes, un ticket ou une réclamation n'a pas de machine à
états prouvée nécessaire : les statuts s'éditent ici directement. La seule
règle tenue par le code est l'horodatage de résolution, posé automatiquement
plutôt que confié à la mémoire de qui traite le dossier.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils import timezone

from apps.support.models import (
    Complaint,
    ReturnRequest,
    ReturnStatus,
    SupportMessage,
    SupportTicket,
    TicketStatus,
)
from common.admin import money_display

__all__ = ["ComplaintAdmin", "ReturnRequestAdmin", "SupportTicketAdmin"]

_TERMINAL_TICKET = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
_TERMINAL_RETURN = {ReturnStatus.REJECTED, ReturnStatus.REFUNDED}


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 1
    fields = ("author", "content", "created_at")
    readonly_fields = ("created_at",)


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "user", "category", "status", "created_at", "resolved_at")
    list_filter = ("category", "status")
    search_fields = ("subject", "user__email")
    list_select_related = ("user",)
    date_hierarchy = "created_at"
    inlines = (SupportMessageInline,)

    def save_model(self, request: HttpRequest, obj: SupportTicket, form: Any, change: bool) -> None:
        if obj.status in _TERMINAL_TICKET and obj.resolved_at is None:
            obj.resolved_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ("subject", "user", "order", "kind", "status", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("subject", "user__email", "order__reference")
    list_select_related = ("user", "order")
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("order", "user", "montant", "status", "created_at", "resolved_at")
    list_filter = ("status",)
    search_fields = ("user__email", "order__reference")
    list_select_related = ("user", "order")
    date_hierarchy = "created_at"

    montant = money_display("refund_amount", "Montant demandé")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: ReturnRequest, form: Any, change: bool) -> None:
        if obj.status in _TERMINAL_RETURN and obj.resolved_at is None:
            obj.resolved_at = timezone.now()
        super().save_model(request, obj, form, change)
