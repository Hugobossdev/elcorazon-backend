"""Points d'entrée du carnet d'adresses et des préférences.

**L'appartenance est un filtre de requête, pas une permission.** `get_queryset`
ne renvoie que les adresses de l'appelant, si bien qu'une adresse d'autrui
donne un 404 et non un 403 : un 403 confirmerait au demandeur que l'identifiant
qu'il a deviné existe — il l'apprendrait par le code de statut, sans jamais
voir le contenu.
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from apps.profiles.models import Address, CustomerPreference
from apps.profiles.serializers import AddressSerializer, CustomerPreferenceSerializer
from common.permissions import authenticated_user

__all__ = ["AddressViewSet", "PreferenceView"]


class AddressViewSet(ModelViewSet[Address]):
    """Carnet d'adresses du client.

    Suppression **dure**, contrairement au catalogue : le RGPD impose un droit
    à l'effacement, et aucune écriture financière ne pointe ici — la commande
    en conserve une copie figée, c'est elle qui doit rester lisible.
    """

    serializer_class = AddressSerializer
    queryset = Address.objects.none()

    def get_queryset(self) -> QuerySet[Address]:
        return Address.objects.filter(user=authenticated_user(self.request)).select_related("city")

    @transaction.atomic
    def perform_create(self, serializer: BaseSerializer[Address]) -> None:
        user = authenticated_user(self.request)
        first_one = not Address.objects.filter(user=user).exists()

        # La première adresse devient le défaut sans qu'on ait à le demander :
        # un carnet dont aucune entrée n'est par défaut obligerait l'écran de
        # commande à choisir arbitrairement, ou à ne rien pré-remplir.
        is_default = serializer.validated_data.get("is_default", False) or first_one
        if is_default:
            self._demote_current_default(user.pk)

        serializer.save(user=user, is_default=is_default)

    @transaction.atomic
    def perform_update(self, serializer: BaseSerializer[Address]) -> None:
        if serializer.validated_data.get("is_default"):
            self._demote_current_default(authenticated_user(self.request).pk)
        serializer.save()

    @staticmethod
    def _demote_current_default(user_pk: uuid.UUID) -> None:
        """Retire le défaut à l'adresse qui le porte, avant d'en désigner une autre.

        L'index unique partiel `one_default_address_per_user` refuse deux
        adresses par défaut : sans ce retrait préalable, dans la même
        transaction, la promotion échouerait en violation d'intégrité. La base
        décide, le code s'y conforme — et non l'inverse.
        """
        Address.objects.filter(user_id=user_pk, is_default=True).update(is_default=False)


class PreferenceView(APIView):
    """Préférences du client — une par compte, créée à la demande.

    Ni `POST` ni `DELETE` : il n'y a rien à créer ni à supprimer, seulement un
    objet unique à lire et à modifier. Les préférences sont séparées de `User`
    parce qu'elles n'évoluent pas au même rythme que l'identité et qu'elles
    n'ont pas à être chargées à chaque authentification.
    """

    @extend_schema(responses={200: CustomerPreferenceSerializer}, tags=["profiles"])
    def get(self, request: Request) -> Response:
        return Response(CustomerPreferenceSerializer(self._preferences(request)).data)

    @extend_schema(
        request=CustomerPreferenceSerializer,
        responses={200: CustomerPreferenceSerializer},
        tags=["profiles"],
    )
    def patch(self, request: Request) -> Response:
        serializer = CustomerPreferenceSerializer(
            self._preferences(request), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @staticmethod
    def _preferences(request: Request) -> CustomerPreference:
        preferences, _ = CustomerPreference.objects.get_or_create(user=authenticated_user(request))
        return preferences
