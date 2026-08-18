"""Administration des comptes et des rôles — ADR-005.

Deux ressources, et une frontière nette entre elles :

* **les clients** se consultent et se bloquent, jamais ne s'éditent. Le
  back-office ne change ni l'adresse électronique ni le nom d'un client : ce
  sont ses données, il les modifie depuis son application. Ce que
  l'exploitation peut faire, c'est fermer un compte — et ce geste-là est
  irréversible pour les jetons en circulation, qui sont révoqués ;
* **les rôles** se composent librement, à partir d'un registre fermé
  (`apps.accounts.permissions.PERMISSIONS`). C'est ce qui permet de créer un
  poste sur mesure sans redéployer, et c'est ce qui manquait à
  l'implémentation précédente : ses rôles n'étaient appliqués que côté
  interface.

**Aucun cloisonnement par établissement ici**, et c'est délibéré : un client
n'appartient à aucun restaurant — il commande où il veut — et un rôle est un
objet d'enseigne. La permission suffit donc à porter la décision, sans
troisième étage.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from apps.accounts.models import Role, User, UserType
from apps.accounts.permissions import PERMISSIONS
from apps.accounts.serializers import (
    BlockSerializer,
    CustomerSerializer,
    PermissionSerializer,
    RoleSerializer,
)
from apps.accounts.services import AuthService
from common.permissions import HasPermission, HasReadWritePermission

__all__ = ["CustomerViewSet", "RoleViewSet"]


class CustomerViewSet(ReadOnlyModelViewSet[User]):
    """Comptes clients — consultation et blocage.

    En lecture seule **par construction** : les seuls verbes d'écriture sont
    `block` et `unblock`, et ils sont sous une permission distincte
    (`customers.block`) de celle qui donne la liste (`customers.read`). Un
    opérateur du service client consulte un dossier sans pouvoir fermer un
    compte.
    """

    serializer_class = CustomerSerializer
    permission_classes = (HasPermission.of("customers.read"),)
    queryset = User.objects.filter(user_type=UserType.CUSTOMER).order_by("-created_at")
    filterset_fields: ClassVar[dict[str, list[str]]] = {"is_active": ["exact"]}
    search_fields: ClassVar[list[str]] = ["email", "full_name", "phone"]
    ordering_fields: ClassVar[list[str]] = ["created_at", "last_seen_at", "full_name"]

    def get_queryset(self) -> QuerySet[User]:
        # Le filtre sur le type est dans la requête et non dans une garde : un
        # compte du personnel n'est pas « interdit » ici, il n'appartient pas à
        # cette ressource. `/restaurants/staff/` est faite pour lui.
        return User.objects.filter(user_type=UserType.CUSTOMER).order_by("-created_at")

    @extend_schema(request=BlockSerializer, responses={200: CustomerSerializer}, tags=["accounts"])
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[HasPermission.of("customers.block")],
    )
    def block(self, request: Request, pk: str) -> Response:
        """Ferme un compte et **révoque ses jetons**.

        Désactiver sans révoquer ne ferme rien pendant la durée de vie des
        jetons en circulation : le compte bloqué continuerait de commander
        jusqu'à l'expiration de son jeton d'accès. `has_permission` refuse déjà
        un compte inactif, mais un client n'a pas de permission à refuser — son
        accès tient au seul jeton.

        Le compte n'est jamais supprimé : des commandes passées y renvoient, et
        leur historique comptable doit rester lisible.
        """
        serializer = BlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        customer = self.get_object()
        customer.is_active = False
        customer.save(update_fields=["is_active", "updated_at"])
        AuthService.revoke_all_sessions(customer)

        return Response(CustomerSerializer(customer).data)

    @extend_schema(responses={200: CustomerSerializer}, tags=["accounts"])
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[HasPermission.of("customers.block")],
    )
    def unblock(self, request: Request, pk: str) -> Response:
        """Rouvre un compte. L'utilisateur devra se reconnecter."""
        customer = self.get_object()
        customer.is_active = True
        customer.save(update_fields=["is_active", "updated_at"])
        return Response(CustomerSerializer(customer).data)


class RoleViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Role],
):
    """Rôles — des groupements de permissions, rien de plus.

    Le code ne teste jamais un nom de rôle (ADR-005) : renommer « Manager » ne
    change aucun comportement, et créer « Responsable de nuit » avec trois
    permissions ne demande pas de déploiement.

    **Un rôle système ne se modifie pas et ne se supprime pas.** Retirer
    « Super Admin » d'une instance en production enfermerait tout le monde
    dehors — panne dont on ne se relève qu'en base. La suppression n'est pas
    exposée du tout : un rôle retiré à chaud priverait sans préavis les comptes
    qui le portent, et l'effet ne se verrait qu'au prochain refus.
    """

    serializer_class = RoleSerializer
    permission_classes = (HasReadWritePermission.of(read="roles.read", write="roles.write"),)
    queryset = Role.objects.order_by("name")
    search_fields: ClassVar[list[str]] = ["name"]

    def perform_update(self, serializer: Any) -> None:
        if serializer.instance.is_system:
            raise PermissionDenied(
                "Les rôles fournis à l'installation ne se modifient pas : "
                "créez-en un sur mesure et attribuez-le."
            )
        serializer.save()

    @extend_schema(responses={200: PermissionSerializer(many=True)}, tags=["accounts"])
    @action(detail=False, methods=["get"], url_path="permissions", url_name="permissions")
    def registry(self, request: Request) -> Response:
        """Le registre des permissions, tel qu'il est dans le code.

        Sans cette route, l'écran qui compose un rôle recopierait la liste des
        codes côté client, et les deux divergeraient à la première permission
        ajoutée — une case à cocher qui n'accorde rien, ou une permission
        existante qu'aucun écran ne propose.
        """
        registre = [
            {"code": code, "description": libelle} for code, libelle in sorted(PERMISSIONS.items())
        ]
        return Response(PermissionSerializer(registre, many=True).data, status=status.HTTP_200_OK)
