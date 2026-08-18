"""Points d'entrée de la fidélité.

Quatre routes pour un seul public — le client, sur son propre compte. Le
personnel n'a rien à faire ici : consulter un solde pour répondre au téléphone
se fait au back-office, et le lui ouvrir par l'API demanderait un cloisonnement
par établissement pour une donnée qui n'en dépend pas.

Le cloisonnement est partout un **filtre de requête** et jamais une permission
d'objet (ADR-005) : le mouvement d'autrui est introuvable, pas interdit. Les
distinguer par le code de statut dirait à un curieux que le compte existe.

`redeem` est la seule écriture, et elle délègue entièrement à `LoyaltyService` :
la vue ne lit pas le solde, ne le compare pas au coût et ne le décrémente pas.
Refaire ici l'un de ces trois gestes rouvrirait la course que F1 ferme, puisque
la garantie tient à ce qu'ils n'aient lieu qu'une fois, dans un seul `UPDATE`.
"""

from __future__ import annotations

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.loyalty.models import (
    PointsAccount,
    PointsEntry,
    Reward,
    RewardRedemption,
    Subscription,
    SubscriptionPlan,
)
from apps.loyalty.serializers import (
    PointsAccountSerializer,
    PointsEntrySerializer,
    RedemptionResultSerializer,
    RewardRedemptionSerializer,
    RewardSerializer,
    SubscribeRequestSerializer,
    SubscriptionPlanSerializer,
    SubscriptionResultSerializer,
    SubscriptionSerializer,
)
from apps.loyalty.services import LoyaltyService
from apps.loyalty.subscriptions import SubscriptionService
from common.permissions import IsCustomer, authenticated_user
from common.throttling import RewardRedemptionThrottle

__all__ = [
    "PointsAccountView",
    "PointsEntryViewSet",
    "RewardRedemptionViewSet",
    "RewardViewSet",
    "SubscriptionPlanViewSet",
    "SubscriptionViewSet",
]


class PointsAccountView(APIView):
    """`GET /loyalty/account/` — le solde de celui qui appelle.

    **Ne crée rien.** `LoyaltyService.account_for` ouvre le compte à la demande,
    ce qui est le bon comportement au crédit et à l'échange ; l'appeler ici
    ferait écrire une ligne à chaque ouverture d'écran, y compris par un client
    qui n'a jamais commandé. Un compte absent et un compte à zéro se
    représentent de la même façon — la ligne peut donc attendre le premier
    mouvement, c'est-à-dire le moment où elle porte une information.
    """

    permission_classes = [IsCustomer]

    @extend_schema(responses={200: PointsAccountSerializer}, tags=["loyalty"])
    def get(self, request: Request) -> Response:
        user = authenticated_user(request)
        # L'instance non enregistrée porte les valeurs par défaut du modèle —
        # solde nul, cumuls nuls, aucune activité — soit exactement ce qu'il y a
        # à dire d'un compte qui n'a pas encore servi.
        account = PointsAccount.objects.filter(user=user).first() or PointsAccount(user=user)
        return Response(PointsAccountSerializer(account).data)


class PointsEntryViewSet(ReadOnlyModelViewSet[PointsEntry]):
    """`GET /loyalty/entries/` — le journal du client (F5).

    Paginé et filtrable par nature de mouvement : « où sont passés mes points »
    se répond en filtrant sur `expired`, ce qui est la question qu'un service
    client reçoit le plus souvent.
    """

    serializer_class = PointsEntrySerializer
    queryset = PointsEntry.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]
    filterset_fields = {"kind": ["exact"]}

    def get_queryset(self) -> QuerySet[PointsEntry]:
        return PointsEntry.objects.filter(account__user=authenticated_user(self.request))


class RewardViewSet(ReadOnlyModelViewSet[Reward]):
    """`GET /loyalty/rewards/` — le catalogue, et l'échange.

    Les récompenses suspendues sont **absentes** et non marquées inactives :
    laisser voir ce qu'on ne peut pas prendre n'aide personne, et un client qui
    tenterait l'échange se ferait refuser après avoir choisi.

    Le catalogue reste lisible en dessous du solde du client. Masquer ce qu'il ne
    peut pas encore s'offrir retirerait à la fidélité la seule chose qui la fait
    fonctionner — savoir ce vers quoi on économise.
    """

    serializer_class = RewardSerializer
    queryset = Reward.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]
    filterset_fields = {"kind": ["exact"], "restaurant": ["exact", "isnull"]}

    def get_queryset(self) -> QuerySet[Reward]:
        return Reward.objects.filter(is_active=True).select_related("restaurant")

    @extend_schema(request=None, responses={201: RedemptionResultSerializer}, tags=["loyalty"])
    @action(
        detail=True,
        methods=["post"],
        url_path="redeem",
        throttle_classes=[RewardRedemptionThrottle],
    )
    def redeem(self, request: Request, pk: str) -> Response:
        """Échange des points contre le code de cette récompense.

        Le corps est **vide** : la récompense est dans l'URL et son coût en
        base. Il n'y a rien à déclarer, donc rien qu'un client puisse annoncer à
        son avantage.

        Un solde insuffisant sort en 409 `insufficient_balance` par le
        gestionnaire d'exceptions du projet — y compris lorsque l'insuffisance
        vient d'une requête concurrente passée entre-temps, que rien ne
        distingue ici d'un solde trop faible au départ. Les deux se traitent
        pareil, et c'est ce qui rend F1 tenable.
        """
        result = LoyaltyService.redeem(user=authenticated_user(request), reward=self.get_object())
        return Response(
            RedemptionResultSerializer(
                {
                    "redemption": result.redemption,
                    "promotion": result.promotion,
                    "balance": result.balance,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )


class RewardRedemptionViewSet(ReadOnlyModelViewSet[RewardRedemption]):
    """`GET /loyalty/redemptions/` — les échanges passés du client.

    Distinct du journal : celui-ci dit ce qui a été *acheté* et sous quel code,
    là où le journal dit ce qui a été débité. Un client qui cherche un code
    reçu la semaine dernière regarde ici, pas dans une liste de mouvements.
    """

    serializer_class = RewardRedemptionSerializer
    queryset = RewardRedemption.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[RewardRedemption]:
        return RewardRedemption.objects.filter(
            user=authenticated_user(self.request)
        ).select_related("reward", "reward__restaurant")


class SubscriptionPlanViewSet(ReadOnlyModelViewSet[SubscriptionPlan]):
    """`GET /loyalty/plans/` — le catalogue tarifé (P4).

    Suspendu et non seulement inactif : un plan retiré du catalogue reste lu
    par les abonnements en cours (`SubscriptionSerializer` l'imbrique), mais
    n'apparaît plus ici — la même règle que pour `RewardViewSet`.
    """

    serializer_class = SubscriptionPlanSerializer
    queryset = SubscriptionPlan.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[SubscriptionPlan]:
        return SubscriptionPlan.objects.filter(is_active=True)


class SubscriptionViewSet(ReadOnlyModelViewSet[Subscription]):
    """`GET /loyalty/subscriptions/` — les abonnements du client, passés et courant.

    `subscribe` et `cancel` délèguent entièrement à `SubscriptionService` : ni
    l'un ni l'autre ne décide d'un prix, d'une période ou d'un statut — le
    service les tient (P4, ADR-010).
    """

    serializer_class = SubscriptionSerializer
    queryset = Subscription.objects.none()  # pour le générateur de schéma
    permission_classes = [IsCustomer]

    def get_queryset(self) -> QuerySet[Subscription]:
        return Subscription.objects.filter(user=authenticated_user(self.request)).select_related(
            "plan"
        )

    @extend_schema(
        request=SubscribeRequestSerializer,
        responses={201: SubscriptionResultSerializer},
        tags=["loyalty"],
    )
    @action(detail=False, methods=["post"])
    def subscribe(self, request: Request) -> Response:
        """Ouvre un abonnement au plan désigné.

        Refusé si le client en a déjà un ouvert — `pending` ou `active` —
        par `one_open_subscription_per_user` (409, pas 400 : la requête est
        bien formée, c'est l'état qui l'interdit).
        """
        payload = SubscribeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        subscription, instruction = SubscriptionService.subscribe(
            user=authenticated_user(request), plan=payload.validated_data["plan"]
        )
        return Response(
            SubscriptionResultSerializer(
                {
                    "subscription": subscription,
                    "checkout_url": instruction.checkout_url,
                    "instructions": instruction.instructions,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=None, responses={200: SubscriptionSerializer}, tags=["loyalty"])
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str) -> Response:
        """Résilie cet abonnement — sans effet sur l'échéance déjà payée."""
        subscription = SubscriptionService.cancel(subscription=self.get_object())
        return Response(SubscriptionSerializer(subscription).data)
