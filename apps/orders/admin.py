"""Back-office des commandes — invariants C2, C3, C4, ADR-010.

**Le statut n'est pas un champ.** C'est la décision qui structure cet écran.
Django admin en ferait par défaut une liste déroulante, et cette liste
déroulante suffirait à écrire `delivered` sur une commande jamais partie : sans
passer par la machine à états, sans journal, sans créditer le livreur, sans
prévenir le client. On rouvrirait C3 et C4 par la porte de service après les
avoir fermés partout ailleurs.

Le statut se lit donc, et se change par une **action** qui appelle
`OrderService.transition_to` — la même porte que l'API. Une transition
impossible y est refusée ici comme là-bas, et laisse la même trace.

Les montants sont dans la même situation pour la même raison : ils sont
recomposés serveur (C2). Un total saisi à la main serait un total faux qui a
l'air juste.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.accounts.models import User
from apps.orders.models import IdempotencyKey, Order, OrderLine, OrderStatusEvent
from apps.orders.services import OrderService
from apps.orders.states import ORDER_MACHINE, OrderStatus
from common.admin import AccountingAdmin, ReadOnlyAdmin, money_display
from common.state_machine import IllegalTransition

__all__ = ["IdempotencyKeyAdmin", "OrderAdmin"]


class OrderLineInline(admin.TabularInline):
    """Lignes de la commande — copies figées, jamais modifiables.

    `item_name` et `unit_price` sont des copies prises à l'achat, pas des
    références vivantes. Les rendre éditables laisserait réécrire ce que le
    client a commandé et à quel prix, ce qui est exactement ce que la copie
    empêche.
    """

    model = OrderLine
    extra = 0
    fields = (
        "item_name",
        "quantity",
        "unit_price_display",
        "line_total_display",
        "options",
        "notes",
    )
    readonly_fields = fields
    can_delete = False

    unit_price_display = money_display("unit_price", "Prix unitaire")
    line_total_display = money_display("line_total", "Total ligne")

    def has_add_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False


class OrderStatusEventInline(admin.TabularInline):
    """Journal des transitions — l'historique, écrit par la machine à états."""

    model = OrderStatusEvent
    extra = 0
    fields = ("created_at", "from_status", "to_status", "actor", "reason")
    readonly_fields = fields
    can_delete = False
    ordering = ("created_at",)

    def has_add_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False


def _transition_action(target: str, label: str) -> Any:
    """Fabrique une action qui fait avancer par le service.

    Une action par cible plutôt qu'un formulaire libre : la liste des actions
    proposées **est** la documentation de ce qui est possible, et elle vient de
    la même table que celle qui refusera l'impossible.
    """

    @admin.action(description=label)
    def action(self: Any, request: HttpRequest, queryset: QuerySet[Order]) -> None:
        acteur = request.user if isinstance(request.user, User) else None

        avancees, refusees = 0, []
        for order in queryset:
            try:
                OrderService.transition_to(
                    order=order,
                    target=target,
                    actor=acteur,
                    reason="Back-office",
                )
                avancees += 1
            except IllegalTransition as exc:
                refusees.append(f"{order.reference} ({exc.source} → {target})")

        if avancees:
            self.message_user(request, f"{avancees} commande(s) en « {label} ».")
        if refusees:
            self.message_user(
                request,
                "Transition refusée pour : " + ", ".join(refusees),
                level=messages.WARNING,
            )

    action.__name__ = f"passer_en_{target}"
    return action


@admin.register(Order)
class OrderAdmin(AccountingAdmin):
    list_display = (
        "reference",
        "restaurant",
        "customer",
        "status",
        "total_display",
        "payment_method",
        "placed_at",
    )
    list_filter = ("status", "payment_method", "restaurant", "placed_at")
    search_fields = ("reference", "customer__email", "customer__full_name", "recipient_phone")
    date_hierarchy = "placed_at"
    list_select_related = ("restaurant", "customer")
    inlines = (OrderLineInline, OrderStatusEventInline)

    total_display = money_display("total", "Total")

    # Tout est en lecture seule. Ce qui se modifie légitimement — le statut —
    # passe par les actions ci-dessous ; le reste est comptable.
    readonly_fields = (
        "reference",
        "restaurant",
        "customer",
        "status",
        "transitions_possibles",
        "subtotal_display",
        "delivery_fee_display",
        "delivery_fee_gross_display",
        "discount_display",
        "total_display",
        "payment_method",
        "promo_code",
        "delivery_address_line",
        "delivery_landmark",
        "delivery_location",
        "delivery_instructions",
        "recipient_name",
        "recipient_phone",
        "placed_at",
        "estimated_delivery_at",
        "delivered_at",
        "cancelled_at",
        "cancellation_reason",
        "created_at",
        "updated_at",
    )

    subtotal_display = money_display("subtotal", "Sous-total")
    delivery_fee_display = money_display("delivery_fee", "Frais facturés")
    delivery_fee_gross_display = money_display("delivery_fee_gross", "Valeur de la course")
    discount_display = money_display("discount", "Remise")

    fieldsets = (
        (
            "Commande",
            {"fields": ("reference", "restaurant", "customer", "status", "transitions_possibles")},
        ),
        (
            "Montants",
            {
                "fields": (
                    "subtotal_display",
                    "delivery_fee_display",
                    "delivery_fee_gross_display",
                    "discount_display",
                    "total_display",
                    "payment_method",
                    "promo_code",
                ),
                "description": (
                    "Recomposés serveur depuis le catalogue et le barème de zone (C2). "
                    "« Frais facturés » et « valeur de la course » diffèrent sous "
                    "franco : le second est la base de la commission du livreur."
                ),
            },
        ),
        (
            "Livraison",
            {
                "fields": (
                    "delivery_address_line",
                    "delivery_landmark",
                    "delivery_location",
                    "delivery_instructions",
                    "recipient_name",
                    "recipient_phone",
                ),
                "description": (
                    "Copie figée de l'adresse au moment de la commande. Le carnet "
                    "d'adresses peut être effacé — le RGPD l'impose — sans rendre cette "
                    "commande illisible."
                ),
            },
        ),
        (
            "Horodatage",
            {
                "fields": (
                    "placed_at",
                    "estimated_delivery_at",
                    "delivered_at",
                    "cancelled_at",
                    "cancellation_reason",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    actions = tuple(
        f"passer_en_{statut}"
        for statut in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.CANCELLED,
        )
    )

    passer_en_confirmed = _transition_action(OrderStatus.CONFIRMED, "Confirmer")
    passer_en_preparing = _transition_action(OrderStatus.PREPARING, "Mettre en préparation")
    passer_en_ready = _transition_action(OrderStatus.READY, "Marquer prête")
    passer_en_cancelled = _transition_action(OrderStatus.CANCELLED, "Annuler")

    @admin.display(description="Transitions possibles")
    def transitions_possibles(self, obj: Order) -> str:
        """Ce que la machine autorise depuis l'état courant.

        Affiché plutôt que deviné : le personnel voit ce qu'il peut faire, et
        l'information vient de la même table que celle qui refusera le reste.
        """
        cibles = sorted(ORDER_MACHINE.targets_from(obj.status))
        return ", ".join(cibles) if cibles else "aucune (état terminal)"

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Consultable, jamais modifiable par formulaire.

        Ce qui doit changer passe par les actions, qui passent par le service.
        """
        return False


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(ReadOnlyAdmin):
    """Clés d'idempotence — utile au diagnostic d'un doublon signalé.

    Une clé sans `completed_at` est une requête qui n'a jamais abouti : soit
    elle est en vol, soit le processus est mort en cours de route. C'est la
    première chose à regarder quand un client dit avoir été bloqué.
    """

    list_display = ("key", "user", "endpoint", "response_status", "completed_at", "created_at")
    list_filter = ("endpoint", "response_status")
    search_fields = ("key", "user__email")
    list_select_related = ("user", "order")
    date_hierarchy = "created_at"
