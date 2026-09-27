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

from django.db import transaction
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from apps.accounts.models import VerificationPurpose
from apps.accounts.services import VerificationService
from apps.accounts.throttling import AuthCodeIssueThrottle, AuthIPThrottle
from apps.delivery.models import Assignment, CourierProfile, CourierRating
from apps.delivery.serializers import (
    AssignmentSerializer,
    CourierApplicationAcceptedSerializer,
    CourierProfileSerializer,
    CourierProvisioningSerializer,
    CourierRatingSerializer,
    CourierRatingWriteSerializer,
    CourierSelfApplicationSerializer,
    CourierSelfUpdateSerializer,
    CourierUpdateSerializer,
    CourierZonesSerializer,
    DeclineSerializer,
    DeliveryTransitionSerializer,
    DocumentsSerializer,
    EarningsSerializer,
    OfferSerializer,
    OnlineSerializer,
    ProofOfDeliverySerializer,
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
    "CourierApplicationView",
    "CourierOnlineView",
    "CourierProfileView",
    "OfferAssignmentView",
    "StaffCourierViewSet",
]


class CourierApplicationView(APIView):
    """`/delivery/apply/` — candidature spontanée d'un livreur.

    ## Ce que cette route change, et ce qu'elle ne change pas

    Elle ouvre une **seconde porte** vers `CourierService.provision`, à côté de
    l'embauche par le back-office (`StaffCourierViewSet.create`). Les deux
    créent exactement le même objet : un compte de type livreur et un dossier
    **en attente**, jamais autre chose. Ce qui distingue un candidat d'un
    embauché n'est donc pas son dossier — il est identique — mais qui a rempli
    le formulaire.

    L'invariant L1 est intact, et c'est le point : `can_accept_orders` exige
    `verification_status == approved`, que seule `verification/` peut poser,
    sous `couriers.approve`. Un candidat ne se valide pas lui-même ; il se met
    dans la file de ceux dont on lira les pièces.

    ## Pourquoi aucun jeton n'en sort

    L'inscription client (`/auth/register/`) rend un couple de jetons dans la
    foulée, et c'est cohérent : rien de ce qu'un client peut faire ensuite ne
    dépend d'une adresse vérifiée. Ici, rendre des jetons ferait de l'écran de
    saisie du code une étape que le client mobile pourrait sauter — il aurait
    déjà tout ce qu'il faut pour appeler l'API. La session s'obtient donc
    **uniquement** par `POST /auth/verify/`, code en main.

    Ouverte sans jeton, et donc limitée en débit comme les routes
    d'authentification : elle crée un compte et expédie un courriel, les deux
    choses qu'on ne veut pas voir répétées en boucle depuis une même origine.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthCodeIssueThrottle]

    @extend_schema(
        request=CourierSelfApplicationSerializer,
        responses={201: CourierApplicationAcceptedSerializer},
        tags=["delivery"],
    )
    def post(self, request: Request) -> Response:
        serializer = CourierSelfApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Les deux services sont atomiques séparément ; les réunir sous une
        # transaction commune les rend indissociables. Sans elle, une panne à
        # l'émission du code laisserait un compte créé et muet — et la seconde
        # tentative du candidat se heurterait à « cette adresse est déjà
        # prise », sur un compte dont il n'a jamais reçu le code. Le courriel,
        # lui, part sur `on_commit` : il n'entre pas dans la transaction.
        with transaction.atomic():
            # Pas d'`assert_in_scope` ici, contrairement à l'embauche : il n'y
            # a pas de personnel appelant dont on borne le périmètre. La garde
            # équivalente est portée par le sérialiseur, qui n'accepte qu'un
            # établissement ouvert au public.
            courier = CourierService.provision(
                application=CourierApplication(**serializer.validated_data)
            )
            challenge = VerificationService.issue(
                user=courier.user, purpose=VerificationPurpose.ACCOUNT_VERIFICATION
            )

        return Response(
            {
                "email": challenge.email,
                "expires_at": challenge.expires_at,
                "retry_after": challenge.retry_after,
                "code_length": challenge.code_length,
                "verification_status": courier.verification_status,
                "detail": (
                    "Votre compte est créé. Saisissez le code envoyé à votre adresse "
                    "pour l'activer, puis déposez vos pièces : votre dossier sera "
                    "instruit par El Corazón avant votre première course."
                ),
            },
            status=status.HTTP_201_CREATED,
        )


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
    """`/delivery/me/` — le dossier du livreur, par lui-même.

    Trois verbes, trois natures :

    * `GET` — le dossier tel qu'il est, pièces comprises (en URL signées) ;
    * `POST` — le dépôt de **pièces**, en `multipart`, qui rouvre l'instruction
      (L5) ;
    * `PATCH` — la correction des champs **descriptifs** du dossier, en JSON,
      qui ne la rouvre pas.

    La séparation des deux écritures suit celle de leurs effets, pas celle de
    leurs formats : téléverser une carte grise remet le dossier dans la file
    d'instruction, corriger la plaque qui y figure ne le fait pas. Les réunir
    sous un seul verbe obligerait à deviner l'intention d'après les clés
    présentes.

    `JSONParser` est déclaré **en plus** des deux parseurs de formulaire. Il
    manquait, et son absence n'avait aucun effet visible tant que la vue
    n'acceptait que du `multipart` : elle en a un dès qu'un verbe attend du
    JSON, et c'est un 415 que rien dans le corps de la requête n'explique.
    """

    permission_classes = [IsCourier]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(responses={200: CourierProfileSerializer}, tags=["delivery"])
    def get(self, request: Request) -> Response:
        return Response(CourierProfileSerializer(courier_of(request)).data)

    @extend_schema(
        request=DocumentsSerializer, responses={200: CourierProfileSerializer}, tags=["delivery"]
    )
    def post(self, request: Request) -> Response:
        """Dépôt de pièces — rouvre l'instruction du dossier (L5)."""
        serializer = DocumentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        courier = CourierService.replace_documents(
            courier=courier_of(request), **serializer.validated_data
        )
        return Response(CourierProfileSerializer(courier).data)

    @extend_schema(
        request=CourierSelfUpdateSerializer,
        responses={200: CourierProfileSerializer},
        tags=["delivery"],
    )
    def patch(self, request: Request) -> Response:
        """Correction du véhicule et des numéros de pièces, par le livreur.

        Le dossier corrigé est celui du jeton : il n'y a pas d'identifiant en
        entrée, donc pas de dossier d'autrui à viser. Même forme de réponse que
        `GET`, pour que l'appelant n'ait qu'une seule lecture à savoir faire.

        Cette route manquait, et son absence se voyait à l'écran : `Dely`
        affichait le véhicule et la plaque dans des champs grisés avec, en
        commentaire, « on ne permet pas de modifier ici pour l'instant ». Un
        livreur qui changeait de véhicule n'avait aucune issue depuis son
        application — et le back-office, seul à disposer de `PATCH
        /delivery/couriers/{id}/`, n'apprenait le changement que par téléphone.
        """
        serializer = CourierSelfUpdateSerializer(
            courier_of(request), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        courier = serializer.save()

        return Response(CourierProfileSerializer(courier).data)


class CourierEarningsView(APIView):
    """`/delivery/me/earnings/` — les gains du livreur, agrégés par période.

    Existe parce que l'application les additionnait elle-même, sur les soixante
    dernières courses chargées : son onglet « ce mois » n'en couvrait donc que
    les tout derniers jours dès qu'un livreur travaillait un peu. Une somme se
    demande au serveur, qui les a toutes.
    """

    permission_classes = [IsCourier]

    @extend_schema(responses={200: EarningsSerializer}, tags=["delivery"])
    def get(self, request: Request) -> Response:
        gains = CourierService.earnings(courier=courier_of(request))
        return Response(EarningsSerializer(gains).data)


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
            .select_related("order__restaurant", "order__city", "courier__user")
            # Le détail des articles est rendu sur chaque course : sans ce
            # préchargement, une file de dix propositions coûterait dix requêtes.
            .prefetch_related("order__lines")
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
    @extend_schema(
        request={"multipart/form-data": ProofOfDeliverySerializer},
        responses={200: AssignmentSerializer},
        tags=["delivery"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="proof",
        url_name="proof",
        parser_classes=[MultiPartParser, FormParser],
    )
    def proof(self, request: Request, pk: str) -> Response:
        """Preuve de livraison : la photo prise à la remise.

        `proof_of_delivery` existait en base sans aucune route pour l'écrire —
        l'application du livreur ne pouvait rien déposer.
        """
        serializer = ProofOfDeliverySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignment = AssignmentService.attach_proof(
            assignment=self.get_object(),
            courier=courier_of(request),
            photo=serializer.validated_data["photo"],
        )
        return Response(AssignmentSerializer(assignment).data)

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


class StaffCourierViewSet(CreateModelMixin, UpdateModelMixin, ReadOnlyModelViewSet[CourierProfile]):
    """Flotte, vue, **ouverte** et corrigée par le personnel de ses établissements.

    Pas de suppression : ce qu'un livreur a livré, encaissé et signé y renvoie.
    Le retirer du service se fait par la suspension (`verification/`), qui
    laisse le dossier lisible.

    **La correction, elle, est possible — en `PATCH` seulement.** Le back-office
    n'avait aucun moyen de rectifier une plaque relevée de travers à l'embauche
    ou un numéro de téléphone qui change : il fallait ouvrir un second compte,
    c'est-à-dire dédoubler un livreur et scinder ses compteurs.

    Deux garde-fous portent cette ouverture :

    * `CourierUpdateSerializer` est une **liste blanche**. Le statut du dossier,
      les pièces, `is_online`, les compteurs, l'établissement et l'adresse
      électronique n'y figurent pas — voir son docstring, qui dit pour chacun
      pourquoi ;
    * `PUT` est refusé (`http_method_names`). Un remplacement complet obligerait
      l'appelant à renvoyer le dossier entier, et le premier champ qu'il
      oublierait serait écrasé par un défaut. Une correction est partielle par
      nature.
    """

    permission_classes = [HasReadWritePermission.of(read="couriers.read", write="couriers.write")]
    serializer_class = CourierProfileSerializer
    queryset = CourierProfile.objects.none()  # pour le générateur de schéma

    #: `put` retiré de la liste par défaut de `UpdateModelMixin` : voir le
    #: docstring de la classe. `delete` n'y a jamais été.
    #:
    #: Sans `ClassVar` : `django.views.View` déclare `http_method_names` en
    #: variable d'instance, et `mypy` refuse qu'une sous-classe la redéclare en
    #: variable de classe. L'annotation est donc simplement omise — l'affectation
    #: reste bien un attribut de classe à l'exécution, comme dans Django même.
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = {
        "verification_status": ["exact"],
        "is_online": ["exact"],
        "restaurant__slug": ["exact"],
    }

    def get_queryset(self) -> QuerySet[CourierProfile]:
        user = authenticated_user(self.request)
        queryset = (
            CourierProfile.objects.select_related("user", "restaurant")
            .prefetch_related("service_zones")
            .order_by("user__full_name")
        )
        if is_unscoped(user):
            return queryset
        return queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action == "create":
            return CourierProvisioningSerializer
        if self.action == "partial_update":
            return CourierUpdateSerializer
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

        # Le code de vérification part **aussi** pour une embauche, comme pour
        # une candidature (`CourierApplicationView`). Il ne l'était pas, et
        # l'asymétrie coûtait : l'adresse saisie par le personnel n'était
        # jamais éprouvée, si bien qu'une faute de frappe ne se découvrait
        # qu'au premier « mot de passe oublié » — sur un compte auquel plus
        # personne ne pouvait envoyer quoi que ce soit.
        #
        # Elle n'ouvre ni ne ferme rien : la vérification d'adresse n'est pas
        # opposée à la connexion, et l'éligibilité aux courses reste celle de
        # L1 — en ligne, dossier validé, compte actif.
        with transaction.atomic():
            courier = CourierService.provision(
                application=CourierApplication(**serializer.validated_data)
            )
            VerificationService.issue(
                user=courier.user, purpose=VerificationPurpose.ACCOUNT_VERIFICATION
            )
        return Response(CourierProfileSerializer(courier).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=CourierUpdateSerializer,
        responses={200: CourierProfileSerializer},
        tags=["delivery"],
    )
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Corrige un dossier — identité de contact et véhicule.

        Pas d'`assert_in_scope` ici, contrairement à `create` : l'objet existe,
        et `get_queryset` l'a déjà filtré sur le périmètre du compte. Un dossier
        hors périmètre est donc **introuvable** (404) plutôt qu'interdit (403),
        ce qui est le comportement de tout le reste du module — l'existence
        d'un livreur d'une autre enseigne n'a pas à se déduire d'un code de
        réponse.

        La réponse rend le dossier **complet** et non les seuls champs
        modifiés : l'écran qui vient de corriger une plaque remplace sa ligne
        avec ce qu'il reçoit, et une réponse partielle lui ferait perdre les
        compteurs qu'il affichait.
        """
        courier = self.get_object()
        serializer = CourierUpdateSerializer(instance=courier, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        courier = serializer.save()
        return Response(CourierProfileSerializer(courier).data)

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
            expirations={
                champ: serializer.validated_data[champ]
                for champ in CourierService.PIECES_DATEES.values()
                if champ in serializer.validated_data
            },
        )
        return Response(CourierProfileSerializer(courier).data)

    @extend_schema(
        request=CourierZonesSerializer,
        responses={200: CourierProfileSerializer},
        tags=["delivery"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="zones",
        permission_classes=[HasPermission.of("couriers.write")],
    )
    def zones(self, request: Request, pk: str) -> Response:
        """Affecte le livreur à des zones de sa cuisine — la liste entière.

        Le dossier est lu par `get_object`, donc cloisonné : un livreur hors
        périmètre est introuvable. Les zones, elles, sont vérifiées par le
        service contre la desserte **de la cuisine du livreur** — une zone d'une
        autre ville est refusée en 409, sans rien écrire.
        """
        courier = self.get_object()
        serializer = CourierZonesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        CourierService.set_service_zones(courier=courier, zones=serializer.validated_data["zones"])
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
        return Response(
            AssignmentSerializer(assignment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


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
        return Response(AssignmentSerializer(cancelled, context={"request": request}).data)


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
