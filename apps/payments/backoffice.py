"""Back-office des mouvements d'argent sortants — retraits livreurs, remboursements.

## Ce que ce module comble

Les deux sortent de l'argent de l'enseigne, et les deux s'arrêtaient à mi-chemin :

* **un retrait livreur** débitait les gains à la demande, puis attendait un
  constat de versement que rien ne pouvait poser — `WithdrawalService.settle`
  et `fail` n'avaient aucun appelant hors des tests, et `Withdrawal` n'était
  même pas dans l'administration Django. L'argent n'était plus dans
  l'application et n'était pas chez le livreur ;
* **un remboursement** se demandait depuis le back-office, et ne se constatait
  que dans l'administration Django — un second outil, que l'opérateur qui
  venait de le demander n'avait pas.

## Ce qu'il ne fait pas

Il ne verse rien. PayDunya n'expose aucune API de remboursement, et le
décaissement vers un livreur part, lui aussi, d'un geste humain chez le
prestataire. Ces routes **constatent** un fait extérieur, et le disent : la
référence du virement est exigée pour un retrait, parce que c'est la preuve
qu'on cherchera.

## Périmètre

Celui du reste du back-office : un livreur appartient à l'établissement de son
dossier, un remboursement à celui de la commande. Hors périmètre, l'objet est
introuvable (404), pas interdit — l'existence d'un retrait ailleurs n'a pas à se
déduire d'un code de réponse.
"""

from __future__ import annotations

from typing import ClassVar

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.payments.models import Refund, Withdrawal
from apps.payments.serializers import (
    ManagedRefundSerializer,
    ManagedWithdrawalSerializer,
    RefundCancelSerializer,
    RefundSettleSerializer,
    WithdrawalRejectSerializer,
    WithdrawalSettleSerializer,
)
from apps.payments.services import RefundService, WithdrawalService
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids
from common.permissions import HasPermission, authenticated_user

__all__ = ["ManagedRefundViewSet", "ManagedWithdrawalViewSet"]


class ManagedWithdrawalViewSet(ReadOnlyModelViewSet[Withdrawal]):
    """`/payments/manage/withdrawals/` — les demandes de retrait à instruire.

    Lire exige `payouts.read` ; constater ou refuser, `payouts.settle`. Les deux
    sont séparés pour que qui prépare les virements ne soit pas forcément qui
    les signe.
    """

    serializer_class = ManagedWithdrawalSerializer
    permission_classes = (HasPermission.of("payouts.read"),)
    queryset = Withdrawal.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact"],
        "courier": ["exact"],
        "courier__restaurant__slug": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["courier__user__full_name", "courier__user__phone"]

    def get_queryset(self) -> QuerySet[Withdrawal]:
        user = authenticated_user(self.request)
        queryset = Withdrawal.objects.select_related(
            "courier__user", "courier__restaurant", "processed_by"
        ).order_by("-created_at")
        if is_unscoped(user):
            return queryset
        return queryset.filter(courier__restaurant_id__in=staff_restaurant_ids(user))

    @extend_schema(
        request=WithdrawalSettleSerializer,
        responses={200: ManagedWithdrawalSerializer},
        tags=["payments"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="settle",
        url_name="settle",
        permission_classes=[HasPermission.of("payouts.settle")],
    )
    def settle(self, request: Request, pk: str) -> Response:
        """Constate que le versement a été fait — référence du virement exigée.

        Une demande déjà soldée ou refusée est refusée en 409 par la machine à
        états : signer deux fois le même versement ferait croire qu'il y en a
        eu deux.
        """
        withdrawal = self.get_object()
        serializer = WithdrawalSettleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        solde = WithdrawalService.settle(
            withdrawal=withdrawal,
            provider_reference=serializer.validated_data["provider_reference"],
            actor=authenticated_user(request),
        )
        return Response(ManagedWithdrawalSerializer(self._relu(solde)).data)

    @extend_schema(
        request=WithdrawalRejectSerializer,
        responses={200: ManagedWithdrawalSerializer},
        tags=["payments"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="reject",
        url_name="reject",
        permission_classes=[HasPermission.of("payouts.settle")],
    )
    def reject(self, request: Request, pk: str) -> Response:
        """Refuse le versement — les gains sont **rendus** au livreur.

        Motif exigé : le livreur le lira, et c'est lui qui devra corriger ce
        qui a bloqué (un numéro erroné, le plus souvent).
        """
        withdrawal = self.get_object()
        serializer = WithdrawalRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        refuse = WithdrawalService.fail(
            withdrawal=withdrawal,
            reason=serializer.validated_data["reason"],
            actor=authenticated_user(request),
        )
        return Response(ManagedWithdrawalSerializer(self._relu(refuse)).data)

    def _relu(self, withdrawal: Withdrawal) -> Withdrawal:
        """Relit la ligne avec ses jointures, pour la réponse."""
        return self.get_queryset().get(pk=withdrawal.pk)


class ManagedRefundViewSet(ReadOnlyModelViewSet[Refund]):
    """`/payments/manage/refunds/` — les remboursements demandés, et leur constat.

    Même permission que la demande (`orders.refund`) : qui a le droit de
    décider qu'un client est remboursé a celui de constater qu'il l'a été. La
    lecture, elle, suit celle des commandes.
    """

    serializer_class = ManagedRefundSerializer
    permission_classes = (HasPermission.of("orders.read"),)
    queryset = Refund.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact"],
        "order": ["exact"],
        "order__restaurant__slug": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["order__reference", "order__customer__full_name"]

    def get_queryset(self) -> QuerySet[Refund]:
        user = authenticated_user(self.request)
        queryset = Refund.objects.select_related(
            "order__restaurant", "order__customer", "transaction", "requested_by"
        ).order_by("-created_at")
        if is_unscoped(user):
            return queryset
        return queryset.filter(order__restaurant_id__in=staff_restaurant_ids(user))

    @extend_schema(
        request=RefundSettleSerializer,
        responses={200: ManagedRefundSerializer},
        tags=["payments"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="settle",
        url_name="settle",
        permission_classes=[HasPermission.of("orders.refund")],
    )
    def settle(self, request: Request, pk: str) -> Response:
        """Constate que le remboursement a été versé au client.

        Jusqu'ici, seule l'action « Constater le virement » de l'administration
        Django le permettait : l'opérateur qui venait de demander un
        remboursement depuis le back-office ne pouvait pas le clore.
        """
        refund = self.get_object()
        serializer = RefundSettleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        solde = RefundService.settle(
            refund=refund,
            provider_reference=serializer.validated_data.get("provider_reference", ""),
            actor=authenticated_user(request),
        )
        return Response(ManagedRefundSerializer(self.get_queryset().get(pk=solde.pk)).data)

    @extend_schema(
        request=RefundCancelSerializer,
        responses={200: ManagedRefundSerializer},
        tags=["payments"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="cancel",
        url_name="cancel",
        permission_classes=[HasPermission.of("orders.refund")],
    )
    def cancel(self, request: Request, pk: str) -> Response:
        """Abandonne un remboursement qui ne sera pas versé — motif exigé.

        Sans cette sortie, une demande saisie par erreur restait « en attente »
        pour toujours **et** continuait de consommer le plafond du remboursable
        (P3) : la commande devenait irremboursable, et le seul recours était
        l'administration Django. Un remboursement déjà versé, lui, ne s'annule
        pas — la machine à états le refuse, et c'est la bonne réponse.
        """
        refund = self.get_object()
        serializer = RefundCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        abandonne = RefundService.cancel(
            refund=refund,
            reason=serializer.validated_data["reason"],
            actor=authenticated_user(request),
        )
        return Response(ManagedRefundSerializer(self.get_queryset().get(pk=abandonne.pk)).data)
