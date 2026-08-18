"""Administration du personnel — ADR-005.

Le rattachement d'un membre du personnel à un établissement vit dans
`restaurants` et non dans `accounts` : c'est l'établissement qui a du personnel,
et `accounts` est le socle dont tout le reste dépend. Lui faire connaître les
restaurants inverserait le graphe de l'ADR-002 — un test le refuse.

Ce module porte donc la vue d'ensemble d'un compte du personnel : ses rôles
(donc ce qu'il sait faire) **et** ses rattachements (donc sur quoi). Les deux
au même endroit, parce que c'est ainsi qu'on embauche : les séparer en deux
écrans laisserait régulièrement des comptes avec des permissions et aucun
établissement — des gens qui ne voient rien et ne comprennent pas pourquoi.

Deux garde-fous y sont tenus par le code :

* **on n'accorde pas ce qu'on n'a pas.** Un gérant ne peut pas attribuer un
  rôle portant une permission qu'il ne détient pas lui-même, sans quoi la
  moindre permission d'administration vaudrait « Super Admin » en deux
  requêtes ;
* **on ne rattache qu'à son périmètre.** Un gérant de Lomé n'embauche pas pour
  Kara.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from apps.accounts.models import User, UserType
from apps.accounts.services import AuthService
from apps.restaurants.models import OpeningHours, Restaurant
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from apps.restaurants.serializers import (
    ManagedOpeningHoursSerializer,
    ManagedRestaurantSerializer,
    StaffSerializer,
)
from common.permissions import (
    HasReadWritePermission,
    assert_unscoped,
    authenticated_user,
)

__all__ = ["ManagedOpeningHoursViewSet", "ManagedRestaurantViewSet", "StaffViewSet"]

RESTAURANT_PERMISSION = HasReadWritePermission.of(
    read="restaurants.read", write="restaurants.write"
)


class StaffViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[User],
):
    """Comptes du personnel : rôles et rattachements.

    Pas de suppression : un compte du personnel a signé des transitions de
    statut, des remboursements, des validations de dossier livreur, et son
    identifiant figure dans ces journaux. On le **désactive** — ce qui révoque
    ses jetons dans la foulée, sans quoi il continuerait de travailler jusqu'à
    l'expiration du sien.
    """

    serializer_class = StaffSerializer
    permission_classes = (HasReadWritePermission.of(read="roles.read", write="roles.write"),)
    queryset = User.objects.filter(user_type=UserType.STAFF).order_by("full_name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {"is_active": ["exact"]}
    search_fields: ClassVar[list[str]] = ["email", "full_name"]

    def get_queryset(self) -> QuerySet[User]:
        user = authenticated_user(self.request)
        base = (
            User.objects.filter(user_type=UserType.STAFF)
            .prefetch_related("roles", "staff_memberships__restaurant")
            .order_by("full_name")
        )
        if is_unscoped(user):
            return base
        # Un gérant voit les collègues de ses établissements. `distinct` parce
        # qu'un membre rattaché à deux des siens sortirait deux fois.
        return base.filter(
            staff_memberships__restaurant_id__in=staff_restaurant_ids(user)
        ).distinct()

    # ------------------------------------------------------------- écritures

    def perform_create(self, serializer: Any) -> None:
        self._assert_grantable(serializer.validated_data)
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        self._assert_grantable(serializer.validated_data)
        etait_actif = serializer.instance.is_active
        membre = serializer.save()

        # La révocation suit la désactivation dans la même requête : les deux
        # séparées, un compte fermé travaillerait jusqu'à l'expiration de son
        # jeton d'accès — quinze minutes pendant lesquelles il peut encore
        # rembourser une commande.
        if etait_actif and not membre.is_active:
            AuthService.revoke_all_sessions(membre)

    # --------------------------------------------------------- garde-fous

    def _assert_grantable(self, data: dict[str, Any]) -> None:
        acteur = authenticated_user(self.request)
        if is_unscoped(acteur):
            return

        self._assert_within_scope(acteur, data.get("restaurants"))
        self._assert_not_escalating(acteur, data.get("roles"))

    def _assert_within_scope(self, acteur: User, restaurants: Any) -> None:
        if restaurants is None:
            return
        autorises = staff_restaurant_ids(acteur)
        hors = [r for r in restaurants if r.pk not in autorises]
        if hors:
            raise PermissionDenied(
                "Rattachement hors périmètre : "
                + ", ".join(sorted(etablissement.name for etablissement in hors))
            )

    def _assert_not_escalating(self, acteur: User, roles: Any) -> None:
        """On n'accorde pas une permission qu'on ne détient pas.

        Sans cette garde, `roles.write` — la permission qui compose les rôles —
        vaudrait « Super Admin » en deux requêtes : créer un rôle portant tout
        le registre, puis se l'attribuer. Le registre fermé de l'ADR-005 ne
        protège que des permissions inventées, pas de celles qu'on s'accorde.
        """
        if roles is None:
            return
        detenues = acteur.permission_codes()
        accordees = {code for role in roles for code in role.permissions}
        excedent = sorted(accordees - detenues)
        if excedent:
            raise PermissionDenied(
                "On n'accorde pas une permission qu'on ne détient pas soi-même : "
                + ", ".join(excedent)
            )


class ManagedRestaurantViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Restaurant],
):
    """Établissements — ouverture, coordonnées, suspension de la prise de commande.

    **Ouvrir un établissement relève du siège.** Un gérant modifie le sien —
    horaires, téléphone, délai de préparation, « on arrête les commandes une
    heure » — mais n'en crée pas : une création s'attribuerait un périmètre
    qu'on ne lui a pas donné, et le cloisonnement n'aurait plus de sens.

    Aucune suppression. Des commandes, un catalogue et des dossiers livreurs y
    renvoient ; `is_active` retire l'établissement de l'application sans rendre
    l'historique illisible.
    """

    serializer_class = ManagedRestaurantSerializer
    permission_classes = (RESTAURANT_PERMISSION,)
    lookup_field = "slug"
    queryset = Restaurant.objects.select_related("zone__city__country").order_by("name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "zone__city__slug": ["exact"],
        "is_active": ["exact"],
        "accepts_orders": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "address"]

    def get_queryset(self) -> QuerySet[Restaurant]:
        user = authenticated_user(self.request)
        base = Restaurant.objects.select_related("zone__city__country").order_by("name")
        if is_unscoped(user):
            return base
        return base.filter(pk__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        assert_unscoped(authenticated_user(self.request), "L'ouverture d'un établissement")
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        # L'établissement est déjà dans le périmètre — `get_queryset` l'a
        # filtré. Ce qui reste à garder, c'est la zone : la changer change la
        # ville, donc le pays, donc la devise et le barème. Un gérant corrige
        # ses horaires, il ne déménage pas son restaurant dans un autre marché.
        zone = serializer.validated_data.get("zone")
        if zone is not None and zone.pk != serializer.instance.zone_id:
            assert_unscoped(
                authenticated_user(self.request), "Le changement de zone d'un établissement"
            )
        serializer.save()


class ManagedOpeningHoursViewSet(ModelViewSet[OpeningHours]):
    """Plages d'ouverture d'un établissement.

    Ressource à part entière plutôt que liste imbriquée dans l'établissement :
    on ajoute une plage, on en corrige une, on en supprime une — trois gestes
    unitaires qu'un `PUT` de la semaine entière transformerait en réécriture
    complète, avec le risque d'effacer ce qu'un collègue vient de saisir.

    C'est la seule ressource de back-office où la **suppression est réelle** :
    une plage horaire n'est référencée par rien, et une plage désactivée qui
    resterait affichée dans un tableau hebdomadaire serait plus déroutante
    qu'utile.
    """

    serializer_class = ManagedOpeningHoursSerializer
    permission_classes = (RESTAURANT_PERMISSION,)
    queryset = OpeningHours.objects.select_related("restaurant").order_by("weekday", "opens_at")
    filterset_fields: ClassVar[dict[str, list[str]]] = {"restaurant": ["exact"]}

    def get_queryset(self) -> QuerySet[OpeningHours]:
        user = authenticated_user(self.request)
        base = OpeningHours.objects.select_related("restaurant").order_by("weekday", "opens_at")
        if is_unscoped(user):
            return base
        return base.filter(restaurant_id__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        assert_in_scope(
            authenticated_user(self.request), serializer.validated_data["restaurant"].pk
        )
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        restaurant = serializer.validated_data.get("restaurant")
        if restaurant is not None:
            assert_in_scope(authenticated_user(self.request), restaurant.pk)
        serializer.save()
