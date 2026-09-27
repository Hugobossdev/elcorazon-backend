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

from django.db.models import Count, QuerySet, Sum
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet

from apps.delivery.models import Assignment, CourierShift
from apps.delivery.serializers import AssignmentSerializer, CourierShiftSerializer
from apps.delivery.states import DeliveryStatus
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.permissions import HasPermission, HasReadWritePermission, authenticated_user

__all__ = ["CourierShiftViewSet", "ManagedAssignmentViewSet"]

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


class ManagedAssignmentViewSet(ReadOnlyModelViewSet[Assignment]):
    """Courses vues par l'exploitation — **qui porte quoi**.

    Elle manquait, et son absence se voyait à l'écran. `AssignmentSerializer`
    annonce depuis le début une course « vue par le livreur ou par le
    personnel », mais aucune route ne la rendait au personnel :
    `AssignmentViewSet` est sous `IsCourier` et filtre sur le dossier de
    l'appelant. Le back-office n'avait donc aucun moyen d'apprendre qui
    transporte une commande — son écran « Livraisons actives » affichait une
    liste de commandes sans porteur, et le détail d'une commande une ligne
    « Livreur » vide.

    Pourquoi ici plutôt que sur la commande : `orders` ne peut pas importer
    `delivery` (ADR-002, graphe vérifié par
    `tests/architecture/test_dependency_graph.py`). La flèche va dans l'autre
    sens, et c'est le bon sens — la livraison connaît la commande, la commande
    ignore qu'on la livre. Ajouter le livreur à `OrderSerializer` aurait
    inversé cette flèche, et au passage exposé le porteur au client sur la même
    route.

    Lecture seule, et sous `orders.read` : savoir qui porte une commande fait
    partie de la superviser. Affecter reste `orders.assign_courier`
    (`OfferAssignmentView`), annuler une course aussi.

    Le cloisonnement suit celui des commandes — une course hors périmètre est
    **introuvable**, comme la commande qu'elle porte.
    """

    serializer_class = AssignmentSerializer
    permission_classes = (HasPermission.of("orders.read"),)

    # Déclaré pour le générateur de schéma seulement — voir `get_queryset`.
    queryset = Assignment.objects.none()

    #: `order` en premier : c'est la question que pose l'écran de supervision,
    #: « qui porte cette commande ». `status` sert la liste des livraisons en
    #: cours, `courier` l'historique d'un livreur.
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "order": ["exact"],
        "courier": ["exact"],
        "status": ["exact"],
        # L'historique d'un livreur se lit sur une période. Sans ces bornes, le
        # back-office chargeait toutes ses courses — puis croisait le résultat
        # avec une année de commandes téléchargées — pour n'en montrer qu'un
        # mois.
        "delivered_at": ["gte", "lte"],
        "offered_at": ["gte", "lte"],
    }

    @extend_schema(
        responses={200: OpenApiTypes.OBJECT},
        tags=["delivery"],
        description=(
            "Totaux de la sélection de courses : compte par statut et **gains "
            "par devise** du livreur. Mêmes filtres que la liste."
        ),
    )
    @action(detail=False, methods=["get"], url_path="summary", url_name="summary")
    def summary(self, request: Request) -> Response:
        """Ce que l'historique d'un livreur additionnait à l'écran, et mal.

        Il affichait un « Revenu » qui était la somme des **totaux des
        commandes** — ce que les clients ont payé, pas ce que le livreur a
        gagné — et mêlait les devises. Les gains d'un livreur sont
        `courier_fee`, dans la devise de la commande.
        """
        selection = self.filter_queryset(self.get_queryset()).order_by()
        gains = [
            {
                "currency": ligne["order__total_currency"],
                "deliveries": ligne["nombre"],
                "earnings_minor": ligne["gains"] or 0,
            }
            for ligne in selection.filter(status=DeliveryStatus.DELIVERED)
            .values("order__total_currency")
            .annotate(nombre=Count("id"), gains=Sum("courier_fee_minor"))
            .order_by("-gains", "order__total_currency")
        ]
        comptes = dict.fromkeys(DeliveryStatus.values, 0)
        comptes.update(
            {
                ligne["status"]: ligne["nombre"]
                for ligne in selection.values("status").annotate(nombre=Count("id"))
            }
        )
        return Response(
            {
                "assignments": sum(comptes.values()),
                "by_status": comptes,
                "earnings": gains,
            }
        )

    def get_queryset(self) -> QuerySet[Assignment]:
        user = authenticated_user(self.request)
        base = (
            Assignment.objects.select_related("courier__user", "order__restaurant", "order__city")
            .prefetch_related("order__lines")
            .order_by("-offered_at")
        )
        if is_unscoped(user):
            return base
        return base.filter(order__restaurant_id__in=staff_restaurant_ids(user))
