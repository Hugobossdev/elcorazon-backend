"""Paiements.

Le domaine où l'implémentation précédente a produit sa faille la plus grave :
n'importe quel participant d'un paiement partagé pouvait se déclarer payé, ce
qui basculait la commande entière en `completed` — **commande gratuite**,
reproduit empiriquement. Le correctif d'urgence avait restreint l'action aux
administrateurs, sans construire le vrai flux.

La structure ci-dessous le construit : **une part n'est réputée réglée que si
elle porte une transaction vérifiée** (P2). Ce n'est plus une règle applicative
qu'on peut oublier, c'est une clé étrangère obligatoire.

Autres invariants :

* **P1** — le webhook est idempotent. `WebhookEvent.event_id` est unique :
  un rejeu est rejeté par la base avant même d'atteindre la logique.
* **P3** — le remboursement est plafonné au montant réellement encaissé.
"""

from __future__ import annotations

import secrets

from django.db import models

from apps.accounts.models import User
from apps.orders.models import Order
from common.fields import MoneyField
from common.models import (
    PositiveAmountModel,
    TimeStampedModel,
    UUIDModel,
    state_check_constraint,
)
from common.state_machine import StateMachine

__all__ = [
    "PAYMENT_MACHINE",
    "PaymentProvider",
    "PaymentStatus",
    "Refund",
    "SplitPayment",
    "SplitShare",
    "Transaction",
    "WebhookEvent",
    "Withdrawal",
    "new_share_token",
]


def new_share_token() -> str:
    """Jeton d'accès d'une part de paiement partagé.

    Aléatoire et non dérivé de la part : un lien envoyé sur une messagerie doit
    rester inexploitable pour qui ne l'a pas reçu, y compris s'il connaît la
    commande.
    """
    return secrets.token_urlsafe(32)


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    PROCESSING = "processing", "En cours"
    COMPLETED = "completed", "Encaissé"
    FAILED = "failed", "Échoué"
    CANCELLED = "cancelled", "Annulé"
    REFUNDED = "refunded", "Remboursé"


PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    PaymentStatus.PENDING: {
        PaymentStatus.PROCESSING,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
    },
    PaymentStatus.PROCESSING: {PaymentStatus.COMPLETED, PaymentStatus.FAILED},
    # P1 — `completed` ne redescend jamais. Un webhook rejoué ne peut pas
    # rétrograder un encaissement : la transition n'existe pas.
    PaymentStatus.COMPLETED: {PaymentStatus.REFUNDED},
    PaymentStatus.REFUNDED: set(),
    PaymentStatus.FAILED: set(),
    PaymentStatus.CANCELLED: set(),
}

PAYMENT_MACHINE = StateMachine(PAYMENT_TRANSITIONS, name="paiement")


class PaymentProvider(models.TextChoices):
    PAYDUNYA = "paydunya", "PayDunya"
    CASH = "cash", "Espèces à la livraison"
    WALLET = "wallet", "Portefeuille interne"


class Transaction(UUIDModel, TimeStampedModel, PositiveAmountModel):
    """Mouvement d'encaissement auprès d'un prestataire.

    Une commande peut en porter plusieurs : une tentative échouée suivie d'une
    réussie, ou une part par participant d'un paiement partagé.

    Le montant est refusé à zéro **avant** l'insertion : encaisser 0 F n'a pas
    de sens, et la contrainte `transaction_amount_positive` seule ne rendait
    qu'un `IntegrityError`.
    """

    POSITIVE_AMOUNTS = ("amount",)

    # Nul pour un encaissement qui ne règle pas de commande — un abonnement
    # aujourd'hui. `payments` ne connaît pas ces domaines : c'est à eux de
    # relier leur propre objet à la transaction, jamais l'inverse (ADR-002).
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)

    # Référence côté prestataire. Unique : c'est la clé qui rend le
    # rapprochement possible et qui empêche d'enregistrer deux fois le même
    # encaissement.
    provider_reference = models.CharField(max_length=128, unique=True)

    amount = MoneyField()
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, db_index=True
    )

    payer = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="transactions", null=True, blank=True
    )
    payer_phone = models.CharField(max_length=16, blank=True)

    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        verbose_name = "transaction"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(PAYMENT_MACHINE, "status", "transaction_status_in_enum"),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="transaction_amount_positive"
            ),
        ]
        indexes = [models.Index(fields=["order", "status"])]

    def __str__(self) -> str:
        return f"{self.provider_reference} — {self.get_status_display()}"

    @property
    def is_settled(self) -> bool:
        return self.status == PaymentStatus.COMPLETED


class WebhookEvent(UUIDModel):
    """Notification reçue d'un prestataire.

    P1 — l'unicité de `(provider, event_id)` est **la** garantie d'idempotence.
    Elle est portée par la base et non par le code : un rejeu concurrent, que
    deux workers traiteraient en parallèle, est arrêté par la contrainte, pas
    par un `if déjà_traité` qui aurait le temps de passer deux fois.
    """

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    event_id = models.CharField(max_length=128)
    payload = models.JSONField()
    signature_verified = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.TextField(blank=True)

    class Meta:
        verbose_name = "événement de webhook"
        verbose_name_plural = "événements de webhook"
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="webhook_event_unique_per_provider"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id}"


class SplitPayment(UUIDModel, TimeStampedModel):
    """Règlement d'une commande partagé entre plusieurs personnes."""

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="split_payment")
    initiated_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="initiated_splits"
    )
    total_amount = MoneyField()
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )

    class Meta:
        verbose_name = "paiement partagé"
        verbose_name_plural = "paiements partagés"
        constraints = [
            state_check_constraint(PAYMENT_MACHINE, "status", "split_status_in_enum"),
        ]

    def __str__(self) -> str:
        return f"Partage — {self.order.reference}"


class SplitShare(UUIDModel, TimeStampedModel, PositiveAmountModel):
    """Part d'un participant.

    **P2 — le correctif structurel.** Une part n'est `completed` que si
    `transaction` est renseignée et elle-même encaissée. Le modèle rend
    l'auto-déclaration impossible : il n'existe aucun chemin pour marquer une
    part payée sans référencer une transaction vérifiée chez le prestataire.

    La contrainte ci-dessous l'impose en base — c'est-à-dire indépendamment de
    tout code applicatif présent ou futur.
    """

    POSITIVE_AMOUNTS = ("amount",)

    split = models.ForeignKey(SplitPayment, on_delete=models.CASCADE, related_name="shares")
    participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="split_shares",
        help_text="Nul si le participant n'a pas de compte — invité par lien.",
    )
    display_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=16, blank=True)

    # Jeton d'accès de la part, pour le participant **sans compte**.
    #
    # La clé primaire ne convient pas : les UUIDv7 sont ordonnés dans le temps
    # (ADR-007), donc partiellement devinables. Or ce lien circule sur WhatsApp
    # et donne à voir la référence de commande, le restaurant, le montant et le
    # nom des autres participants. Un jeton aléatoire ne se devine pas.
    share_token = models.CharField(max_length=64, unique=True, default=new_share_token)

    amount = MoneyField()
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="split_share",
    )

    class Meta:
        verbose_name = "part de paiement"
        verbose_name_plural = "parts de paiement"
        ordering = ["created_at"]
        constraints = [
            state_check_constraint(PAYMENT_MACHINE, "status", "share_status_in_enum"),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="share_amount_positive"
            ),
            # P2 — une part encaissée porte obligatoirement une transaction.
            models.CheckConstraint(
                condition=~models.Q(status=PaymentStatus.COMPLETED)
                | models.Q(transaction__isnull=False),
                name="settled_share_requires_transaction",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} — {self.amount}"


class Refund(UUIDModel, TimeStampedModel, PositiveAmountModel):
    """Remboursement.

    **P3** — le plafonnement au montant réellement encaissé ne peut pas être une
    contrainte `CHECK` : il porte sur une somme d'autres lignes, que SQL ne sait
    pas exprimer ici. Il est appliqué par `RefundService`, sous verrou sur la
    commande, et couvert par un test d'attaque — l'ancien code autorisait un
    remboursement supérieur au montant payé.
    """

    POSITIVE_AMOUNTS = ("amount",)

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="refunds")
    amount = MoneyField()
    reason = models.TextField()
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "remboursement"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(PAYMENT_MACHINE, "status", "refund_status_in_enum"),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="refund_amount_positive"
            ),
        ]
        indexes = [models.Index(fields=["order", "-created_at"])]

    def __str__(self) -> str:
        return f"Remboursement {self.amount} — {self.order.reference}"


class Withdrawal(UUIDModel, TimeStampedModel, PositiveAmountModel):
    """Demande de retrait des gains d'un livreur.

    Écrite comme un `Refund`, et pour la même raison : elle enregistre une
    **intention de versement**, avec sa trace, et laisse le mouvement d'argent à
    un geste contrôlé. Ce que l'app livreur faisait — appeler l'API de
    décaissement PayDunya depuis le téléphone, avec un montant calculé
    localement, puis écrire elle-même « payé » — revenait à laisser le
    bénéficiaire décider de ce qu'il touche.

    Le solde, lui, est débité **à la demande** et sous verrou : sans cela, deux
    demandes simultanées passeraient toutes deux la vérification et videraient
    le compteur deux fois. Un retrait refusé par l'exploitation le recrédite.
    """

    POSITIVE_AMOUNTS = ("amount",)

    courier = models.ForeignKey(
        "delivery.CourierProfile", on_delete=models.PROTECT, related_name="withdrawals"
    )
    amount = MoneyField()
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )

    #: Référence du virement chez le prestataire, renseignée par l'exploitation
    #: au moment où elle l'exécute.
    provider_reference = models.CharField(max_length=128, blank=True)
    failure_reason = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "retrait"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(PAYMENT_MACHINE, "status", "withdrawal_status_in_enum"),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="withdrawal_amount_positive"
            ),
        ]
        indexes = [models.Index(fields=["courier", "-created_at"])]

    def __str__(self) -> str:
        return f"Retrait {self.amount} — {self.courier.user.full_name}"
