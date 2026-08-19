"""Points d'entrée du paiement.

Trois routes, trois publics : le client initie, le prestataire notifie, le
personnel rembourse. La seule ouverte sans jeton est le webhook — un
prestataire n'a pas de compte utilisateur — et elle est authentifiée par la
signature du corps, ce qui est plus fort qu'un jeton porteur : une signature ne
peut pas être rejouée sur un autre corps.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.accounts.models import UserType
from apps.delivery.views import courier_of
from apps.orders.models import Order
from apps.payments.gateway import GatewayError, gateway_for
from apps.payments.models import (
    PaymentProvider,
    SplitPayment,
    SplitShare,
    Transaction,
    Withdrawal,
)
from apps.payments.serializers import (
    CheckoutSerializer,
    RefundRequestSerializer,
    RefundSerializer,
    ShareCheckoutSerializer,
    SplitCreateSerializer,
    SplitPaymentSerializer,
    SplitShareSerializer,
    TransactionSerializer,
    WebhookSerializer,
    WithdrawalRequestSerializer,
    WithdrawalSerializer,
)
from apps.payments.services import PaymentService, RefundService, WithdrawalService
from apps.payments.split import ParticipantInput, SplitService
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from common.permissions import HasPermission, IsCourier, IsCustomer, authenticated_user
from common.throttling import (
    PaymentInitiationThrottle,
    ResilientScopedRateThrottle,
    StrictScopedRateThrottle,
)

__all__ = [
    "InitiatePaymentView",
    "RefundView",
    "ShareView",
    "SplitPaymentView",
    "TransactionViewSet",
    "WebhookView",
]

SIGNATURE_HEADER = "X-Signature"

logger = logging.getLogger(__name__)


class TransactionViewSet(ReadOnlyModelViewSet[Transaction]):
    """Historique des encaissements, filtré sur le demandeur."""

    serializer_class = TransactionSerializer
    queryset = Transaction.objects.none()
    filterset_fields = {"order": ["exact"], "status": ["exact"]}

    def get_queryset(self) -> QuerySet[Transaction]:
        user = authenticated_user(self.request)
        queryset = Transaction.objects.select_related("order").order_by("-created_at")
        if user.user_type == UserType.STAFF:
            if is_unscoped(user):
                return queryset
            # Même périmètre que les commandes : un encaissement appartient à
            # l'établissement qui l'a réalisé.
            return queryset.filter(order__restaurant_id__in=staff_restaurant_ids(user))
        # Le client voit les transactions de **ses** commandes, y compris
        # celles qu'un tiers a réglées pour lui — pas seulement celles dont il
        # est le payeur.
        return queryset.filter(order__customer=user)


class InitiatePaymentView(APIView):
    """`POST /payments/{order}/initiate/` — ouvre une demande de paiement."""

    permission_classes = [IsCustomer]
    throttle_classes = [PaymentInitiationThrottle]

    @extend_schema(request=None, responses={201: CheckoutSerializer}, tags=["payments"])
    def post(self, request: Request, order_id: str) -> Response:
        user = authenticated_user(request)
        order = get_object_or_404(Order, pk=order_id, customer=user)

        txn, instruction = PaymentService.initiate(order=order, payer=user)
        return Response(
            CheckoutSerializer(
                {
                    "transaction": txn,
                    "checkout_url": instruction.checkout_url,
                    "instructions": instruction.instructions,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )


class WebhookView(APIView):
    """`POST /payments/webhook/{provider}/` — notification du prestataire.

    **Seule source de vérité de l'encaissement** (§6.3). Le retour du client
    sur l'application ne déclenche aucune écriture d'état : c'est ce qui rend
    l'auto-déclaration de paiement impossible plutôt qu'interdite.

    La réponse est un 200 dès que la notification a été **prise en compte**,
    même si elle ne correspond à rien de connu : un prestataire qui reçoit une
    erreur retente, indéfiniment, et finit par saturer la file de l'un comme de
    l'autre. Ce qui n'a pas pu être appliqué est tracé dans `WebhookEvent`.
    """

    permission_classes = [AllowAny]
    authentication_classes: list[type[Any]] = []
    # Ouvert si le cache tombe, contrairement au reste de `payments` : ce qui
    # garde cette route est la **signature** du prestataire, pas le compteur.
    # Refuser ici perdrait des confirmations de paiement — donc des commandes
    # payées mais jamais marquées telles — pour protéger une porte qu'une
    # signature ferme déjà.
    throttle_classes = [ResilientScopedRateThrottle]
    throttle_scope = "webhook"

    @extend_schema(
        request=WebhookSerializer,
        responses={200: None},
        parameters=[
            OpenApiParameter(
                name=SIGNATURE_HEADER,
                location=OpenApiParameter.HEADER,
                required=True,
                description="HMAC-SHA256 du corps brut, en hexadécimal.",
            )
        ],
        tags=["payments"],
    )
    def post(self, request: Request, provider: str) -> Response:
        if provider not in PaymentProvider.values:
            raise ValidationError({"provider": f"Prestataire inconnu : {provider}."})

        connecteur = gateway_for(provider)
        recu = _corps(request)

        # Authentification d'abord. Rien n'est enregistré tant qu'elle n'a pas
        # abouti, sans quoi n'importe qui remplirait la table des événements en
        # postant du JSON. Le schéma appartient au connecteur : PayDunya joint
        # une empreinte de sa clé maîtresse là où le bac à sable signe le corps.
        #
        # Le refus sort en 403 et non en 401 : la route ne déclare aucun
        # authentificateur DRF — le justificatif *est* dans le corps ou
        # l'en-tête — donc aucun schéma n'est proposé en défi.
        if not connecteur.authenticate(raw_body=request.body, headers=request.headers, data=recu):
            raise AuthenticationFailed("Notification non authentifiée.")

        try:
            notification = connecteur.parse(recu)
        except GatewayError as exc:
            # Notification authentique mais illisible : on l'accepte pour ne
            # pas la faire retenter, et on la trace. Le prestataire n'y peut
            # rien, et nous non plus dans l'instant.
            logger.warning("webhook.illisible", extra={"provider": provider, "detail": str(exc)})
            return Response({"accepted": True, "detail": str(exc)})

        outcome = PaymentService.handle_webhook(
            provider=provider, notification=notification, payload=recu
        )
        return Response({"accepted": outcome.accepted, "detail": outcome.detail})


def _corps(request: Request) -> dict[str, Any]:
    """Corps de la notification, JSON ou formulaire.

    PayDunya poste un formulaire imbriqué, le bac à sable du JSON. Choisir
    selon le type de contenu plutôt que d'imposer le JSON évite d'écrire un
    connecteur qui ne pourrait pas être appelé par son propre prestataire.
    """
    if request.content_type and "json" in request.content_type:
        try:
            parsed: dict[str, Any] = json.loads(request.body or b"{}")
        except ValueError:
            return {}
        return parsed
    return dict(request.data.items())


class SplitPaymentView(APIView):
    """`/payments/{order}/split/` — ouvrir et consulter un partage."""

    permission_classes = [IsCustomer]

    @extend_schema(responses={200: SplitPaymentSerializer}, tags=["payments"])
    def get(self, request: Request, order_id: str) -> Response:
        order = get_object_or_404(Order, pk=order_id, customer=authenticated_user(request))
        split = get_object_or_404(SplitPayment, order=order)
        return Response(SplitPaymentSerializer(split).data)

    @extend_schema(
        request=SplitCreateSerializer, responses={201: SplitPaymentSerializer}, tags=["payments"]
    )
    def post(self, request: Request, order_id: str) -> Response:
        user = authenticated_user(request)
        order = get_object_or_404(Order, pk=order_id, customer=user)

        serializer = SplitCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        split = SplitService.create(
            order=order,
            initiator=user,
            participants=[
                ParticipantInput(
                    display_name=p["display_name"],
                    user=p.get("user"),
                    phone=p.get("phone", ""),
                    amount=p.get("amount"),
                )
                for p in serializer.validated_data["participants"]
            ],
        )
        return Response(SplitPaymentSerializer(split).data, status=status.HTTP_201_CREATED)


class ShareView(APIView):
    """`/payments/shares/{token}/` — la part, vue par son destinataire.

    **Sans authentification**, et c'est le cœur de la fonctionnalité : la
    moitié des convives d'un repas partagé n'ont pas de compte, et exiger une
    inscription pour payer sa part ferait échouer la fonctionnalité sur son cas
    le plus courant.

    Le justificatif est le jeton du lien, aléatoire et long. Il ne donne accès
    qu'à cette part — son montant, son statut, de quoi payer — jamais à la
    commande ni aux autres participants.
    """

    permission_classes = [AllowAny]
    # Fermé si le cache tombe : sans authentification, le seul justificatif est
    # un jeton, et le quota est ce qui rend son énumération impraticable. Le
    # jeton est long et aléatoire, mais c'est une raison de le garder difficile
    # à deviner, pas de laisser essayer sans compter.
    throttle_classes = [StrictScopedRateThrottle]
    throttle_scope = "share_access"

    @extend_schema(responses={200: SplitShareSerializer}, tags=["payments"])
    def get(self, request: Request, token: str) -> Response:
        return Response(SplitShareSerializer(self._share(token)).data)

    @extend_schema(request=None, responses={201: ShareCheckoutSerializer}, tags=["payments"])
    def post(self, request: Request, token: str) -> Response:
        """Ouvre le règlement de cette part.

        Ne solde rien : la part suivra sa transaction, qui suivra la
        notification signée du prestataire (P2).
        """
        share = self._share(token)
        payeur = authenticated_user(request) if request.user is not None else None

        _, instruction = SplitService.pay_share(share=share, payer=payeur)
        share.refresh_from_db()

        return Response(
            ShareCheckoutSerializer(
                {
                    "share": share,
                    "checkout_url": instruction.checkout_url,
                    "instructions": instruction.instructions,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _share(token: str) -> SplitShare:
        return get_object_or_404(
            SplitShare.objects.select_related("split__order"), share_token=token
        )


class RefundView(APIView):
    """`POST /payments/{order}/refund/` — remboursement, réservé au personnel."""

    permission_classes = [HasPermission.of("orders.refund")]

    @extend_schema(
        request=RefundRequestSerializer, responses={201: RefundSerializer}, tags=["payments"]
    )
    def post(self, request: Request, order_id: str) -> Response:
        actor = authenticated_user(request)
        # La permission `orders.refund` dit qu'on sait rembourser ; le
        # rattachement dit sur quelles commandes. Sans ce filtre, un opérateur
        # de Kara rembourserait une commande de Lomé — avec l'argent de Lomé.
        scope = Order.objects.all()
        if not is_unscoped(actor):
            scope = scope.filter(restaurant_id__in=staff_restaurant_ids(actor))
        order = get_object_or_404(scope, pk=order_id)

        serializer = RefundRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        refund = RefundService.refund(
            order=order,
            transaction_id=str(serializer.validated_data["transaction"]),
            amount=serializer.validated_data["amount"],
            reason=serializer.validated_data["reason"],
            actor=actor,
        )
        return Response(RefundSerializer(refund).data, status=status.HTTP_201_CREATED)


class WithdrawalView(APIView):
    """`/payments/withdrawals/` — les retraits du livreur qui appelle.

    Réservé aux livreurs : le bénéficiaire est l'appelant, il ne se désigne pas.

    La demande **débite les gains** et enregistre une intention de versement ;
    elle ne verse rien. Comme pour les remboursements, le mouvement d'argent est
    un geste de l'exploitation — c'est ce qu'il faut dire à qui verra une ligne
    « en attente » apparaître.
    """

    permission_classes = [IsCourier]

    @extend_schema(responses={200: WithdrawalSerializer(many=True)}, tags=["payments"])
    def get(self, request: Request) -> Response:
        withdrawals = Withdrawal.objects.filter(courier=courier_of(request)).order_by("-created_at")
        return Response(WithdrawalSerializer(withdrawals, many=True).data)

    @extend_schema(
        request=WithdrawalRequestSerializer,
        responses={201: WithdrawalSerializer},
        tags=["payments"],
    )
    def post(self, request: Request) -> Response:
        payload = WithdrawalRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        withdrawal = WithdrawalService.request(
            courier=courier_of(request), amount=payload.validated_data["amount"]
        )
        return Response(WithdrawalSerializer(withdrawal).data, status=status.HTTP_201_CREATED)
