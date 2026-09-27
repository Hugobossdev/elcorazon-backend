"""Back-office du support — `/api/v1/support/manage/*`.

## Ce que ce module comble

Les routes du support n'étaient ouvertes qu'aux clients (`IsCustomer`). Un
client écrivait, réclamait, demandait un retour — et le back-office n'avait
aucun moyen de le lire, encore moins de lui répondre. Tout se traitait dans
l'administration Django, dont les réponses ne partaient vers personne.

## Périmètre

* **Tickets** : ils ne portent ni commande ni établissement — un client écrit à
  l'enseigne. Ils se lisent donc avec la seule permission, comme les comptes
  clients (`/administration/customers/`) : c'est `support.read` qui décide qui
  lit ce qu'un client a écrit.
* **Réclamations et retours** : ils portent sur une commande, et suivent donc
  l'établissement de celle-ci, comme le reste du back-office. Hors périmètre,
  l'objet est introuvable (404).
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import Count, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from apps.support.models import Complaint, ReturnRequest, SupportTicket
from apps.support.serializers import (
    ComplaintDecisionSerializer,
    ManagedComplaintSerializer,
    ManagedReturnSerializer,
    ManagedTicketDetailSerializer,
    ManagedTicketSerializer,
    MessageWriteSerializer,
    ReturnDecisionSerializer,
    SupportMessageSerializer,
    TicketStatusSerializer,
)
from apps.support.services import SupportDeskService
from common.permissions import HasPermission, authenticated_user

__all__ = ["ManagedComplaintViewSet", "ManagedReturnViewSet", "ManagedTicketViewSet"]

SUPPORT_READ = "support.read"
SUPPORT_WRITE = "support.write"


class ManagedTicketViewSet(ReadOnlyModelViewSet[SupportTicket]):
    """Les tickets des clients — lecture, réponse, statut."""

    permission_classes = (HasPermission.of(SUPPORT_READ),)
    queryset = SupportTicket.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        # `in` : « à traiter » couvre deux statuts (ouvert et en cours, en
        # attente et en examen…). Sans lui, l'écran ferait deux requêtes et
        # recollerait deux pages, dont la pagination ne voudrait plus rien dire.
        "status": ["exact", "in"],
        "category": ["exact"],
        "user": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["subject", "user__full_name", "user__email"]

    def get_queryset(self) -> QuerySet[SupportTicket]:
        base = SupportTicket.objects.select_related("user").annotate(
            messages_count=Count("messages")
        )
        if self.action == "retrieve":
            base = base.prefetch_related("messages__author")
        # Les plus anciens non traités d'abord serait l'ordre d'un guichet ;
        # l'ordre par défaut reste « le plus récent », et l'écran filtre par
        # statut — ce qui répond à la même question sans surprendre ailleurs.
        return base.order_by("-created_at")

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action == "retrieve":
            return ManagedTicketDetailSerializer
        return ManagedTicketSerializer

    @extend_schema(
        request=MessageWriteSerializer,
        responses={201: SupportMessageSerializer},
        tags=["support"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="reply",
        url_name="reply",
        permission_classes=[HasPermission.of(SUPPORT_WRITE)],
    )
    def reply(self, request: Request, pk: str) -> Response:
        """Répond au client — il en est prévenu, et le ticket passe « en cours »."""
        ticket = self.get_object()
        serializer = MessageWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = SupportDeskService.answer(
            ticket=ticket,
            author=authenticated_user(request),
            content=serializer.validated_data["content"],
        )
        return Response(SupportMessageSerializer(message).data, status=http.HTTP_201_CREATED)

    @extend_schema(
        request=TicketStatusSerializer,
        responses={200: ManagedTicketSerializer},
        tags=["support"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="status",
        url_name="status",
        permission_classes=[HasPermission.of(SUPPORT_WRITE)],
    )
    def set_status(self, request: Request, pk: str) -> Response:
        """Résout, ferme ou rouvre. Résoudre exige de dire comment."""
        ticket = self.get_object()
        serializer = TicketStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        SupportDeskService.set_ticket_status(
            ticket=ticket,
            status=serializer.validated_data["status"],
            resolution=serializer.validated_data["resolution"],
            actor=authenticated_user(request),
        )
        return Response(ManagedTicketSerializer(self.get_queryset().get(pk=ticket.pk)).data)


class ManagedComplaintViewSet(ReadOnlyModelViewSet[Complaint]):
    """Les réclamations sur commande, dans le périmètre du compte."""

    serializer_class = ManagedComplaintSerializer
    permission_classes = (HasPermission.of(SUPPORT_READ),)
    queryset = Complaint.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact", "in"],
        "kind": ["exact"],
        "order": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["subject", "order__reference", "user__full_name"]

    def get_queryset(self) -> QuerySet[Complaint]:
        user = authenticated_user(self.request)
        base = Complaint.objects.select_related("user", "order__restaurant").order_by("-created_at")
        if is_unscoped(user):
            return base
        return base.filter(order__restaurant_id__in=staff_restaurant_ids(user))

    @extend_schema(
        request=ComplaintDecisionSerializer,
        responses={200: ManagedComplaintSerializer},
        tags=["support"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="decide",
        url_name="decide",
        permission_classes=[HasPermission.of(SUPPORT_WRITE)],
    )
    def decide(self, request: Request, pk: str) -> Response:
        """Prend en examen, résout ou rejette — le client lit la réponse."""
        complaint = self.get_object()
        serializer = ComplaintDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        SupportDeskService.decide_complaint(
            complaint=complaint,
            status=serializer.validated_data["status"],
            resolution=serializer.validated_data["resolution"],
            actor=authenticated_user(request),
        )
        return Response(ManagedComplaintSerializer(self.get_queryset().get(pk=complaint.pk)).data)


class ManagedReturnViewSet(ReadOnlyModelViewSet[ReturnRequest]):
    """Les demandes de retour, dans le périmètre du compte.

    Statuer ne rembourse rien : le remboursement se fait depuis la commande,
    et se constate dans « Remboursements » (`payments`, `orders.refund`).
    """

    serializer_class = ManagedReturnSerializer
    permission_classes = (HasPermission.of(SUPPORT_READ),)
    queryset = ReturnRequest.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact", "in"],
        "order": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["order__reference", "user__full_name"]

    def get_queryset(self) -> QuerySet[ReturnRequest]:
        user = authenticated_user(self.request)
        base = ReturnRequest.objects.select_related("user", "order__restaurant").order_by(
            "-created_at"
        )
        if is_unscoped(user):
            return base
        return base.filter(order__restaurant_id__in=staff_restaurant_ids(user))

    @extend_schema(
        request=ReturnDecisionSerializer,
        responses={200: ManagedReturnSerializer},
        tags=["support"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="decide",
        url_name="decide",
        permission_classes=[HasPermission.of(SUPPORT_WRITE)],
    )
    def decide(self, request: Request, pk: str) -> Response:
        """Approuve, refuse, ou constate le remboursement d'un retour."""
        demande = self.get_object()
        serializer = ReturnDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        SupportDeskService.decide_return(
            return_request=demande,
            status=serializer.validated_data["status"],
            resolution=serializer.validated_data["resolution"],
            actor=authenticated_user(request),
        )
        return Response(ManagedReturnSerializer(self.get_queryset().get(pk=demande.pk)).data)
