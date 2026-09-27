"""Back-office des paiements — invariants P1, P2, P3, C5.

**Tout est en lecture seule ici, sans exception.** Le webhook signé du
prestataire est la seule source de vérité de l'encaissement ; offrir un
formulaire qui écrirait `completed` sur une transaction reviendrait à rendre
possible depuis le back-office ce que P2 rend impossible partout ailleurs — se
déclarer payé. La faille la plus grave de l'implémentation précédente était
exactement celle-là, et son correctif d'alors avait consisté à *restreindre
l'action aux administrateurs*. C'est-à-dire à la laisser ouverte ici.

Le remboursement fait exception au sens où il s'initie : mais il passe par
`RefundService`, qui applique le plafond P3 sous verrou. Il n'y a pas de
formulaire de création de `Refund`.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.payments.models import (
    PaymentStatus,
    Refund,
    SplitPayment,
    SplitShare,
    Transaction,
    WebhookEvent,
)
from apps.payments.services import RefundService
from common.admin import ReadOnlyAdmin, money_display

__all__ = [
    "RefundAdmin",
    "SplitPaymentAdmin",
    "TransactionAdmin",
    "WebhookEventAdmin",
]


@admin.register(Transaction)
class TransactionAdmin(ReadOnlyAdmin):
    """Mouvements d'encaissement.

    Une commande peut en porter plusieurs : une tentative échouée suivie d'une
    réussie, ou une part par participant d'un paiement partagé. C'est
    `provider_reference` qui permet le rapprochement avec le relevé du
    prestataire.
    """

    list_display = (
        "provider_reference",
        "order",
        "provider",
        "amount_display",
        "status",
        "payer",
        "completed_at",
    )
    list_filter = ("status", "provider", "created_at")
    search_fields = ("provider_reference", "order__reference", "payer__email", "payer_phone")
    list_select_related = ("order", "payer")
    date_hierarchy = "created_at"

    amount_display = money_display("amount", "Montant")


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    """Remboursements.

    P3 — le plafond est le total encaissé **moins ce qui a déjà été
    remboursé**, appliqué par le service sous verrou sur la commande. Un
    formulaire de saisie ici contournerait ce calcul, et trois remboursements
    successifs rendraient trois fois la totalité.
    """

    list_display = ("order", "amount_display", "status", "requested_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order__reference", "reason", "requested_by__email")
    list_select_related = ("order", "transaction", "requested_by")
    date_hierarchy = "created_at"
    actions = ("constater_le_virement",)

    amount_display = money_display("amount", "Montant")

    @admin.action(description="Constater le virement (le remboursement a été versé)")
    def constater_le_virement(self, request: HttpRequest, queryset: QuerySet[Refund]) -> None:
        """Enregistre qu'un virement déjà effectué a bien eu lieu.

        ## Pourquoi une action et non un automatisme

        PayDunya n'expose **aucune** API de remboursement : le virement part de
        leur tableau de bord, à la main. `RefundService.refund` n'écrit donc
        qu'une intention, et rien — aucune route, aucune action, aucun service —
        ne la ramenait ensuite hors de `pending`.

        Les remboursements s'accumulaient dans une attente perpétuelle, et
        `PaymentStatus.REFUNDED` n'était atteint par personne. Ce qui manquait
        n'était pas l'automatisation, qui n'est pas réalisable : c'était le
        moyen de **constater** ce qu'un humain venait de faire.

        Cette action ne verse rien et ne prétend pas le contraire. Elle
        enregistre un fait extérieur, et son libellé le dit.
        """
        constates = 0
        for refund in queryset:
            if refund.status != PaymentStatus.PENDING:
                continue
            RefundService.settle(refund=refund)
            constates += 1

        ignores = len(queryset) - constates
        if constates:
            self.message_user(
                request,
                f"{constates} remboursement(s) constaté(s) comme versé(s).",
                messages.SUCCESS,
            )
        if ignores:
            self.message_user(
                request,
                f"{ignores} ligne(s) ignorée(s) : seul un remboursement en attente se constate.",
                messages.WARNING,
            )


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    """Notifications reçues des prestataires.

    L'écran de diagnostic du paiement. `processing_error` renseigné signale une
    notification acceptée mais non appliquée — le plus souvent une référence
    inconnue, c'est-à-dire un test du prestataire ou une erreur de
    configuration. `processed_at` vide sur un événement ancien est une anomalie
    à regarder.
    """

    list_display = (
        "event_id",
        "provider",
        "signature_verified",
        "received_at",
        "processed_at",
        "en_erreur",
    )
    list_filter = ("provider", "signature_verified", "received_at")
    search_fields = ("event_id",)
    date_hierarchy = "received_at"

    @admin.display(description="En erreur", boolean=True)
    def en_erreur(self, obj: WebhookEvent) -> bool:
        return bool(obj.processing_error)


class SplitShareInline(admin.TabularInline):
    """Parts d'un paiement partagé.

    P2 — une part n'est `completed` que si elle porte une transaction vérifiée,
    et la contrainte est **en base**. La rendre modifiable ici ne servirait à
    rien : la base refuserait l'écriture, et le message d'erreur serait
    incompréhensible.
    """

    model = SplitShare
    extra = 0
    fields = ("display_name", "participant", "amount_minor", "status", "transaction")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: object, obj: SplitPayment | None = None) -> bool:
        return False


@admin.register(SplitPayment)
class SplitPaymentAdmin(ReadOnlyAdmin):
    list_display = ("order", "initiated_by", "total_amount_display", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("order__reference", "initiated_by__email")
    list_select_related = ("order", "initiated_by")
    inlines = (SplitShareInline,)

    total_amount_display = money_display("total_amount", "Total")
