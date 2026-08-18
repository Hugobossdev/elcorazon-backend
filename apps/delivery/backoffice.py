"""Planning de la flotte — l'écran « horaires » du back-office.

Ce que ces routes font : dire **qui l'exploitation attend, et quand**. Elles ne
décident de rien d'autre.

L'éligibilité d'un livreur reste `CourierProfile.can_accept_orders` (L1) : en
ligne, dossier validé, compte actif. Un créneau ne s'y ajoute pas. Ce choix
mérite d'être écrit, parce que l'inverse semble naturel :

* un livreur présent, en ligne, à qui le serveur refuserait une course parce
  qu'il est 18 h 05 alors que son créneau finissait à 18 h, verrait un refus
  qu'aucun écran ne sait expliquer — et la commande resterait sans porteur ;
* la bascule « en ligne » est déjà une déclaration volontaire du livreur, qui
  sait mieux que le planning s'il roule à cet instant.

Le planning sert donc à organiser et à constater les écarts, pas à interdire.
Le jour où il devra l'être, ce sera une décision explicite, avec un terme ajouté
à `can_accept_orders` — le seul endroit où l'éligibilité se compose.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from rest_framework.viewsets import ModelViewSet

from apps.delivery.models import CourierShift
from apps.delivery.serializers import CourierShiftSerializer
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.permissions import HasReadWritePermission, authenticated_user

__all__ = ["CourierShiftViewSet"]

#: Le planning se lit avec la flotte et s'écrit avec elle.
FLEET_PERMISSION = HasReadWritePermission.of(read="couriers.read", write="couriers.write")


class CourierShiftViewSet(ModelViewSet[CourierShift]):
    """Créneaux planifiés — création, ajustement, retrait.

    La suppression **est** exposée ici, contrairement aux autres back-offices :
    un créneau n'est pas une pièce comptable, rien n'y renvoie, et une ligne
    saisie par erreur dans un planning doit pouvoir disparaître. Une absence
    ponctuelle, elle, se marque avec `is_available` — elle se lit dans le
    planning au lieu d'en être absente.

    Le cloisonnement suit celui de la flotte : un compte rattaché à un
    établissement ne voit et n'écrit que le planning de ses livreurs.
    """

    serializer_class = CourierShiftSerializer
    permission_classes = (FLEET_PERMISSION,)
    queryset = CourierShift.objects.select_related("courier__user")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "courier": ["exact"],
        "day_of_week": ["exact"],
        "is_available": ["exact"],
    }

    def get_queryset(self) -> QuerySet[CourierShift]:
        user = authenticated_user(self.request)
        base = CourierShift.objects.select_related("courier__user", "courier__restaurant")
        if is_unscoped(user):
            return base
        return base.filter(courier__restaurant_id__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        # Le livreur arrive du corps de la requête : il n'y a pas encore d'objet
        # à filtrer, et sans ce contrôle on planifierait le livreur d'une autre
        # enseigne.
        assert_in_scope(
            authenticated_user(self.request),
            serializer.validated_data["courier"].restaurant_id,
        )
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        courier = serializer.validated_data.get("courier")
        if courier is not None:
            assert_in_scope(authenticated_user(self.request), courier.restaurant_id)
        serializer.save()
