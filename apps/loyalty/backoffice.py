"""Catalogue des récompenses — l'écran « fidélité » du back-office.

Ce qu'on édite ici, c'est **ce qui s'achète en points** : un plat offert, une
livraison gratuite, une remise. Distinct des promotions, qui remisent une
commande sur présentation d'un code : une récompense se paie, avec des points
qu'un client a accumulés en commandant.

Ce que ces routes ne font **pas**, et ne feront pas :

* **créditer des points.** Aucune route ne touche `PointsAccount` : les points
  s'acquièrent à la livraison, par signal (F1), et se dépensent à l'échange
  (F2). Une écriture manuelle depuis un écran d'administration frapperait
  monnaie, et le journal des points ne dirait plus d'où vient un solde ;
* **échanger à la place d'un client.** L'échange débite le compte du porteur du
  jeton, et c'est la seule façon de garantir la symétrie débit/récompense.

La suppression n'est pas exposée : `RewardRedemption` référence la récompense en
`PROTECT`, et un échange passé doit rester lisible — « 500 points contre quoi ? »
est une question qu'un client repose des mois plus tard. `is_active` la retire
du catalogue sans réécrire le passé.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import Q, QuerySet
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.viewsets import GenericViewSet

from apps.loyalty.models import Reward
from apps.loyalty.serializers import ManagedRewardSerializer
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.permissions import HasReadWritePermission, authenticated_user

__all__ = ["ManagedRewardViewSet"]

LOYALTY_PERMISSION = HasReadWritePermission.of(read="loyalty.read", write="loyalty.write")


class ManagedRewardViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Reward],
):
    """Récompenses échangeables contre des points.

    **Les récompenses nationales appartiennent au siège**, comme les codes
    promotionnels : une récompense sans établissement s'échange partout, et la
    confier à quelqu'un dont le périmètre est un seul restaurant lui donnerait le
    pouvoir d'engager les autres. Un compte cloisonné ne voit donc, et n'écrit
    donc, que celles de ses établissements — plus les nationales, qu'il consulte
    sans pouvoir les modifier.
    """

    serializer_class = ManagedRewardSerializer
    permission_classes = (LOYALTY_PERMISSION,)
    queryset = Reward.objects.select_related("restaurant").order_by("points_cost")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "kind": ["exact"],
        "is_active": ["exact"],
        "restaurant": ["exact", "isnull"],
    }
    search_fields: ClassVar[list[str]] = ["name", "description"]

    def get_queryset(self) -> QuerySet[Reward]:
        user = authenticated_user(self.request)
        base = Reward.objects.select_related("restaurant").order_by("points_cost")
        if is_unscoped(user):
            return base
        # Les nationales restent visibles : les cacher laisserait croire qu'un
        # client de cet établissement ne peut rien échanger d'autre. En `Q`
        # explicite et non par un `None` glissé dans le `__in` : SQL ne fait pas
        # correspondre `NULL` à une liste, et le filtre les aurait perdues sans
        # rien signaler.
        return base.filter(
            Q(restaurant_id__in=staff_restaurant_ids(user)) | Q(restaurant__isnull=True)
        )

    def perform_create(self, serializer: Any) -> None:
        self._assert_writable(serializer.validated_data.get("restaurant"))
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        self._assert_writable(serializer.instance.restaurant)
        if "restaurant" in serializer.validated_data:
            self._assert_writable(serializer.validated_data["restaurant"])
        serializer.save()

    def _assert_writable(self, restaurant: Any) -> None:
        user = authenticated_user(self.request)
        if is_unscoped(user):
            return
        if restaurant is None:
            # `assert_in_scope` ne peut pas trancher : il n'y a pas
            # d'établissement à comparer, et c'est précisément le cas dangereux.
            raise PermissionDenied(
                "Une récompense nationale engage tous les établissements : elle relève du siège."
            )
        assert_in_scope(user, restaurant.pk)
