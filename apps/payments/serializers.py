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
from common.money import Money
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


class PaymentMethodSerializer(serializers.Serializer[Any]):
    """Un moyen de paiement accepté — code à renvoyer, libellé à afficher."""

    code = serializers.CharField(read_only=True)
    # `label` est aussi un attribut de `Field` (le libellé du formulaire) ; la
    # clé est gardée parce que c'est celle des `TextChoices` qu'elle recopie,
    # et que le client la lit sous ce nom.
    label = serializers.CharField(read_only=True)  # type: ignore[assignment]


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

    def validate_amount(self, value: Money) -> Money:
        # Un montant négatif **créditait** les gains avant que la contrainte
        # `withdrawal_amount_positive` n'annule la transaction : la base était
        # la seule défense, et le livreur lisait une erreur d'intégrité au lieu
        # d'une phrase.
        if value.amount_minor <= 0:
            raise serializers.ValidationError("Le montant à retirer doit être positif.")
        return value


class RefundRequestSerializer(serializers.Serializer[Any]):
    transaction = serializers.UUIDField()
    amount = MoneyField()
    reason = serializers.CharField(max_length=500)


# ------------------------------------------------------------- back-office


class ManagedWithdrawalSerializer(serializers.ModelSerializer[Withdrawal]):
    """Une demande de retrait, telle que l'exploitation l'instruit.

    Porte **de quoi verser** sans rouvrir le dossier livreur : à qui, sur quel
    numéro, pour quelle cuisine. Le numéro est celui du compte — le dossier
    livreur ne porte pas de coordonnées de versement distinctes, et en inventer
    un champ ici ferait croire qu'il existe.
    """

    amount = MoneyField(read_only=True)
    courier_name = serializers.CharField(source="courier.user.full_name", read_only=True)
    courier_phone = serializers.CharField(source="courier.user.phone", read_only=True)
    restaurant = serializers.CharField(source="courier.restaurant.slug", read_only=True)
    restaurant_name = serializers.CharField(source="courier.restaurant.name", read_only=True)
    processed_by_name = serializers.CharField(
        source="processed_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = Withdrawal
        fields = [
            "id",
            "courier",
            "courier_name",
            "courier_phone",
            "restaurant",
            "restaurant_name",
            "amount",
            "status",
            "provider_reference",
            "failure_reason",
            "processed_by_name",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class WithdrawalSettleSerializer(serializers.Serializer[Any]):
    """Constater un versement : la référence du virement est **exigée**.

    C'est ce qu'on cherchera le jour où le livreur affirmera n'avoir rien reçu.
    Un constat sans référence ne prouve rien — ni à lui, ni à la comptabilité.
    """

    provider_reference = serializers.CharField(max_length=128, trim_whitespace=True)


class WithdrawalRejectSerializer(serializers.Serializer[Any]):
    """Refuser un versement : le motif est exigé, et lu par le livreur."""

    reason = serializers.CharField(max_length=500, trim_whitespace=True)


class ManagedRefundSerializer(serializers.ModelSerializer[Refund]):
    """Un remboursement, vu de l'exploitation qui doit l'exécuter."""

    amount = MoneyField(read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    restaurant_name = serializers.CharField(source="order.restaurant.name", read_only=True)
    customer_name = serializers.CharField(source="order.customer.full_name", read_only=True)
    customer_phone = serializers.CharField(source="order.customer.phone", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.full_name", read_only=True)
    provider = serializers.CharField(source="transaction.provider", read_only=True)

    class Meta:
        model = Refund
        fields = [
            "id",
            "order",
            "order_reference",
            "restaurant_name",
            "customer_name",
            "customer_phone",
            "transaction",
            "provider",
            "amount",
            "reason",
            "status",
            "requested_by_name",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields


class RefundSettleSerializer(serializers.Serializer[Any]):
    """Constater un remboursement versé — la référence est facultative ici.

    Un remboursement en espèces, rendu au comptoir, n'en a pas. Un virement en
    a une, et `RefundService.settle` la joint au motif.
    """

    provider_reference = serializers.CharField(
        max_length=128, required=False, allow_blank=True, trim_whitespace=True
    )


class RefundCancelSerializer(serializers.Serializer[Any]):
    """Abandonner un remboursement — le motif est exigé.

    « Annulé » sans raison est exactement ce qu'on cherche à comprendre six
    mois plus tard, quand un client réclame un remboursement dont la trace dit
    qu'il n'a pas été versé.
    """

    reason = serializers.CharField(max_length=500, allow_blank=False, trim_whitespace=True)
