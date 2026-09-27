"""Contrats du support.

Le client désigne une commande ; il ne déclare ni son statut, ni sa
réclamation, ni le montant qu'il croit avoir payé — ces derniers se lisent
depuis la commande elle-même. Seul `refund_amount` est déclaré, et il est
plafonné par le service, jamais ici : la validation de forme (un entier
positif) n'est pas la même chose que la règle métier (au plus le total payé).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from apps.accounts.models import User
from apps.orders.models import Order
from apps.support.models import (
    Complaint,
    ComplaintKind,
    ComplaintStatus,
    ReturnRequest,
    ReturnStatus,
    SupportMessage,
    SupportTicket,
    TicketCategory,
    TicketStatus,
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


@extend_schema_serializer(component_name="SupportAuthor")
class AuthorSerializer(serializers.ModelSerializer[User]):
    """L'auteur d'un message de support — le client ou un agent, d'où `user_type`.

    Le nom de composant est **imposé**, et ce n'est pas cosmétique : `support`
    et `social` déclarent chacun un `AuthorSerializer`, et `drf-spectacular`
    nomme ses composants d'après la classe. Les deux tombaient donc sur
    « Author », avec des champs différents — un client engendré depuis ce
    schéma lisait `user_type` là où le serveur envoie `avatar`, ou l'inverse,
    selon celui des deux que le générateur avait écrit en dernier.

    L'avertissement existait ; il ne faisait pas échouer la génération, et
    `--fail-on-warn` n'était atteint qu'en présence d'une route qui référence
    les deux.
    """

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
        # `resolution` : ce que l'exploitation a répondu — le motif d'un refus,
        # d'abord. Le client ne pouvait pas savoir pourquoi son retour l'était.
        fields = [
            "id",
            "order",
            "reason",
            "items",
            "refund_amount",
            "status",
            "resolution",
            "resolved_at",
            "created_at",
        ]
        read_only_fields = fields


class ReturnRequestWriteSerializer(serializers.Serializer[Any]):
    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all())
    reason = serializers.CharField()
    items = serializers.ListField(child=serializers.CharField(max_length=200))
    refund_amount = MoneyField()


# ------------------------------------------------------------- back-office


class ManagedTicketSerializer(serializers.ModelSerializer[SupportTicket]):
    """Un ticket vu du support : qui écrit, sur quoi, et depuis quand.

    Le fil (`messages`) n'est rendu qu'au détail : la liste en porterait des
    centaines pour n'en afficher aucun.
    """

    customer_name = serializers.CharField(source="user.full_name", read_only=True)
    customer_email = serializers.CharField(source="user.email", read_only=True)
    customer_phone = serializers.CharField(source="user.phone", read_only=True)
    messages_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = SupportTicket
        fields = [
            "id",
            "user",
            "customer_name",
            "customer_email",
            "customer_phone",
            "category",
            "subject",
            "description",
            "attachments",
            "status",
            "resolution",
            "resolved_at",
            "messages_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ManagedTicketDetailSerializer(ManagedTicketSerializer):
    messages = SupportMessageSerializer(many=True, read_only=True)

    class Meta(ManagedTicketSerializer.Meta):
        fields = [*ManagedTicketSerializer.Meta.fields, "messages"]
        read_only_fields = fields


class TicketStatusSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=TicketStatus.choices)
    resolution = serializers.CharField(required=False, allow_blank=True, default="")


class ManagedComplaintSerializer(serializers.ModelSerializer[Complaint]):
    customer_name = serializers.CharField(source="user.full_name", read_only=True)
    customer_phone = serializers.CharField(source="user.phone", read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    restaurant_name = serializers.CharField(source="order.restaurant.name", read_only=True)

    class Meta:
        model = Complaint
        fields = [
            "id",
            "order",
            "order_reference",
            "restaurant_name",
            "customer_name",
            "customer_phone",
            "kind",
            "subject",
            "description",
            "photos",
            "status",
            "resolution",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ComplaintDecisionSerializer(serializers.Serializer[Any]):
    #: `pending` n'y est pas : on ne remet pas une réclamation « en attente »,
    #: on la prend en examen ou on statue.
    status = serializers.ChoiceField(
        choices=[
            (ComplaintStatus.UNDER_REVIEW, ComplaintStatus.UNDER_REVIEW.label),
            (ComplaintStatus.RESOLVED, ComplaintStatus.RESOLVED.label),
            (ComplaintStatus.REJECTED, ComplaintStatus.REJECTED.label),
        ]
    )
    resolution = serializers.CharField(required=False, allow_blank=True, default="")


class ManagedReturnSerializer(serializers.ModelSerializer[ReturnRequest]):
    refund_amount = MoneyField(read_only=True)
    customer_name = serializers.CharField(source="user.full_name", read_only=True)
    customer_phone = serializers.CharField(source="user.phone", read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    order_total = MoneyField(source="order.total", read_only=True)
    restaurant_name = serializers.CharField(source="order.restaurant.name", read_only=True)

    class Meta:
        model = ReturnRequest
        fields = [
            "id",
            "order",
            "order_reference",
            "order_total",
            "restaurant_name",
            "customer_name",
            "customer_phone",
            "reason",
            "items",
            "refund_amount",
            "status",
            "resolution",
            "resolved_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ReturnDecisionSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(
        choices=[
            (ReturnStatus.APPROVED, ReturnStatus.APPROVED.label),
            (ReturnStatus.REJECTED, ReturnStatus.REJECTED.label),
            (ReturnStatus.REFUNDED, ReturnStatus.REFUNDED.label),
        ]
    )
    resolution = serializers.CharField(required=False, allow_blank=True, default="")
