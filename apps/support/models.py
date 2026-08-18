"""Support : tickets, réclamations, retours.

Trois entités, une seule question à chaque fois : « ce client est-il vraiment
le sien ? ». Un ticket n'a pas de commande à vérifier — le compte suffit —
mais une réclamation et une demande de retour portent sur une **commande**, et
la propriété de cette commande est vérifiée à la création
(`apps.support.services`), jamais ici : `order` et `user` n'ont aucune relation
déclarée qu'une contrainte de base saurait exprimer.

Aucune machine à états ici, à la différence des commandes : un ticket ou une
réclamation n'a pas d'historique d'incident prouvé sur l'implémentation
précédente — les statuts s'éditent depuis le back-office, en `ModelAdmin`
ordinaire, sans les garde-fous plus lourds qu'exigent commandes et livraisons.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.accounts.models import User
from apps.orders.models import Order
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel

__all__ = [
    "Complaint",
    "ComplaintKind",
    "ComplaintStatus",
    "ReturnRequest",
    "ReturnStatus",
    "SupportMessage",
    "SupportTicket",
    "TicketCategory",
    "TicketStatus",
]


class TicketCategory(models.TextChoices):
    ORDER = "order", "Commande"
    PAYMENT = "payment", "Paiement"
    ACCOUNT = "account", "Compte"
    DELIVERY = "delivery", "Livraison"
    OTHER = "other", "Autre"


class TicketStatus(models.TextChoices):
    OPEN = "open", "Ouvert"
    IN_PROGRESS = "in_progress", "En cours"
    RESOLVED = "resolved", "Résolu"
    CLOSED = "closed", "Fermé"


class SupportTicket(UUIDModel, TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_tickets")
    category = models.CharField(max_length=16, choices=TicketCategory.choices)
    subject = models.CharField(max_length=160)
    description = models.TextField()
    attachments = ArrayField(models.URLField(), default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=TicketStatus.choices, default=TicketStatus.OPEN
    )
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "ticket de support"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"]), models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"{self.subject} — {self.user.email}"


class SupportMessage(UUIDModel):
    """Message du fil d'un ticket — client ou personnel, sur le même modèle.

    Un seul champ `author` plutôt que `user_id` / `admin_id` distincts : c'est
    le même `User`, et son `user_type` dit déjà de quel côté il parle.
    """

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_messages")
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "message de support"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.author.email} — {self.ticket_id}"


class ComplaintKind(models.TextChoices):
    QUALITY = "quality", "Qualité"
    DELIVERY = "delivery", "Livraison"
    SERVICE = "service", "Service"
    OTHER = "other", "Autre"


class ComplaintStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    UNDER_REVIEW = "under_review", "En cours d'examen"
    RESOLVED = "resolved", "Résolue"
    REJECTED = "rejected", "Rejetée"


class Complaint(UUIDModel, TimeStampedModel):
    """Réclamation sur une commande — propriété vérifiée à la création (S3-like)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="complaints")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="complaints")
    kind = models.CharField(max_length=16, choices=ComplaintKind.choices)
    subject = models.CharField(max_length=160)
    description = models.TextField()
    photos = ArrayField(models.URLField(), default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=ComplaintStatus.choices, default=ComplaintStatus.PENDING
    )
    resolution = models.TextField(blank=True)

    class Meta:
        verbose_name = "réclamation"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.subject} — {self.order.reference}"


class ReturnStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    APPROVED = "approved", "Approuvée"
    REJECTED = "rejected", "Rejetée"
    REFUNDED = "refunded", "Remboursée"


class ReturnRequest(UUIDModel, TimeStampedModel):
    """Demande de retour — une **demande**, pas un remboursement exécuté.

    `refund_amount` est plafonné au total de la commande à la création
    (`apps.support.services`), mais son versement reste, comme partout
    ailleurs dans ce projet, un geste humain via `payments.RefundService` —
    la demande ne fait qu'enregistrer l'intention et sa justification.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="return_requests")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="return_requests")
    reason = models.TextField()
    items = ArrayField(models.CharField(max_length=200), help_text="Articles concernés, en clair.")
    refund_amount = MoneyField()
    status = models.CharField(
        max_length=16, choices=ReturnStatus.choices, default=ReturnStatus.PENDING
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "demande de retour"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(refund_amount_minor__gt=0), name="return_amount_positive"
            ),
        ]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.order.reference} — {self.get_status_display()}"
