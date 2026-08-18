"""Campagnes de notifications — l'écran « push ciblés » du back-office.

Deux temps, comme pour les codes promotionnels : on **rédige**, puis on
**envoie**. Ce n'est pas une lourdeur d'interface, c'est la seule protection
possible contre la faute de frappe dans un message qui part à plusieurs
milliers de personnes — un envoi de masse ne se rappelle pas.

Une campagne envoyée devient **immuable**. La modifier après coup ferait mentir
la trace : l'historique afficherait un texte que personne n'a reçu, et la
question « qu'a-t-on envoyé le 3 mars ? » n'aurait plus de réponse.

Aucun cloisonnement par établissement : `notifications` ne connaît ni
`restaurants` ni `geography` (ADR-002), et les segments qu'elle sait viser sont
ceux de `accounts` et `orders`. Une campagne est donc un objet d'enseigne, et
`notifications.send` est la clé qui l'ouvre.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.notifications.models import Campaign, CampaignStatus
from apps.notifications.serializers import CampaignSerializer
from apps.notifications.services import recipients_of, send_campaign
from common.permissions import HasPermission, authenticated_user

__all__ = ["CampaignViewSet"]

SEND_PERMISSION = HasPermission.of("notifications.send")


class CampaignViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Campaign],
):
    """Rédaction, estimation et envoi d'une campagne."""

    serializer_class = CampaignSerializer
    permission_classes = (SEND_PERMISSION,)
    queryset = Campaign.objects.select_related("created_by").order_by("-created_at")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact"],
        "audience": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["title", "body"]

    def perform_create(self, serializer: Any) -> None:
        # L'auteur vient du jeton et n'est pas un champ d'entrée : une trace
        # qu'on peut renseigner soi-même ne trace rien.
        serializer.save(created_by=authenticated_user(self.request))

    def perform_update(self, serializer: Any) -> None:
        if serializer.instance.status == CampaignStatus.SENT:
            raise PermissionDenied(
                "Une campagne envoyée ne se modifie plus : l'historique afficherait "
                "un texte que personne n'a reçu."
            )
        serializer.save()

    @extend_schema(responses={200: CampaignSerializer}, tags=["notifications"])
    @action(detail=True, methods=["post"], permission_classes=[SEND_PERMISSION])
    def send(self, request: Request, pk: str) -> Response:
        """Envoie la campagne, une seule fois.

        Le rejeu est absorbé plutôt que refusé : un double clic renvoie la
        campagne telle qu'elle est partie, avec son horodatage et son compte,
        au lieu d'une erreur qui ferait croire à un échec.
        """
        return Response(CampaignSerializer(send_campaign(self.get_object())).data)

    @extend_schema(
        responses={200: {"type": "object", "properties": {"recipients": {"type": "integer"}}}},
        tags=["notifications"],
    )
    @action(detail=True, methods=["get"], permission_classes=[SEND_PERMISSION])
    def audience(self, request: Request, pk: str) -> Response:
        """Combien de personnes cette campagne viserait, si on l'envoyait.

        Le chiffre est un **majorant** : il compte le segment, pas les envois
        aboutis, puisque le consentement au marketing ne se vérifie qu'à
        l'écriture de chaque notification. L'annoncer autrement ferait passer
        un refus de consentement pour une erreur d'envoi.
        """
        return Response({"recipients": recipients_of(self.get_object()).count()})
