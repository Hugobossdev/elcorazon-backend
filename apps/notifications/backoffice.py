"""Campagnes de notifications — l'écran « push ciblés » du back-office.

Deux temps, comme pour les codes promotionnels : on **rédige**, puis on
**envoie**. Ce n'est pas une lourdeur d'interface, c'est la seule protection
possible contre la faute de frappe dans un message qui part à plusieurs
milliers de personnes — un envoi de masse ne se rappelle pas.

Une campagne envoyée devient **immuable**. La modifier après coup ferait mentir
la trace : l'historique afficherait un texte que personne n'a reçu, et la
question « qu'a-t-on envoyé le 3 mars ? » n'aurait plus de réponse.

Une campagne est un objet **d'enseigne** : ses segments sont ceux d'`accounts`
et d'`orders` — « les clients actifs », « ceux qui ne commandent plus » — et
aucun ne s'arrête à une ville. L'envoyer touche donc tout le monde, dans tous
les pays.

C'est pourquoi la **rédaction et l'envoi relèvent du siège** (`assert_unscoped`),
comme un code promotionnel valable partout (`promotions/backoffice.py`) ou un
pays. `notifications.send` disait seulement « a le droit d'envoyer une
campagne » ; il ne pouvait rien dire de *à qui*, et un gérant de Lomé poussait
un message à la clientèle d'Abidjan sans qu'aucune garde ne s'y oppose — le
contraire exact de ce que l'ADR-005 tient partout ailleurs.

La **lecture** reste ouverte à qui détient la permission : un texte de campagne
n'est pas une donnée d'exploitation, et son bilan, lui, est cloisonné
(`campaign_stats`). Ce que ce refus ferme, une campagne *par périmètre* le
rouvrirait — elle demande de décider ce qu'est « la clientèle d'une cuisine »,
décision métier qui n'est pas prise ici.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import transaction
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
from apps.notifications.serializers import (
    CampaignScheduleSerializer,
    CampaignSerializer,
    CampaignStatsSerializer,
)
from apps.notifications.services import (
    campaign_stats,
    recipients_of,
    schedule_campaign,
    send_campaign,
    unschedule_campaign,
)
from common.permissions import HasPermission, assert_unscoped, authenticated_user

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
        acteur = authenticated_user(self.request)
        assert_unscoped(acteur, "Une campagne, qui vise la clientèle de l'enseigne,")
        serializer.save(created_by=acteur)

    @transaction.atomic
    def perform_update(self, serializer: Any) -> None:
        assert_unscoped(
            authenticated_user(self.request), "Une campagne, qui vise la clientèle de l'enseigne,"
        )
        # Relue sous verrou : l'instance de la vue date d'avant l'envoi que le
        # battement faisait peut-être au même instant, et le texte d'une
        # campagne partie aurait été réécrit après coup.
        serializer.instance = Campaign.objects.select_for_update().get(pk=serializer.instance.pk)
        if serializer.instance.status == CampaignStatus.SENT:
            raise PermissionDenied(
                "Une campagne envoyée ne se modifie plus : l'historique afficherait "
                "un texte que personne n'a reçu."
            )
        # Programmée, elle ne se modifie pas davantage : le texte relu au
        # moment de dater est celui qui partira. On annule la programmation
        # pour le reprendre, ce qui est un geste visible.
        if serializer.instance.status == CampaignStatus.SCHEDULED:
            raise PermissionDenied(
                "Cette campagne est programmée : annulez la programmation pour "
                "la modifier, sinon le texte qui part ne serait plus celui qu'on a relu."
            )
        serializer.save()

    @extend_schema(responses={200: CampaignSerializer}, tags=["notifications"])
    @action(detail=True, methods=["post"], permission_classes=[SEND_PERMISSION])
    def send(self, request: Request, pk: str) -> Response:
        """Envoie la campagne, une seule fois.

        Le rejeu est absorbé plutôt que refusé : un double clic renvoie la
        campagne telle qu'elle est partie, avec son horodatage et son compte,
        au lieu d'une erreur qui ferait croire à un échec.

        Réservé au siège : le segment visé ne connaît pas les frontières d'un
        périmètre, et un envoi ne se rappelle pas.
        """
        assert_unscoped(
            authenticated_user(request), "L'envoi d'une campagne à la clientèle de l'enseigne"
        )
        return Response(CampaignSerializer(send_campaign(self.get_object())).data)

    @extend_schema(
        request=CampaignScheduleSerializer,
        responses={200: CampaignSerializer},
        tags=["notifications"],
    )
    @action(detail=True, methods=["post"], permission_classes=[SEND_PERMISSION])
    def schedule(self, request: Request, pk: str) -> Response:
        """Date l'envoi : la campagne partira seule, à l'heure dite.

        Une campagne se prépare la veille et part quand les gens ont leur
        téléphone en main. C'est le battement qui l'envoie (`celery beat`,
        toutes les cinq minutes) : **sans lui, une campagne programmée reste
        programmée** — dépendance dite au guide de déploiement.

        Réservé au siège, comme l'envoi immédiat : programmer, c'est envoyer,
        avec un délai.
        """
        assert_unscoped(authenticated_user(request), "La programmation d'une campagne à l'enseigne")
        serializer = CampaignScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        campagne = schedule_campaign(
            self.get_object(), quand=serializer.validated_data["scheduled_at"]
        )
        return Response(CampaignSerializer(campagne).data)

    @extend_schema(request=None, responses={200: CampaignSerializer}, tags=["notifications"])
    @action(
        detail=True,
        methods=["post"],
        url_path="unschedule",
        url_name="unschedule",
        permission_classes=[SEND_PERMISSION],
    )
    def unschedule(self, request: Request, pk: str) -> Response:
        """Annule la programmation — la campagne redevient un brouillon."""
        assert_unscoped(authenticated_user(request), "La programmation d'une campagne à l'enseigne")
        return Response(CampaignSerializer(unschedule_campaign(self.get_object())).data)

    @extend_schema(responses={200: CampaignStatsSerializer}, tags=["notifications"])
    @action(detail=True, methods=["get"], permission_classes=[SEND_PERMISSION])
    def stats(self, request: Request, pk: str) -> Response:
        """Le bilan d'une campagne : ouvertures, commandes, chiffre attribué.

        Rendu aussi pour un brouillon, à zéro : l'écran n'a pas à distinguer
        « rien envoyé » de « route absente ».
        """
        bilan = campaign_stats(self.get_object(), viewer=authenticated_user(request))
        return Response(CampaignStatsSerializer(bilan).data)

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

        Réservé au siège, comme l'envoi qu'il estime : le compte porte sur la
        clientèle de l'enseigne entière.
        """
        assert_unscoped(authenticated_user(request), "L'estimation de la clientèle de l'enseigne")
        return Response({"recipients": recipients_of(self.get_object()).count()})
