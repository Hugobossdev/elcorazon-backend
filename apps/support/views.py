"""Points d'entrée du support.

Trois ressources, toutes cloisonnées par un filtre de requête (ADR-005) : un
ticket, une réclamation ou une demande de retour d'un autre client sont
introuvables, jamais refusées avec un code qui trahirait leur existence.
"""

from __future__ import annotations

from django.db.models import QuerySet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import GenericViewSet

from apps.support.models import Complaint, ReturnRequest, SupportTicket
from apps.support.serializers import (
    ComplaintSerializer,
    ComplaintWriteSerializer,
    MessageWriteSerializer,
    ReturnRequestSerializer,
    ReturnRequestWriteSerializer,
    SupportMessageSerializer,
    SupportTicketSerializer,
    TicketCreateSerializer,
)
from apps.support.services import SupportService
from common.permissions import IsCustomer, authenticated_user

__all__ = ["ComplaintViewSet", "ReturnRequestViewSet", "SupportTicketViewSet"]


class SupportTicketViewSet(
    ListModelMixin, CreateModelMixin, RetrieveModelMixin, GenericViewSet[SupportTicket]
):
    permission_classes = [IsCustomer]
    queryset = SupportTicket.objects.none()  # pour le générateur de schéma
    filterset_fields = {"status": ["exact"], "category": ["exact"]}

    def get_queryset(self) -> QuerySet[SupportTicket]:
        return SupportTicket.objects.filter(user=authenticated_user(self.request))

    def get_serializer_class(self) -> type[BaseSerializer[SupportTicket]]:
        return TicketCreateSerializer if self.action == "create" else SupportTicketSerializer

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = TicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ticket = SupportService.open_ticket(
            user=authenticated_user(request), **serializer.validated_data
        )
        return Response(SupportTicketSerializer(ticket).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def messages(self, request: Request, pk: str | None = None) -> Response:
        ticket = self.get_object()

        if request.method == "POST":
            write_serializer = MessageWriteSerializer(data=request.data)
            write_serializer.is_valid(raise_exception=True)
            message = SupportService.reply(
                ticket=ticket, author=authenticated_user(request), **write_serializer.validated_data
            )
            return Response(SupportMessageSerializer(message).data, status=status.HTTP_201_CREATED)

        assert self.paginator is not None
        page = self.paginator.paginate_queryset(
            ticket.messages.select_related("author"), request, view=self
        )
        read_serializer = SupportMessageSerializer(page, many=True)
        return self.get_paginated_response(read_serializer.data)


class ComplaintViewSet(
    ListModelMixin, CreateModelMixin, RetrieveModelMixin, GenericViewSet[Complaint]
):
    permission_classes = [IsCustomer]
    queryset = Complaint.objects.none()  # pour le générateur de schéma
    filterset_fields = {"status": ["exact"], "order": ["exact"]}

    def get_queryset(self) -> QuerySet[Complaint]:
        return Complaint.objects.filter(user=authenticated_user(self.request)).select_related(
            "order"
        )

    def get_serializer_class(self) -> type[BaseSerializer[Complaint]]:
        return ComplaintWriteSerializer if self.action == "create" else ComplaintSerializer

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = ComplaintWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        complaint = SupportService.file_complaint(
            user=authenticated_user(request), **serializer.validated_data
        )
        return Response(ComplaintSerializer(complaint).data, status=status.HTTP_201_CREATED)


class ReturnRequestViewSet(
    ListModelMixin, CreateModelMixin, RetrieveModelMixin, GenericViewSet[ReturnRequest]
):
    permission_classes = [IsCustomer]
    queryset = ReturnRequest.objects.none()  # pour le générateur de schéma
    filterset_fields = {"status": ["exact"], "order": ["exact"]}

    def get_queryset(self) -> QuerySet[ReturnRequest]:
        return ReturnRequest.objects.filter(user=authenticated_user(self.request)).select_related(
            "order"
        )

    def get_serializer_class(self) -> type[BaseSerializer[ReturnRequest]]:
        return ReturnRequestWriteSerializer if self.action == "create" else ReturnRequestSerializer

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = ReturnRequestWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        demande = SupportService.request_return(
            user=authenticated_user(request), **serializer.validated_data
        )
        return Response(ReturnRequestSerializer(demande).data, status=status.HTTP_201_CREATED)
