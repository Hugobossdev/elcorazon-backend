"""Points d'entrée de la livraison.

Deux publics, deux racines :

* `/delivery/me/` et `/delivery/assignments/` — le **livreur**, sur son propre
  dossier et ses propres courses ;
* `/delivery/couriers/` — le **personnel**, sous permissions nommées, et
  restreint aux établissements auxquels il est rattaché.

Aucune route n'est ouverte sans jeton : rien ici n'est public.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from apps.delivery.models import Assignment, CourierProfile, CourierRating
from apps.delivery.serializers import (
    AssignmentSerializer,
    CourierProfileSerializer,
    CourierProvisioningSerializer,
    CourierRatingSerializer,
    CourierRatingWriteSerializer,
    DeclineSerializer,
    DeliveryTransitionSerializer,
    DocumentsSerializer,
    OfferSerializer,
    OnlineSerializer,
    VerificationSerializer,
)
from apps.delivery.services import (
    AssignmentService,
    CourierApplication,
    CourierRatingService,
    CourierService,
)
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.exceptions import BusinessRuleViolation
from common.permissions import (
    HasPermission,
    HasReadWritePermission,
    IsCourier,
    IsCustomer,
    authenticated_user,
)

#: `order_id` n'est pas un champ de `CourierProfile` : le générateur ne peut
#: pas en déduire le type, et le déclarer vaut mieux qu'un `string` par défaut.
ORDER_ID = OpenApiParameter(
    name="order_id",
    location=OpenApiParameter.PATH,
    type=OpenApiTypes.UUID,
    description="Commande pour laquelle chercher un livreur.",
)

__all__ = [
    "AssignmentViewSet",
    "CancelAssignmentView",
    "CourierOnlineView",
    "CourierProfileView",
    "OfferAssignmentView",
    "StaffCourierViewSet",
]


def courier_of(request: Request) -> CourierProfile:
    """Dossier du livreur qui appelle, ou 404.

    Un compte de type livreur sans dossier est une anomalie de création de
    compte ; le 404 la rend visible sans exposer autre chose.
    """
    return get_object_or_404(
        CourierProfile.objects.select_related("user", "restaurant"),
        user=authenticated_user(request),
    )


class CourierProfileView(APIView):
    """`/delivery/me/` — le dossier du livreur, par lui-même."""

    permission_classes = [IsCourier]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(responses={200: CourierProfileSerializer}, tags=["delivery"])
    def get(self, request: Request) -> Response:
        return Response(CourierProfileSerializer(courier_of(request)).data)

    @extend_schema(
        request=DocumentsSerializer, responses={200: CourierProfileSerializer}, tags=["delivery"]
    )
    def post(self, request: Request) -> Response:
        """Dépôt de pièces — repasse le dossier en attente (L5)."""
        serializer = DocumentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        courier = CourierService.replace_documents(
            courier=courier_of(request), **serializer.validated_data
        )
        return Response(CourierProfileSerializer(courier).data)


class CourierOnlineView(APIView):
    """`/delivery/me/online/` — la bascule de disponibilité."""

    permission_classes = [IsCourier]

    @extend_schema(
        request=OnlineSerializer, responses={200: CourierProfileSerializer}, tags=["delivery"]
    )
    def post(self, request: Request) -> Response:
        serializer = OnlineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        courier = CourierService.set_online(
            courier=courier_of(request), is_online=serializer.validated_data["is_online"]
        )
        return Response(CourierProfileSerializer(courier).data)


class AssignmentViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet[Assignment]):
    """Courses du livreur qui appelle.

    Le filtre est sur le dossier, pas sur une permission d'objet : la course
    d'un collègue est introuvable plutôt qu'interdite.
    """

    permission_classes = [IsCourier]
    serializer_class = AssignmentSerializer
    queryset = Assignment.objects.none()  # pour le générateur de schéma
    filterset_fields = {"status": ["exact"], "order": ["exact"]}

    def get_queryset(self) -> QuerySet[Assignment]:
        return (
            Assignment.objects.filter(courier__user=authenticated_user(self.request))
            .select_related("order__restaurant", "courier__user")
            .order_by("-offered_at")
        )

    @extend_schema(request=None, responses={200: AssignmentSerializer}, tags=["delivery"])
    @action(detail=True, methods=["post"])
    def accept(self, request: Request, pk: str) -> Response:
        """L2 — acceptation exclusive : deux livreurs ne peuvent pas prendre la
        même course, et le perdant reçoit un refus métier, pas une 500."""
        assignment = AssignmentService.accept(
            assignment=self.get_object(), courier=courier_of(request)
        )
        return Response(AssignmentSerializer(assignment).data)

    @extend_schema(
        request=DeclineSerializer, responses={200: AssignmentSerializer}, tags=["delivery"]
    )
    @action(detail=True, methods=["post"])
    def decline(self, request: Request, pk: str) -> Response:
        serializer = DeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignment = AssignmentService.decline(
            assignment=self.get_object(),
            courier=courier_of(request),
            reason=serializer.validated_data["reason"],
        )
        return Response(AssignmentSerializer(assignment).data)

    @extend_schema(
        request=DeliveryTransitionSerializer,
        responses={200: AssignmentSerializer},
        tags=["delivery"],
    )
    @action(detail=True, methods=["post"], url_path="status", url_name="status")
    def update_status(self, request: Request, pk: str) -> Response:
        """Progression de la course : récupérée, en route, livrée.

        La commande suit par projection déclarée, jamais par une écriture faite
        ici — c'est une projection à la main qui avait produit C4.
        """
        serializer = DeliveryTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignment = AssignmentService.transition_to(
            assignment=self.get_object(),
            target=serializer.validated_data["status"],
            actor=authenticated_user(request),
            reason=serializer.validated_data["reason"],
        )
        return Response(AssignmentSerializer(assignment).data)


class StaffCourierViewSet(CreateModelMixin, ReadOnlyModelViewSet[CourierProfile]):
    """Flotte, vue et **ouverte** par le personnel de ses établissements.

    Pas de suppression, et pas de modification du dossier par le personnel non
    plus : ce qu'un livreur a livré, encaissé et signé y renvoie. Le retirer du
    service se fait par la suspension (`verification/`), qui laisse le dossier
    lisible.
    """

    permission_classes = [HasReadWritePermission.of(read="couriers.read", write="couriers.write")]
    serializer_class = CourierProfileSerializer
    queryset = CourierProfile.objects.none()  # pour le générateur de schéma
    filterset_fields = {
        "verification_status": ["exact"],
        "is_online": ["exact"],
        "restaurant__slug": ["exact"],
    }

    def get_queryset(self) -> QuerySet[CourierProfile]:
        user = authenticated_user(self.request)
        queryset = CourierProfile.objects.select_related("user", "restaurant").order_by(
            "user__full_name"
        )
        if is_unscoped(user):
            return queryset
        return queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action == "create":
            return CourierProvisioningSerializer
        return CourierProfileSerializer

    @extend_schema(
        request=CourierProvisioningSerializer,
        responses={201: CourierProfileSerializer},
        tags=["delivery"],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ouvre un compte livreur rattaché à l'un de ses établissements.

        Un livreur ne s'inscrit pas : on l'embauche. Le compte naît avec un
        dossier **en attente** — c'est le livreur qui déposera ses pièces, et
        `verification/` qui les instruira ensuite.

        `assert_in_scope` et non le filtre de `get_queryset` : l'objet n'existe
        pas encore, il n'y a donc rien à filtrer, et l'établissement arrive du
        corps de la requête. Sans cette garde, un gérant de Kara embaucherait
        pour Lomé — et se donnerait au passage un livreur qu'il ne pourrait plus
        relire.
        """
        serializer = CourierProvisioningSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assert_in_scope(authenticated_user(request), serializer.validated_data["restaurant"].pk)

        courier = CourierService.provision(
            application=CourierApplication(**serializer.validated_data)
        )
        return Response(CourierProfileSerializer(courier).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=VerificationSerializer,
        responses={200: CourierProfileSerializer},
        tags=["delivery"],
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            HasPermission.of("couriers.approve") | HasPermission.of("couriers.suspend")
        ],
    )
    def verification(self, request: Request, pk: str) -> Response:
        """Valide, rejette ou suspend un dossier.

        **Deux permissions, pas une.** `couriers.approve` instruit le dossier —
        valider les pièces, rejeter un permis illisible, remettre en attente ;
        `couriers.suspend` retire du service quelqu'un qui travaillait. Les
        deux gestes n'ont ni la même urgence ni le même auteur : l'instruction
        se fait au calme, la suspension se décide un samedi soir après un
        incident, et les confondre reviendrait à donner le second pouvoir à
        toute personne chargée du premier.

        La route accepte l'une **ou** l'autre — sinon un compte n'ayant que
        `couriers.suspend` ne l'atteindrait pas — et c'est le statut demandé
        qui départage.
        """
        serializer = VerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        cible = serializer.validated_data["status"]
        requise = (
            "couriers.suspend" if cible == VerificationStatus.SUSPENDED else "couriers.approve"
        )
        if not authenticated_user(request).has_permission(requise):
            raise PermissionDenied(f"Ce geste demande la permission « {requise} ».")

        courier = CourierService.review(
            courier=self.get_object(),
            target=serializer.validated_data["status"],
            actor=authenticated_user(request),
            notes=serializer.validated_data["notes"],
        )
        return Response(CourierProfileSerializer(courier).data)

    @extend_schema(
        responses={200: CourierProfileSerializer(many=True)},
        parameters=[ORDER_ID],
        tags=["delivery"],
    )
    @action(detail=False, methods=["get"], url_path=r"available/(?P<order_id>[^/.]+)")
    def available(self, request: Request, order_id: str) -> Response:
        """Livreurs éligibles pour une commande, du plus proche au plus loin."""
        order = get_object_or_404(self._orders_in_scope(), pk=order_id)
        return Response(
            CourierProfileSerializer(CourierService.available_for(order), many=True).data
        )

    def _orders_in_scope(self) -> QuerySet[Order]:
        user = authenticated_user(self.request)
        if is_unscoped(user):
            return Order.objects.all()
        return Order.objects.filter(restaurant_id__in=staff_restaurant_ids(user))


class OfferAssignmentView(APIView):
    """`POST /delivery/orders/{order}/offer/` — propose une course à un livreur."""

    permission_classes = [HasPermission.of("orders.assign_courier")]

    @extend_schema(
        request=OfferSerializer, responses={201: AssignmentSerializer}, tags=["delivery"]
    )
    def post(self, request: Request, order_id: str) -> Response:
        actor = authenticated_user(request)

        scope = Order.objects.all()
        if not is_unscoped(actor):
            scope = scope.filter(restaurant_id__in=staff_restaurant_ids(actor))
        order = get_object_or_404(scope, pk=order_id)

        serializer = OfferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignment = AssignmentService.offer(
            order=order, courier=serializer.validated_data["courier"], actor=actor
        )
        return Response(AssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)


class CancelAssignmentView(APIView):
    """`POST /delivery/assignments/{id}/cancel/` — annulation par le personnel.

    Distincte du refus par le livreur : celui-ci décline une proposition, le
    personnel annule une course déjà engagée. Les deux libèrent la commande
    pour une nouvelle affectation, mais seule l'annulation incrémente le
    compteur d'annulations du livreur.
    """

    permission_classes = [HasPermission.of("orders.assign_courier")]

    @extend_schema(
        request=DeclineSerializer, responses={200: AssignmentSerializer}, tags=["delivery"]
    )
    def post(self, request: Request, assignment_id: str) -> Response:
        actor = authenticated_user(request)

        scope = Assignment.objects.select_related("order")
        if not is_unscoped(actor):
            scope = scope.filter(order__restaurant_id__in=staff_restaurant_ids(actor))
        assignment = get_object_or_404(scope, pk=assignment_id)

        serializer = DeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not assignment.is_active:
            raise BusinessRuleViolation(
                "Cette course est déjà terminée.", current_status=assignment.status
            )

        cancelled = AssignmentService.transition_to(
            assignment=assignment,
            target=DeliveryStatus.CANCELLED,
            actor=actor,
            reason=serializer.validated_data["reason"],
        )
        return Response(AssignmentSerializer(cancelled).data)


class OrderRatingView(APIView):
    """`GET|POST /delivery/orders/{order}/rating/` — la note du client sur sa livraison.

    Une seule route pour les deux gestes parce que l'écran pose toujours les
    deux questions à la suite : « ai-je déjà noté ? », puis « voici ma note ».
    Le 404 du GET est la réponse à la première, et non une erreur à traiter.

    La commande est cherchée **dans les commandes de l'appelant** : il n'y a
    donc aucune vérification de propriété à écrire ensuite, et aucune à
    oublier. Noter la livraison d'autrui rend un 404, pas un 403 — l'existence
    de la commande d'un tiers ne se déduit pas d'ici.
    """

    permission_classes = [IsCustomer]

    def _assignment(self, request: Request, order_id: str) -> Assignment:
        order = get_object_or_404(
            Order.objects.filter(customer=authenticated_user(request)), pk=order_id
        )
        return get_object_or_404(
            Assignment.objects.select_related("courier__user"),
            order=order,
            status=DeliveryStatus.DELIVERED,
        )

    @extend_schema(responses={200: CourierRatingSerializer}, tags=["delivery"])
    def get(self, request: Request, order_id: str) -> Response:
        assignment = self._assignment(request, order_id)
        rating = get_object_or_404(CourierRating, assignment=assignment)
        return Response(CourierRatingSerializer(rating).data)

    @extend_schema(
        request=CourierRatingWriteSerializer,
        responses={201: CourierRatingSerializer},
        tags=["delivery"],
    )
    def post(self, request: Request, order_id: str) -> Response:
        assignment = self._assignment(request, order_id)

        payload = CourierRatingWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        rating = CourierRatingService.rate(
            assignment=assignment,
            customer=authenticated_user(request),
            score=payload.validated_data["score"],
            comment=payload.validated_data.get("comment", ""),
        )
        return Response(CourierRatingSerializer(rating).data, status=status.HTTP_201_CREATED)
