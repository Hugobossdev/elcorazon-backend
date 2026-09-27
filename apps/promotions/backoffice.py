"""Administration des codes promotionnels — invariant F4.

Cette application n'a **aucune surface publique** : un client ne liste pas les
codes, il en saisit un. Toutes les routes montées ici sont donc réservées au
personnel, et c'est pourquoi elles vivent à la racine de `/promotions/` plutôt
que sous un préfixe `manage/` — il n'y a rien dont les distinguer.

Le vrai sujet du module est ailleurs : les cinq conditions de F4 sont écrites
en base et posées par des contraintes `CHECK`. Une promotion incohérente — un
pourcentage à zéro sur un code « pourcentage », un montant absent sur un code
« montant fixe » — est refusée par PostgreSQL. Sans validation ici, ce refus
sortirait en 500 au lieu de 400, et l'exploitation verrait « erreur serveur »
là où il fallait lire « il manque le pourcentage ».
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.viewsets import GenericViewSet

from apps.promotions.models import Promotion
from apps.promotions.serializers import ManagedPromotionSerializer
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.permissions import HasReadWritePermission, authenticated_user

__all__ = ["ManagedPromotionViewSet"]

PROMOTION_PERMISSION = HasReadWritePermission.of(read="promotions.read", write="promotions.write")


class ManagedPromotionViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Promotion],
):
    """Codes promotionnels : création, conditions, suspension.

    **Les codes nationaux appartiennent au siège.** Un code sans établissement
    s'applique partout ; le confier à quelqu'un dont le périmètre est un seul
    restaurant lui donnerait le pouvoir de remiser les autres. Un compte
    cloisonné ne voit donc, et n'écrit donc, que les codes de ses
    établissements.

    **Les codes nominatifs ne se créent pas ici.** `owner` est en lecture
    seule : un code nominatif naît d'un échange de points de fidélité, qui l'a
    fait payer. Pouvoir en frapper un depuis cet écran reviendrait à distribuer
    des récompenses sans débit — exactement l'asymétrie que F1 et F2 ferment du
    côté des points.

    La suppression n'est pas exposée — d'où l'assemblage de mixins plutôt qu'un
    `ModelViewSet` amputé de sa route : `is_active` suspend, et les
    utilisations déjà consommées (`PromotionRedemption`) renvoient au code.
    L'effacer rendrait illisible la remise portée par une commande passée.
    """

    serializer_class = ManagedPromotionSerializer
    permission_classes = (PROMOTION_PERMISSION,)
    queryset = Promotion.objects.select_related("restaurant").order_by("-created_at")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "kind": ["exact"],
        "is_active": ["exact"],
        "restaurant__slug": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["code", "description"]
    ordering_fields: ClassVar[list[str]] = ["created_at", "starts_at", "ends_at", "used_count"]

    def get_queryset(self) -> QuerySet[Promotion]:
        user = authenticated_user(self.request)
        queryset = Promotion.objects.select_related("restaurant", "owner")
        if is_unscoped(user):
            return queryset
        return queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        self._assert_writable(serializer.validated_data.get("restaurant"))
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        # Seulement si la requête **change** l'établissement : un `PATCH` qui
        # n'y touche pas ne doit pas être relu comme une bascule vers le
        # national. Le code déjà hors périmètre, lui, est introuvable — le
        # filtre de `get_queryset` s'en est chargé avant d'arriver ici.
        if "restaurant" in serializer.validated_data:
            self._assert_writable(serializer.validated_data["restaurant"])
        serializer.save()

    def _assert_writable(self, restaurant: Any) -> None:
        """Un code national exige un compte non cloisonné.

        `assert_in_scope` ne peut rien dire d'un code sans établissement : il
        n'y a pas d'identifiant à comparer. Le cas est donc traité ici, et il
        est traité par un refus — accorder le national à qui n'a qu'un
        restaurant serait l'élargissement silencieux que l'ADR-005 cherche
        partout à empêcher.
        """
        user = authenticated_user(self.request)
        if restaurant is None:
            if not is_unscoped(user):
                raise PermissionDenied(
                    "Un code valable partout relève du siège : renseignez un établissement."
                )
            return
        assert_in_scope(user, restaurant.pk)
