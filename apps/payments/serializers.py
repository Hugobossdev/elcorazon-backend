"""Contrats du paiement — ADR-009.

Aucun sérialiseur d'entrée ne porte de statut de paiement. C'est structurel :
le seul chemin qui fasse passer une transaction en `completed` est le webhook
signé du prestataire, et il n'y a donc pas de champ à protéger.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.payments.models import Refund, SplitPayment, SplitShare, Transaction, Withdrawal
from common.serializers import MoneyField

__all__ = [
    "CheckoutSerializer",
    "ParticipantSerializer",
    "RefundRequestSerializer",
    "RefundSerializer",
    "ShareCheckoutSerializer",
    "SplitCreateSerializer",
    "SplitPaymentSerializer",
    "SplitShareSerializer",
    "TransactionSerializer",
    "WebhookSerializer",
    "WithdrawalRequestSerializer",
    "WithdrawalSerializer",
]


class TransactionSerializer(serializers.ModelSerializer[Transaction]):
    amount = MoneyField(read_only=True)

    class Meta:
        model = Transaction
        fields = [
            "id",
            "order",
            "provider",
            "provider_reference",
            "amount",
            "status",
            "completed_at",
            "failure_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class CheckoutSerializer(serializers.Serializer[Any]):
    """Réponse à l'initiation : la transaction ouverte et où aller payer."""

    transaction = TransactionSerializer(read_only=True)
    checkout_url = serializers.URLField(read_only=True)
    instructions = serializers.CharField(read_only=True)


class WebhookSerializer(serializers.Serializer[Any]):
    """Notification du prestataire.

    Le corps est validé pour sa **forme** seulement. Son authenticité tient à
    la signature du corps brut, vérifiée avant que ce sérialiseur ne soit
    construit : un payload bien formé mais non signé n'atteint jamais ici.
    """

    event_id = serializers.CharField(max_length=128)
    provider_reference = serializers.CharField(max_length=128)
    status = serializers.CharField(max_length=16)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class RefundSerializer(serializers.ModelSerializer[Refund]):
    amount = MoneyField(read_only=True)

    class Meta:
        model = Refund
        fields = [
            "id",
            "order",
            "transaction",
            "amount",
            "reason",
            "status",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields


class SplitShareSerializer(serializers.ModelSerializer[SplitShare]):
    """Une part, telle que la voit son destinataire.

    `share_token` est rendu : c'est le lien à transmettre. Il ne l'est qu'aux
    participants du partage et à l'initiateur — le donner à un tiers reviendrait
    à lui laisser voir la commande.
    """

    amount = MoneyField(read_only=True)

    class Meta:
        model = SplitShare
        fields = [
            "id",
            "display_name",
            "phone",
            "amount",
            "status",
            "share_token",
            "created_at",
        ]
        read_only_fields = fields


class SplitPaymentSerializer(serializers.ModelSerializer[SplitPayment]):
    shares = SplitShareSerializer(many=True, read_only=True)
    total_amount = MoneyField(read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)

    class Meta:
        model = SplitPayment
        fields = [
            "id",
            "order",
            "order_reference",
            "total_amount",
            "status",
            "shares",
            "created_at",
        ]
        read_only_fields = fields


class ParticipantSerializer(serializers.Serializer[Any]):
    """Un convive à inviter.

    `user` est facultatif — la moitié des participants d'un repas partagé n'ont
    pas de compte, et exiger une inscription pour payer sa part ferait échouer
    la fonctionnalité sur son cas le plus courant.

    `amount` l'est aussi : omis pour tout le monde, le total est réparti à parts
    égales sans perdre une unité mineure.
    """

    display_name = serializers.CharField(max_length=150)
    user = serializers.PrimaryKeyRelatedField[Any](
        queryset=User.objects.filter(is_active=True), required=False, allow_null=True
    )
    phone = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    amount = MoneyField(required=False, allow_null=True)


class SplitCreateSerializer(serializers.Serializer[Any]):
    # Les bornes portent sur la **liste**, pas sur le sérialiseur imbriqué :
    # `ListSerializer` les accepte, `ParticipantSerializer` non. Deux convives
    # au minimum — en deçà ce n'est pas un partage — et vingt au plus, pour que
    # la création reste une transaction de taille bornée.
    participants = serializers.ListField(child=ParticipantSerializer(), min_length=2, max_length=20)


class ShareCheckoutSerializer(serializers.Serializer[Any]):
    share = SplitShareSerializer(read_only=True)
    checkout_url = serializers.URLField(read_only=True)
    instructions = serializers.CharField(read_only=True)


class WithdrawalSerializer(serializers.ModelSerializer[Withdrawal]):
    """Une demande de retrait, telle que le livreur la relit."""

    amount = MoneyField(read_only=True)

    class Meta:
        model = Withdrawal
        fields = [
            "id",
            "amount",
            "status",
            "provider_reference",
            "failure_reason",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields


class WithdrawalRequestSerializer(serializers.Serializer[Any]):
    """Le seul champ d'une demande : combien.

    Ni le bénéficiaire — c'est l'appelant — ni le statut : une demande qui
    naîtrait « versée » ferait sortir de l'argent sans que personne l'ait versé.
    """

    amount = MoneyField()


class RefundRequestSerializer(serializers.Serializer[Any]):
    transaction = serializers.UUIDField()
    amount = MoneyField()
    reason = serializers.CharField(max_length=500)
