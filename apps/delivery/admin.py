"""Back-office de la flotte — invariants L1, L4, L5.

C'est l'écran le plus utilisé de tout le back-office : **valider un dossier
livreur** est un geste quotidien, et c'est celui qui débloque quelqu'un qui
attend de pouvoir travailler.

Comme pour les commandes, `verification_status` n'est pas un champ. Le passer
par `CourierService.review` fait trois choses qu'un formulaire ne ferait pas :
il vérifie que la transition existe — on ne suspend pas un dossier jamais
validé —, il horodate et attribue la décision, et il remet le livreur hors
ligne quand le dossier cesse d'être valide. Ce dernier point compte : un
livreur suspendu resté « en ligne » continuerait d'apparaître dans les listes
d'affectation.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.accounts.models import User
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import CourierService
from apps.delivery.states import VERIFICATION_MACHINE, VerificationStatus
from common.admin import AccountingAdmin, money_display
from common.state_machine import IllegalTransition

__all__ = ["AssignmentAdmin", "CourierProfileAdmin"]


def _review_action(target: str, label: str) -> Any:
    @admin.action(description=label)
    def action(self: Any, request: HttpRequest, queryset: QuerySet[CourierProfile]) -> None:
        acteur = request.user
        if not isinstance(acteur, User):  # pragma: no cover - l'admin exige une session
            return

        traites, refuses = 0, []
        for courier in queryset:
            try:
                CourierService.review(
                    courier=courier,
                    target=target,
                    actor=acteur,
                    notes=f"Décision back-office ({label}).",
                )
                traites += 1
            except IllegalTransition as exc:
                refuses.append(f"{courier.user.full_name} ({exc.source} → {target})")

        if traites:
            self.message_user(request, f"{traites} dossier(s) en « {label} ».")
        if refuses:
            self.message_user(
                request, "Transition refusée pour : " + ", ".join(refuses), level=messages.WARNING
            )

    action.__name__ = f"marquer_{target}"
    return action


@admin.register(CourierProfile)
class CourierProfileAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "restaurant",
        "vehicle_type",
        "verification_status",
        "is_online",
        "deliveries_completed",
        "rating_average",
    )
    list_filter = ("verification_status", "is_online", "vehicle_type", "restaurant")
    search_fields = ("user__full_name", "user__email", "national_id_number", "vehicle_plate")
    list_select_related = ("user", "restaurant")
    autocomplete_fields = ("user",)

    actions = ("marquer_approved", "marquer_rejected", "marquer_suspended")

    marquer_approved = _review_action(VerificationStatus.APPROVED, "Valider le dossier")
    marquer_rejected = _review_action(VerificationStatus.REJECTED, "Rejeter le dossier")
    marquer_suspended = _review_action(VerificationStatus.SUSPENDED, "Suspendre")

    # Statut, compteurs et gains sont produits par le service : les éditer
    # laisserait valider un dossier sans trace, ou se payer sans course (L4).
    readonly_fields = (
        "verification_status",
        "transitions_possibles",
        "verified_by",
        "verified_at",
        "can_accept_orders",
        "deliveries_completed",
        "deliveries_cancelled",
        "rating_average",
        "rating_count",
        "total_earnings_display",
        "last_location",
        "last_location_at",
        "created_at",
        "updated_at",
    )

    total_earnings_display = money_display("total_earnings", "Gains cumulés")

    fieldsets = (
        ("Livreur", {"fields": ("user", "restaurant", "is_online", "can_accept_orders")}),
        (
            "Dossier",
            {
                "fields": (
                    "verification_status",
                    "transitions_possibles",
                    "verification_notes",
                    "verified_by",
                    "verified_at",
                ),
                "description": (
                    "Le statut se change par les actions de la liste, qui passent par "
                    "le service : il horodate la décision et remet le livreur hors "
                    "ligne si le dossier cesse d'être valide."
                ),
            },
        ),
        (
            "Pièces",
            {
                "fields": (
                    "national_id_number",
                    "licence_number",
                    "vehicle_type",
                    "vehicle_plate",
                    "id_document",
                    "licence_document",
                    "vehicle_document",
                ),
                "description": (
                    "Remplacer une pièce depuis l'application repasse le dossier en "
                    "attente (L5) : un dossier validé sur des documents qu'on a ensuite "
                    "changés n'est plus un dossier validé."
                ),
            },
        ),
        (
            "Activité",
            {
                "fields": (
                    "deliveries_completed",
                    "deliveries_cancelled",
                    "rating_average",
                    "rating_count",
                    "total_earnings_display",
                    "last_location",
                    "last_location_at",
                ),
                "classes": ("collapse",),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Nom")
    def full_name(self, obj: CourierProfile) -> str:
        return obj.user.full_name

    @admin.display(description="Transitions possibles")
    def transitions_possibles(self, obj: CourierProfile) -> str:
        cibles = sorted(VERIFICATION_MACHINE.targets_from(obj.verification_status))
        return ", ".join(cibles) if cibles else "aucune"


@admin.register(Assignment)
class AssignmentAdmin(AccountingAdmin):
    """Courses.

    Consultables, jamais créées ni supprimées ici : une course naît d'une
    proposition faite à un livreur éligible sur une commande prête, et la
    saisir à la main produirait une affectation que les gardes L1 et L2
    n'auraient jamais laissée passer.
    """

    list_display = (
        "order",
        "courier",
        "status",
        "courier_fee_display",
        "offered_at",
        "delivered_at",
    )
    list_filter = ("status", "courier__restaurant", "offered_at")
    search_fields = ("order__reference", "courier__user__full_name")
    list_select_related = ("order", "courier__user")
    date_hierarchy = "offered_at"

    courier_fee_display = money_display("courier_fee", "Rémunération")

    readonly_fields = (
        "order",
        "courier",
        "status",
        "courier_fee_display",
        "offered_at",
        "accepted_at",
        "picked_up_at",
        "delivered_at",
        "decline_reason",
        "proof_of_delivery",
        "created_at",
        "updated_at",
    )

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
