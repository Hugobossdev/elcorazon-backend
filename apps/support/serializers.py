"""Contrats du support.

Le client désigne une commande ; il ne déclare ni son statut, ni sa
réclamation, ni le montant qu'il croit avoir payé — ces derniers se lisent
depuis la commande elle-même. Seul `refund_amount` est déclaré, et il est
plafonné par le service, jamais ici : la validation de forme (un entier
positif) n'est pas la même chose que la règle métier (au plus le total payé).
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.orders.models import Order
from apps.support.models import (
    Complaint,
    ComplaintKind,
    ReturnRequest,
    SupportMessage,
    SupportTicket,
    TicketCategory,
)
from common.serializers import MoneyField

__all__ = [
    "AuthorSerializer",
    "ComplaintSerializer",
    "ComplaintWriteSerializer",
    "MessageWriteSerializer",
    "ReturnRequestSerializer",
    "ReturnRequestWriteSerializer",
    "SupportMessageSerializer",
    "SupportTicketSerializer",
    "TicketCreateSerializer",
]


class AuthorSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ["id", "full_name", "user_type"]
        read_only_fields = fields


class SupportMessageSerializer(serializers.ModelSerializer[SupportMessage]):
    author = AuthorSerializer(read_only=True)

    class Meta:
        model = SupportMessage
        fields = ["id", "ticket", "author", "content", "created_at"]
        read_only_fields = fields


class SupportTicketSerializer(serializers.ModelSerializer[SupportTicket]):
    class Meta:
        model = SupportTicket
        fields = [
            "id",
            "category",
            "subject",
            "description",
            "attachments",
            "status",
            "resolution",
            "resolved_at",
            "created_at",
        ]
        read_only_fields = fields


class TicketCreateSerializer(serializers.Serializer[Any]):
    category = serializers.ChoiceField(choices=TicketCategory.choices)
    subject = serializers.CharField(max_length=160)
    description = serializers.CharField()
    attachments = serializers.ListField(child=serializers.URLField(), required=False, default=list)


class MessageWriteSerializer(serializers.Serializer[Any]):
    content = serializers.CharField()


class ComplaintSerializer(serializers.ModelSerializer[Complaint]):
    class Meta:
        model = Complaint
        fields = [
            "id",
            "order",
            "kind",
            "subject",
            "description",
            "photos",
            "status",
            "resolution",
            "created_at",
        ]
        read_only_fields = fields


class ComplaintWriteSerializer(serializers.Serializer[Any]):
    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all())
    kind = serializers.ChoiceField(choices=ComplaintKind.choices)
    subject = serializers.CharField(max_length=160)
    description = serializers.CharField()
    photos = serializers.ListField(child=serializers.URLField(), required=False, default=list)


class ReturnRequestSerializer(serializers.ModelSerializer[ReturnRequest]):
    refund_amount = MoneyField(read_only=True)

    class Meta:
        model = ReturnRequest
        fields = ["id", "order", "reason", "items", "refund_amount", "status", "created_at"]
        read_only_fields = fields


class ReturnRequestWriteSerializer(serializers.Serializer[Any]):
    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all())
    reason = serializers.CharField()
    items = serializers.ListField(child=serializers.CharField(max_length=200))
    refund_amount = MoneyField()
