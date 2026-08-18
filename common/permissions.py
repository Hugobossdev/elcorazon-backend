"""Permissions DRF réutilisables — ADR-005.

Le refus est le défaut : le réglage global du projet est `IsAuthenticated`, et
toute route publique déclare `AllowAny` explicitement. La liste des points
d'entrée ouverts est donc auditable par une simple recherche de `AllowAny`.

Ces classes couvrent les deux premiers étages du modèle — le type de compte et
la permission nommée. Le troisième, l'appartenance de la ressource, vit dans
les `get_queryset` : « ce client ne voit que ses commandes » est un filtre de
requête, pas une permission, sinon un objet interdit serait d'abord chargé puis
refusé, ce qui fuit son existence par le code de statut.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import User, UserType

__all__ = [
    "HasPermission",
    "HasReadWritePermission",
    "IsCourier",
    "IsCustomer",
    "IsOwner",
    "IsStaff",
    "ReadOnly",
    "active_user",
    "assert_unscoped",
    "authenticated_user",
    "is_unscoped",
]


def active_user(request: Request) -> User | None:
    """L'utilisateur de la requête, s'il est authentifié et actif — sinon `None`.

    Renvoyer `None` plutôt qu'un `AnonymousUser` n'est pas une préférence de
    style : `AnonymousUser` n'a ni `user_type` ni `has_permission`, et tout
    code qui l'oublie plante à l'exécution sur une requête non authentifiée —
    c'est-à-dire sur la requête d'un attaquant. Avec `None`, le vérificateur de
    types refuse l'oubli avant la livraison.
    """
    user = getattr(request, "user", None)
    if isinstance(user, User) and user.is_authenticated and user.is_active:
        return user
    return None


def authenticated_user(request: Request) -> User:
    """Le même, dans une vue déjà protégée par une permission.

    La permission a déjà écarté l'anonyme ; ce que la vue doit encore faire,
    c'est le **prouver** au vérificateur de types, sans quoi chaque usage de
    `request.user` traîne un `AnonymousUser` impossible. La levée n'est donc
    pas une garde défensive mais le filet qui attrape une vue publiée par
    inadvertance sans `permission_classes`.
    """
    user = active_user(request)
    if user is None:  # pragma: no cover - la permission de la vue l'a déjà exclu
        raise NotAuthenticated
    return user


def is_unscoped(user: User) -> bool:
    """Vrai pour les comptes qui voient l'enseigne entière.

    Seul le superutilisateur en fait partie. Le rendre explicite évite qu'un
    appelant confonde « aucun rattachement » — un membre du personnel qu'on a
    oublié de rattacher, qui ne doit donc rien voir — avec « tous les
    établissements ». C'est exactement l'ambiguïté qui transforme un oubli de
    configuration en fuite de données.

    Défini dans le socle et non dans `apps.restaurants`, où il vivait
    d'abord : la géographie en a besoin — un pays n'appartient à aucun
    établissement — et elle ne connaît pas les restaurants (ADR-002). Une
    seconde définition, même identique, aurait divergé le jour où la première
    aurait changé.
    """
    return user.is_superuser


def assert_unscoped(user: User, quoi: str) -> None:
    """Réserve une écriture d'enseigne aux comptes non cloisonnés.

    Certains objets ne se rattachent à aucun établissement : un pays, une
    ville, une zone, un code promotionnel national. Le cloisonnement ne peut
    donc rien en dire, et le défaut sûr est le refus — accorder « tout ce qui
    n'a pas de périmètre » à qui a un périmètre étroit serait exactement
    l'élargissement silencieux que l'ADR-005 cherche à empêcher.
    """
    if not is_unscoped(user):
        raise PermissionDenied(
            f"{quoi} relève du siège : votre compte est rattaché à un périmètre."
        )


class _AuthenticatedBase(BasePermission):
    """Facteur commun : un utilisateur non authentifié n'a jamais rien."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        return active_user(request) is not None


class _UserTypePermission(_AuthenticatedBase):
    """Exige un type de compte, déclaré par la sous-classe."""

    user_type: UserType

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = active_user(request)
        return user is not None and user.user_type == self.user_type


class IsCustomer(_UserTypePermission):
    user_type = UserType.CUSTOMER


class IsCourier(_UserTypePermission):
    user_type = UserType.COURIER


class IsStaff(_UserTypePermission):
    user_type = UserType.STAFF


class HasPermission(_AuthenticatedBase):
    """Exige une permission nommée du registre.

    S'utilise en la paramétrant sur la vue :

        permission_classes = [HasPermission.of("orders.refund")]

    La fabrique produit une sous-classe plutôt que d'utiliser un attribut de
    vue : la permission devient visible à l'endroit où on la lit — dans la
    liste `permission_classes` — au lieu d'être un attribut séparé qu'on peut
    oublier de déclarer en ajoutant une action.
    """

    code: str = ""

    @classmethod
    def of(cls, code: str) -> type[HasPermission]:
        return type(f"HasPermission_{code.replace('.', '_')}", (cls,), {"code": code})

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = active_user(request)
        if user is None:
            return False
        if not self.code:  # pragma: no cover - erreur de programmation
            raise ValueError("HasPermission doit être paramétrée par .of('domaine.action').")
        return user.has_permission(self.code)


class HasReadWritePermission(_AuthenticatedBase):
    """Deux permissions nommées sur une même vue : une pour lire, une pour écrire.

    Un écran de back-office a besoin des deux — un opérateur consulte le
    catalogue sans pouvoir le modifier —, et DRF n'offre pour cela que
    `get_permissions()`, qui décide à l'exécution et échappe donc à l'audit
    statique de la surface publique (`tests/architecture/test_layers.py`).
    Paramétrer une classe garde la décision **lisible dans
    `permission_classes`**, là où on la cherche, et laisse l'audit voir ce
    qu'il doit voir.

        permission_classes = [
            HasReadWritePermission.of(read="catalog.read", write="catalog.write")
        ]
    """

    read_code: str = ""
    write_code: str = ""

    @classmethod
    def of(cls, *, read: str, write: str) -> type[HasReadWritePermission]:
        suffixe = f"{read}_{write}".replace(".", "_")
        return type(
            f"HasReadWritePermission_{suffixe}",
            (cls,),
            {"read_code": read, "write_code": write},
        )

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = active_user(request)
        if user is None:
            return False
        code = self.read_code if request.method in SAFE_METHODS else self.write_code
        if not code:  # pragma: no cover - erreur de programmation
            raise ValueError(
                "HasReadWritePermission doit être paramétrée par .of(read=…, write=…)."
            )
        return user.has_permission(code)


class IsOwner(_AuthenticatedBase):
    """L'objet appartient à l'utilisateur.

    L'attribut portant le propriétaire est configurable, car il change selon le
    modèle : `user` sur une adresse, `customer` sur une commande.
    """

    owner_field: str = "user"

    @classmethod
    def through(cls, field: str) -> type[IsOwner]:
        return type(f"IsOwner_{field}", (cls,), {"owner_field": field})

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        owner = obj
        for part in self.owner_field.split("__"):
            owner = getattr(owner, part, None)
            if owner is None:
                return False
        return bool(owner == active_user(request))


class ReadOnly(BasePermission):
    """À combiner : `[IsStaff | ReadOnly]` ouvre la lecture, réserve l'écriture."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        return bool(request.method in SAFE_METHODS)
