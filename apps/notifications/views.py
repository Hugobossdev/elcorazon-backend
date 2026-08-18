"""Points d'entrée des notifications.

Lecture seule, plus un marquage : une notification est produite par le serveur,
jamais par le client. L'ADR-003 range explicitement ce domaine parmi ceux qui
n'ont pas de service — le ViewSet va à l'ORM.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.notifications.models import Notification
from apps.notifications.serializers import NotificationSerializer, UnreadCountSerializer
from common.permissions import authenticated_user

__all__ = ["NotificationViewSet"]


class NotificationViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet[Notification]):
    """Historique de l'utilisateur qui appelle."""

    serializer_class = NotificationSerializer
    queryset = Notification.objects.none()  # pour le générateur de schéma
    filterset_fields = {"kind": ["exact"]}

    def get_queryset(self) -> QuerySet[Notification]:
        # Le cloisonnement est un filtre de requête : la notification d'autrui
        # est introuvable, pas interdite.
        return Notification.objects.filter(user=authenticated_user(self.request))

    @extend_schema(responses={200: UnreadCountSerializer}, tags=["notifications"])
    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request: Request) -> Response:
        """Compteur de la pastille.

        Une route dédiée plutôt qu'un décompte côté client : la liste est
        paginée, et compter les non-lues d'une page donnerait un nombre faux
        dès la vingt-et-unième.
        """
        return Response({"unread": self.get_queryset().filter(read_at__isnull=True).count()})

    @extend_schema(request=None, responses={200: NotificationSerializer}, tags=["notifications"])
    @action(detail=True, methods=["post"], url_path="read")
    def mark_read(self, request: Request, pk: str) -> Response:
        """Marque une notification comme lue.

        Idempotent : relire n'écrase pas la date de première lecture. C'est
        elle qui a un sens — savoir *quand* l'utilisateur a vu passer
        l'information, pas quand il a rouvert l'écran.
        """
        notification = self.get_object()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at", "updated_at"])

        return Response(NotificationSerializer(notification).data)

    @extend_schema(request=None, responses={204: None}, tags=["notifications"])
    @action(detail=False, methods=["post"], url_path="read-all")
    def mark_all_read(self, request: Request) -> Response:
        self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)
