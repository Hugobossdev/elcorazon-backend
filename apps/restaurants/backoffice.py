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

import logging
from typing import Any, ClassVar

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
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
from rest_framework.viewsets import GenericViewSet, ModelViewSet, ReadOnlyModelViewSet

from apps.accounts.models import User, UserType
from apps.accounts.services import AuthService
from apps.geography.journal import (
    record_zone_changes,
    record_zone_creation,
    record_zone_deletion,
    zone_fingerprint,
)
from apps.geography.models import DeliveryZone
from apps.restaurants.duplication import SECTION_GENERAL, copy_sections
from apps.restaurants.models import (
    AreaMembership,
    KitchenClosure,
    OpeningHours,
    Restaurant,
    StaffMembership,
    kitchen_state_prefetches,
)
from apps.restaurants.scoping import (
    assert_can_manage,
    assert_can_open_in_zone,
    assert_in_scope,
    is_unscoped,
    staff_restaurant_ids,
)
from apps.restaurants.serializers import (
    AuditEntrySerializer,
    ManagedKitchenClosureSerializer,
    ManagedOpeningHoursSerializer,
    ManagedRestaurantSerializer,
    ManagedRestaurantZoneSerializer,
    RestaurantDuplicationSerializer,
    RestaurantPerimeterSerializer,
    RestaurantStatusTransitionSerializer,
    StaffSerializer,
)
from apps.restaurants.states import RestaurantStatus
from common.audit import AuditAction, AuditEntry, record_change
from common.exceptions import BusinessRuleViolation
from common.permissions import (
    HasPermission,
    HasReadWritePermission,
    IsStaff,
    authenticated_user,
)

__all__ = [
    "ManagedOpeningHoursViewSet",
    "ManagedRestaurantViewSet",
    "ManagedRestaurantZoneViewSet",
    "StaffViewSet",
]

logger = logging.getLogger(__name__)

RESTAURANT_PERMISSION = HasReadWritePermission.of(
    read="restaurants.read", write="restaurants.write"
)

#: Horaires, fermetures et zones propres : le geste courant du gérant, sur ses
#: établissements (le cloisonnement reste celui de chaque vue). `operate` y lit
#: aussi — on ne règle pas des horaires qu'on ne voit pas.
OPERATION_PERMISSION = HasReadWritePermission.of(
    read=("restaurants.read", "restaurants.operate"),
    write=("restaurants.write", "restaurants.operate"),
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
        membre = serializer.save()
        _consigner_personnel(
            authenticated_user(self.request),
            membre,
            avant=_EMPREINTE_VIDE,
            mot_de_passe_change=False,
        )

    def perform_update(self, serializer: Any) -> None:
        # Avant tout le reste : voir un collègue n'est pas en répondre. Sans
        # cette garde, remplacer le mot de passe d'un compte plus puissant — le
        # siège rattaché à l'établissement, un caissier qui rembourse — suffisait
        # à se connecter à sa place.
        assert_can_manage(authenticated_user(self.request), serializer.instance)
        self._assert_grantable(serializer.validated_data)
        etait_actif = serializer.instance.is_active
        avant = _empreinte_du_personnel(serializer.instance)
        membre = serializer.save()

        # La révocation suit la désactivation dans la même requête : les deux
        # séparées, un compte fermé travaillerait jusqu'à l'expiration de son
        # jeton d'accès — quinze minutes pendant lesquelles il peut encore
        # rembourser une commande.
        #
        # Un mot de passe **remplacé** révoque aussi, pour la raison de T2 en
        # plus fort : on le remplace quand il est perdu, ou quand on soupçonne
        # qu'un autre le détient. Sans révocation, celui qui l'avait gardait
        # son jeton de rafraîchissement — trente jours de session.
        mot_de_passe_remplace = bool(serializer.validated_data.get("password"))
        if (etait_actif and not membre.is_active) or mot_de_passe_remplace:
            AuthService.revoke_all_sessions(membre)

        _consigner_personnel(
            authenticated_user(self.request),
            membre,
            avant=avant,
            mot_de_passe_change=mot_de_passe_remplace,
        )

    # --------------------------------------------------------- garde-fous

    def _assert_grantable(self, data: dict[str, Any]) -> None:
        acteur = authenticated_user(self.request)
        if is_unscoped(acteur):
            return

        self._assert_within_scope(acteur, data.get("restaurants"))
        self._assert_areas_within_scope(acteur, data.get("_countries"), data.get("_cities"))
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

    def _assert_areas_within_scope(self, acteur: User, pays: Any, villes: Any) -> None:
        """On n'accorde pas un marché qu'on ne couvre pas soi-même.

        C'est le pendant de `_assert_not_escalating`, sur l'autre axe. Sans
        cette garde, `roles.write` suffirait à s'attribuer un pays entier : le
        rattachement de périmètre est plus large que le rattachement
        d'établissement, et la garde qui protège le second ne dit rien du
        premier.

        Un directeur du Togo peut nommer un responsable de Lomé — la ville est
        dans son marché — mais pas un directeur de Côte d'Ivoire. Le siège, lui,
        n'est pas concerné : `_assert_grantable` sort avant.
        """
        if pays is None and villes is None:
            return

        couverts = AreaMembership.objects.filter(user=acteur)
        pays_couverts = set(couverts.values_list("country_id", flat=True)) - {None}
        villes_couvertes = set(couverts.values_list("city_id", flat=True)) - {None}

        hors_pays = [marche for marche in (pays or []) if marche.pk not in pays_couverts]
        if hors_pays:
            raise PermissionDenied(
                "Marché hors périmètre : " + ", ".join(sorted(marche.name for marche in hors_pays))
            )

        # Une ville est couverte par elle-même **ou par son pays** : un
        # directeur pays qui ne pourrait pas nommer de responsable de ville
        # devrait passer par le siège pour chacune des siennes.
        hors_villes = [
            ville
            for ville in (villes or [])
            if ville.pk not in villes_couvertes and ville.country_id not in pays_couverts
        ]
        if hors_villes:
            raise PermissionDenied(
                "Ville hors périmètre : " + ", ".join(sorted(ville.name for ville in hors_villes))
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


def _empreinte_geographique(etablissement: Restaurant) -> dict[str, Any]:
    """Ce qu'on compare pour décider s'il faut journaliser.

    La position est arrondie à six décimales — onze centimètres environ. En
    deçà, l'écart vient de la représentation en flottant et non d'un geste :
    consigner un « déplacement » de trois millimètres remplirait le journal de
    bruit, et un journal bruyant ne s'ouvre plus.
    """
    return {
        "location": [round(etablissement.location.y, 6), round(etablissement.location.x, 6)],
        "address": etablissement.address,
        "zone": str(etablissement.zone_id),
    }


def _avec_compteurs() -> QuerySet[Restaurant]:
    """Établissements, chacun avec ses trois compteurs d'exploitation.

    `distinct=True` sur les trois, et ce n'est pas une précaution de style :
    trois `Count` sur trois relations inverses dans la même requête produisent
    un produit cartésien, et un établissement de 4 commandes, 2 livreurs et
    10 articles annoncerait 80 de chaque. Le défaut ne se voit pas sur un jeu de
    démonstration où l'une des trois vaut 1.

    Les articles supprimés sont exclus : la suppression du catalogue est douce
    (`deleted_at`), et « 43 produits » dont la moitié ne sont plus à la carte
    n'aide personne à décider si un établissement est prêt à ouvrir.
    """
    return (
        Restaurant.objects.select_related("zone__city__country")
        # Le verdict « commandable maintenant » que rend la fiche lit les plages
        # d'ouverture : sans ce préchargement, une requête de plus par ligne.
        .prefetch_related(*kitchen_state_prefetches())
        .annotate(
            orders_count=Count("orders", distinct=True),
            couriers_count=Count("couriers", distinct=True),
            menu_items_count=Count(
                "items", filter=Q(items__deleted_at__isnull=True), distinct=True
            ),
        )
        .order_by("name")
    )


class ManagedRestaurantViewSet(
    ListModelMixin,
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    GenericViewSet[Restaurant],
):
    """Établissements — ouverture, coordonnées, suspension de la prise de commande.

    **Ouvrir un établissement relève du marché.** Un gérant modifie le sien —
    horaires, téléphone, délai de préparation, « on arrête les commandes une
    heure » — mais n'en crée pas : une création s'attribuerait un périmètre
    qu'on ne lui a pas donné, et le cloisonnement n'aurait plus de sens.

    Le siège ouvre partout ; un directeur de marché ouvre **chez lui**, et
    l'établissement neuf tombe dans son périmètre parce qu'il est dans son pays,
    non parce qu'il vient de le créer (`AreaMembership`). C'est la différence
    qui rend le geste sûr, et c'est pourquoi la garde porte sur la zone visée
    plutôt que sur le compte.

    Aucune suppression. Des commandes, un catalogue et des dossiers livreurs y
    renvoient ; `is_active` retire l'établissement de l'application sans rendre
    l'historique illisible.
    """

    serializer_class = ManagedRestaurantSerializer
    permission_classes = (RESTAURANT_PERMISSION,)
    lookup_field = "slug"
    queryset = _avec_compteurs()
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "zone__city__slug": ["exact"],
        "zone__city__country__iso_code": ["exact"],
        "status": ["exact"],
        "is_active": ["exact"],
        "accepts_orders": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "address"]

    def get_queryset(self) -> QuerySet[Restaurant]:
        user = authenticated_user(self.request)
        base = _avec_compteurs()
        if is_unscoped(user):
            return base
        return base.filter(pk__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        """Ouvrir relève du siège — ou du directeur du marché visé.

        Le contrôle porte sur la **zone**, pas sur l'utilisateur seul : c'est
        elle qui emporte la ville et le pays, donc le périmètre dans lequel
        l'établissement neuf tombera. Un directeur du Togo ouvre à Kara ; il
        n'ouvre pas à Abidjan.
        """
        assert_can_open_in_zone(authenticated_user(self.request), serializer.validated_data["zone"])
        etablissement = serializer.save()
        record_change(
            actor=authenticated_user(self.request),
            action=AuditAction.RESTAURANT_CREATE,
            target_type="restaurant",
            target_id=etablissement.pk,
            target_label=etablissement.name,
            before={},
            after={"status": etablissement.status, "zone": str(etablissement.zone_id)},
            scope_restaurant_id=etablissement.pk,
        )

    def perform_update(self, serializer: Any) -> None:
        avant = _empreinte_geographique(serializer.instance)

        # L'établissement est déjà dans le périmètre — `get_queryset` l'a
        # filtré. Ce qui reste à garder, c'est la zone : la changer change la
        # ville, donc le pays, donc la devise et le barème. Un gérant corrige
        # ses horaires, il ne déménage pas son restaurant dans un autre marché.
        zone = serializer.validated_data.get("zone")
        if zone is not None and zone.pk != serializer.instance.zone_id:
            # Déplacer un restaurant vers une autre zone, c'est l'ouvrir dans le
            # marché d'arrivée : même écriture, donc même garde. Un gérant
            # corrige ses horaires, il ne déménage pas son restaurant dans un
            # autre marché — et un directeur pays ne l'exporte pas hors du sien.
            assert_can_open_in_zone(authenticated_user(self.request), zone)

        etablissement = serializer.save()
        self._journaliser(avant, etablissement)

    def _journaliser(self, avant: dict[str, Any], etablissement: Restaurant) -> None:
        """Consigne un déplacement ou un changement de marché.

        Deux entrées distinctes plutôt qu'une : déplacer un restaurant de trois
        cents mètres et le rattacher à une autre zone n'ont ni la même cause ni
        les mêmes conséquences — le premier corrige une saisie, le second change
        la devise et le barème. Les fondre en « géographie modifiée » obligerait
        à lire les deux valeurs pour savoir laquelle a bougé.

        Rien n'est écrit quand rien n'a changé : un formulaire renvoie tous ses
        champs à chaque validation, et corriger un numéro de téléphone
        consignerait sinon « position inchangée » à chaque fois.
        """
        acteur = authenticated_user(self.request)
        apres = _empreinte_geographique(etablissement)

        if avant["location"] != apres["location"]:
            record_change(
                actor=acteur,
                action=AuditAction.RESTAURANT_LOCATION,
                target_type="restaurant",
                target_id=etablissement.pk,
                target_label=etablissement.name,
                before={"location": avant["location"], "address": avant["address"]},
                after={"location": apres["location"], "address": apres["address"]},
            )

        if avant["zone"] != apres["zone"]:
            record_change(
                actor=acteur,
                action=AuditAction.RESTAURANT_ZONE,
                target_type="restaurant",
                target_id=etablissement.pk,
                target_label=etablissement.name,
                before={"zone": avant["zone"]},
                after={"zone": apres["zone"]},
            )

    # ------------------------------------------------------------ périmètre

    @extend_schema(
        responses={200: RestaurantPerimeterSerializer(many=True)},
        tags=["restaurants"],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="perimeter",
        url_name="perimeter",
        permission_classes=[IsStaff],
        pagination_class=None,
    )
    def perimeter(self, request: Request) -> Response:
        """Les établissements que **ce compte** supervise — sans `restaurants.read`.

        Tout compte du personnel a besoin de savoir où il travaille : le poste
        de cuisine, le stock et les recettes sont ceux d'une cuisine. Réserver
        cette réponse à `restaurants.read` rendait le poste inutilisable pour
        le rôle même qui le tient (« Opérateur »), sans rien protéger : le
        cloisonnement ne change pas, seuls les établissements du périmètre
        sortent, et sous une forme réduite (`RestaurantPerimeterSerializer`).

        Non paginé : un périmètre est borné par la taille du réseau, et l'écran
        doit l'avoir entier pour proposer le sélecteur. Les établissements en
        service viennent en tête — le premier est celui qu'on ouvre par défaut,
        et ouvrir la cuisine d'un établissement fermé n'a pas de sens.
        """
        user = authenticated_user(request)
        base = Restaurant.objects.select_related("zone__city__country")
        if not is_unscoped(user):
            base = base.filter(pk__in=staff_restaurant_ids(user))
        etablissements = base.order_by(
            "-is_active", "zone__city__country__iso_code", "zone__city__name", "name"
        )
        return Response(RestaurantPerimeterSerializer(etablissements, many=True).data)

    # ------------------------------------------------------- cycle de vie

    @extend_schema(
        request=RestaurantStatusTransitionSerializer,
        responses={200: ManagedRestaurantSerializer},
        tags=["restaurants"],
    )
    @action(detail=True, methods=["post"], url_path="status", url_name="status")
    def update_status(self, request: Request, slug: str) -> Response:
        """Fait avancer l'établissement dans son cycle de vie.

        Route à part du `PATCH` de la fiche, et non un champ de plus : publier
        un établissement et corriger son numéro de téléphone ne sont pas le même
        geste, n'appellent pas les mêmes vérifications, et n'ont pas à partager
        un formulaire. Fondus dans le `PATCH`, la machine à états et le contrôle
        de complétude s'exécuteraient à chaque enregistrement d'un champ de
        contact.

        **La mise en service relève du marché.** Le reste du cycle — passer en
        configuration, se déclarer prêt, suspendre pour la journée — est ouvert
        au gérant de l'établissement : ce sont les gestes de l'exploitation.
        Ouvrir au public ne l'est pas, pour la même raison que la création :
        cela engage l'enseigne. Le siège et le directeur du marché où se trouve
        l'établissement le peuvent ; un gérant, non.

        Une transition refusée sort en 409 avec les cibles autorisées
        (`IllegalTransition`), et un établissement incomplet en 409 avec la
        liste de ce qui manque (`IncompleteConfiguration`) — deux réponses
        distinctes, parce que les deux appellent deux gestes distincts.
        """
        payload = RestaurantStatusTransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        cible = payload.validated_data["status"]

        etablissement = self.get_object()

        if cible == RestaurantStatus.ACTIVE:
            # Publier engage l'enseigne : réservé au siège, ou au directeur du
            # marché où se trouve l'établissement. Un gérant, lui, dispose du
            # reste du cycle — passer en configuration, se déclarer prêt,
            # suspendre pour la journée : ce sont les gestes de l'exploitation.
            assert_can_open_in_zone(
                authenticated_user(request),
                etablissement.zone,
                "La mise en service d'un établissement",
            )

        avant = etablissement.status
        etablissement.transition_to(cible)
        # Après la transition, qui lève si elle est refusée : une tentative
        # rejetée n'a rien décidé, et ne s'écrit pas.
        record_change(
            actor=authenticated_user(request),
            action=AuditAction.RESTAURANT_STATUS,
            target_type="restaurant",
            target_id=etablissement.pk,
            target_label=etablissement.name,
            before={"status": avant},
            after={"status": cible},
            scope_restaurant_id=etablissement.pk,
        )
        return Response(ManagedRestaurantSerializer(etablissement).data)

    @extend_schema(
        request=RestaurantDuplicationSerializer,
        responses={201: ManagedRestaurantSerializer},
        tags=["restaurants"],
    )
    @action(detail=True, methods=["post"], url_path="duplicate", url_name="duplicate")
    def duplicate(self, request: Request, slug: str) -> Response:
        """Ouvre un établissement en repartant d'un autre.

        **Relève du siège**, comme la création dont c'est une variante : un
        compte cloisonné qui dupliquerait s'attribuerait un second périmètre en
        une requête, ce qui viderait le cloisonnement de son sens.

        Le nouvel établissement naît en **brouillon**, quel que soit l'état de
        la source. Hériter d'« en service » publierait une fiche dont personne
        n'a encore vérifié l'adresse, et la carte recopiée lui donnerait
        justement l'air complète.

        Tout se fait dans une transaction : une duplication interrompue à
        mi-chemin laisserait un établissement avec la moitié de sa carte, état
        qu'aucun écran ne sait montrer comme anormal.
        """
        payload = RestaurantDuplicationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        donnees = payload.validated_data

        # Dupliquer, c'est ouvrir : même garde que la création, et sur la zone
        # d'arrivée. Elle est vérifiée **avant** de lire la source, pour qu'un
        # refus de périmètre ne dépende pas de l'existence du modèle.
        assert_can_open_in_zone(authenticated_user(request), donnees["zone"])

        source = self.get_object()
        sections = set(donnees["sections"])
        self._assert_meme_devise(source, donnees["zone"], sections)

        with transaction.atomic():
            cible = Restaurant.objects.create(
                name=donnees["name"],
                slug=donnees["slug"],
                zone=donnees["zone"],
                address=donnees["address"],
                location=donnees["location"],
                phone=donnees["phone"],
                email=donnees.get("email", ""),
                # Les champs de « informations générales », copiés seulement
                # s'ils sont demandés. Le délai de préparation en fait partie :
                # c'est une caractéristique de la cuisine, pas de l'enseigne.
                description=source.description if SECTION_GENERAL in sections else "",
                default_preparation_minutes=(
                    source.default_preparation_minutes
                    if SECTION_GENERAL in sections
                    else Restaurant._meta.get_field("default_preparation_minutes").default
                ),
                status=RestaurantStatus.DRAFT,
            )
            copies = copy_sections(source=source, cible=cible, sections=sections)

        # Relu à travers `_avec_compteurs` pour que la réponse porte les mêmes
        # champs qu'une lecture ordinaire — sans quoi l'écran qui reçoit la
        # fiche créée afficherait « — » là où la liste affiche « 0 ».
        cible = _avec_compteurs().get(pk=cible.pk)
        corps = ManagedRestaurantSerializer(cible).data
        return Response({**corps, "copied": copies}, status=201)

    @staticmethod
    def _assert_meme_devise(source: Restaurant, zone: Any, sections: set[str]) -> None:
        """Refuse de recopier une carte dans une autre devise.

        Un prix est un montant **et** une devise (ADR-007). Recopier 2 500 XOF
        vers un marché en NGN produirait des articles à 2 500 nairas — un prix
        plausible, faux d'un facteur cinq, et que rien n'afficherait comme
        anormal puisque la fiche serait par ailleurs complète.

        Convertir automatiquement serait pire : le taux du jour n'a pas à
        décider d'une politique tarifaire. Le refus est explicite, et la
        duplication reste possible sans la carte — c'est ce que dit le message.
        """
        if not sections - {SECTION_GENERAL}:
            return
        devise_source = source.currency
        devise_cible = zone.city.country.currency
        if devise_source != devise_cible:
            raise BusinessRuleViolation(
                f"« {source.name} » facture en {devise_source} et la zone visée "
                f"en {devise_cible}. Une carte recopiée garderait ses montants "
                "sans changer d'unité. Dupliquez sans le catalogue, puis "
                "saisissez les prix du nouveau marché.",
                source_currency=devise_source,
                target_currency=devise_cible,
            )


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
    permission_classes = (OPERATION_PERMISSION,)
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


class ManagedKitchenClosureViewSet(ModelViewSet[KitchenClosure]):
    """Fermetures exceptionnelles — `/restaurants/manage/closures/`.

    Même permission et même cloisonnement que les horaires, dont c'est
    l'exception datée. La suppression est réelle, comme pour une plage : une
    fermeture annulée n'a jamais fermé la cuisine, et une fermeture passée
    reste lisible tant qu'on ne l'efface pas.

    `?upcoming=true` ne rend que ce qui n'est pas terminé — ce que l'écran
    affiche par défaut ; l'historique complet reste accessible sans filtre.
    """

    serializer_class = ManagedKitchenClosureSerializer
    permission_classes = (OPERATION_PERMISSION,)
    queryset = KitchenClosure.objects.none()
    filterset_fields: ClassVar[dict[str, list[str]]] = {"restaurant": ["exact"]}

    def get_queryset(self) -> QuerySet[KitchenClosure]:
        user = authenticated_user(self.request)
        base = KitchenClosure.objects.select_related("restaurant").order_by("starts_at")
        if str(self.request.query_params.get("upcoming", "")).lower() == "true":
            base = base.filter(ends_at__gt=timezone.now())
        if is_unscoped(user):
            return base
        return base.filter(restaurant_id__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        acteur = authenticated_user(self.request)
        restaurant = serializer.validated_data["restaurant"]
        assert_in_scope(acteur, restaurant.pk)
        fermeture = serializer.save(created_by=acteur)
        logger.info(
            "kitchen.closure.created",
            extra={
                "kitchen": restaurant.slug,
                "starts_at": fermeture.starts_at.isoformat(),
                "ends_at": fermeture.ends_at.isoformat(),
            },
        )

    def perform_update(self, serializer: Any) -> None:
        restaurant = serializer.validated_data.get("restaurant")
        if restaurant is not None:
            assert_in_scope(authenticated_user(self.request), restaurant.pk)
        serializer.save()

    def perform_destroy(self, instance: KitchenClosure) -> None:
        logger.info("kitchen.closure.deleted", extra={"kitchen": instance.restaurant.slug})
        instance.delete()


def _etablissement_proprietaire(zone: DeliveryZone) -> Restaurant:
    """L'établissement d'une zone propre — jamais nul sur ce jeu de requête.

    `DeliveryZone.restaurant` est nullable : une zone sans établissement est
    **municipale** et vaut pour toutes les cuisines de sa ville. Le jeu de
    requête de la vue ci-dessous les écarte (`restaurant__isnull=False`), si
    bien que l'attribut y est toujours renseigné.

    Le vérificateur de types, lui, ne lit pas ce filtre. Écrire
    `zone.restaurant.name` directement le laisserait passer — et le jour où
    quelqu'un élargirait le jeu de requête, la suppression d'une zone
    municipale rendrait un 500 au lieu d'un refus lisible.

    La levée n'est donc pas une garde défensive : c'est le filet qui attrape
    cet élargissement-là.
    """
    if zone.restaurant is None:  # pragma: no cover - le jeu de requête l'exclut
        raise BusinessRuleViolation(
            f"La zone « {zone.name} » est municipale : elle vaut pour toute la "
            "ville et ne se gère pas depuis les zones propres d'un établissement.",
            code="municipal_zone",
        )
    return zone.restaurant


class ManagedRestaurantZoneViewSet(ModelViewSet[DeliveryZone]):
    """Zones **propres à un établissement** — `/restaurants/manage/zones/`.

    ## Pourquoi ces zones-là vivent ici

    Une zone municipale s'administre depuis la géographie : elle appartient à
    une ville, elle vaut pour toutes ses cuisines, et son ouverture relève du
    siège. Une zone propre à un établissement est un autre geste — c'est le
    gérant qui décide jusqu'où *sa* cuisine livre — et elle ne peut pas
    s'écrire depuis `geography`, qui n'a pas le droit de connaître les
    établissements (ADR-002).

    Les deux partagent le même contrat, par héritage de sérialiseur : les trois
    modes de saisie, la validation de devise et le calcul du contour sont écrits
    une fois. Ce qui s'ajoute ici est le rattachement, et la vérification qu'il
    a un sens — une zone de Douala ne se rattache pas à la cuisine de Lomé.

    ## Le cloisonnement s'applique

    Un gérant ne voit et n'écrit que les zones de ses établissements. Le
    contrôle passe par `staff_restaurant_ids`, le même point que partout
    ailleurs : une zone est un levier tarifaire, et l'ouvrir plus largement que
    les commandes n'aurait aucun sens.

    ## La suppression est réelle

    Contrairement aux établissements et aux villes, une zone d'établissement
    n'est référencée par rien : les commandes figent leurs montants, elles ne
    pointent pas la zone qui les a produits. La retirer ne rend donc aucun
    historique illisible, et une zone désactivée qu'on ne pourrait pas effacer
    encombrerait l'écran qui sert à en dessiner.
    """

    serializer_class = ManagedRestaurantZoneSerializer
    permission_classes = (OPERATION_PERMISSION,)
    queryset = (
        DeliveryZone.objects.filter(restaurant__isnull=False)
        .select_related("city__country", "restaurant")
        .order_by("restaurant__name", "name")
    )
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "restaurant__slug": ["exact"],
        "city__slug": ["exact"],
        "shape": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name"]

    def get_queryset(self) -> QuerySet[DeliveryZone]:
        user = authenticated_user(self.request)
        base = (
            DeliveryZone.objects.filter(restaurant__isnull=False)
            .select_related("city__country", "restaurant")
            .order_by("restaurant__name", "name")
        )
        if is_unscoped(user):
            return base
        return base.filter(restaurant_id__in=staff_restaurant_ids(user))

    def perform_create(self, serializer: Any) -> None:
        assert_in_scope(
            authenticated_user(self.request), serializer.validated_data["restaurant"].pk
        )
        zone = serializer.save()
        record_zone_creation(authenticated_user(self.request), zone)

    def perform_update(self, serializer: Any) -> None:
        # Deux périmètres à garder, et non un : celui de la zone telle qu'elle
        # est, et celui de l'établissement vers lequel on voudrait la déplacer.
        # Ne vérifier que le premier laisserait un gérant offrir sa zone — donc
        # son barème — à une cuisine qu'il n'administre pas.
        acteur = authenticated_user(self.request)
        assert_in_scope(acteur, serializer.instance.restaurant_id)
        cible = serializer.validated_data.get("restaurant")
        if cible is not None:
            assert_in_scope(acteur, cible.pk)

        avant = zone_fingerprint(serializer.instance)
        zone = serializer.save()
        record_zone_changes(acteur, avant, zone)

    def perform_destroy(self, instance: DeliveryZone) -> None:
        etablissement = _etablissement_proprietaire(instance)
        assert_in_scope(authenticated_user(self.request), etablissement.pk)
        # Une cuisine peut être posée sur l'une de ses propres zones
        # (`zone_anchoring_problem`). `Restaurant.zone` est `PROTECT` : la base
        # refusait la suppression en `ProtectedError`, donc en 500. On dit
        # plutôt ce qui bloque, et quoi faire.
        portees = list(Restaurant.objects.filter(zone=instance).values_list("name", flat=True))
        if portees:
            raise BusinessRuleViolation(
                f"La zone « {instance.name} » porte l'établissement « {', '.join(portees)} » : "
                "rattachez-le à une autre zone avant de la supprimer.",
                zone=str(instance.pk),
            )
        record_zone_deletion(authenticated_user(self.request), instance)
        instance.delete()


# ------------------------------------------------ journal du personnel

_EMPREINTE_VIDE: dict[str, Any] = {
    "roles": [],
    "restaurants": [],
    "countries": [],
    "cities": [],
    "is_active": None,
}


def _empreinte_du_personnel(membre: User) -> dict[str, Any]:
    """Ce qu'un compte du personnel peut faire, et sur quoi — lu en base.

    Les rôles par leur **nom** : c'est ce qu'on lit dans un journal, et un
    identifiant ne dirait rien le jour où le rôle aura été vidé.
    """
    zones = AreaMembership.objects.filter(user=membre).select_related("country", "city")
    return {
        "roles": sorted(membre.roles.values_list("name", flat=True)),
        "restaurants": sorted(
            StaffMembership.objects.filter(user=membre).values_list("restaurant__slug", flat=True)
        ),
        "countries": sorted(z.country.iso_code for z in zones if z.country is not None),
        "cities": sorted(z.city.slug for z in zones if z.city is not None),
        "is_active": membre.is_active,
    }


def _consigner_personnel(
    acteur: User, membre: User, *, avant: dict[str, Any], mot_de_passe_change: bool
) -> None:
    """Journalise rôles, périmètre, activation et remplacement de mot de passe.

    Trois entrées distinctes plutôt qu'une : « qui a donné `Manager` à Kofi »
    et « qui l'a rattaché à Lomé » sont deux questions, qu'on pose séparément.
    `record_change` n'écrit rien pour ce qui n'a pas bougé.
    """
    apres = _empreinte_du_personnel(membre)

    def consigner(action: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        record_change(
            actor=acteur,
            action=action,
            target_type="staff",
            target_id=membre.pk,
            target_label=membre.email,
            before=before,
            after=after,
        )

    perimetre = ("restaurants", "countries", "cities")
    consigner(AuditAction.STAFF_ROLES, {"roles": avant["roles"]}, {"roles": apres["roles"]})
    consigner(
        AuditAction.STAFF_SCOPE,
        {k: avant[k] for k in perimetre},
        {k: apres[k] for k in perimetre},
    )
    if avant["is_active"] is not None:
        consigner(
            AuditAction.STAFF_ACTIVATION,
            {"is_active": avant["is_active"]},
            {"is_active": apres["is_active"]},
        )
    if mot_de_passe_change:
        consigner(AuditAction.STAFF_PASSWORD, {}, {"password": "remplacé"})


# ------------------------------------------------------------ journal


class AuditEntryViewSet(ReadOnlyModelViewSet[AuditEntry]):
    """`/restaurants/audit/` — le journal des décisions, en lecture.

    Il était écrit à chaque changement de barème, de zone ou d'emplacement, et
    ne se lisait **nulle part** : ni route, ni administration Django. Le jour
    où les frais d'un quartier changeaient sans explication, la trace existait
    et personne ne pouvait l'ouvrir.

    ## Cloisonnement

    Le siège lit tout. Un compte rattaché lit ce qui touche **son** périmètre :
    ses établissements, leurs zones, le personnel qui y est rattaché, et toute
    entrée qui porte l'un de ses établissements (`scope_restaurant_id`) — un
    retrait livreur constaté, un remboursement, une réclamation tranchée, un
    avis masqué. Les rôles, les clients et les pays ne relèvent d'aucun
    établissement : ils ne se lisent qu'au siège — le défaut sûr, comme
    `assert_unscoped` pour les écritures.

    Le périmètre d'une entrée est **écrit au moment de la décision** plutôt que
    déduit de sa cible : `restaurants` n'a pas le droit de connaître `payments`
    ni `support` (ADR-002), et une trace qui se déduirait de l'objet suivrait un
    livreur muté ailleurs au lieu de rester où le versement a été signé.

    Il vit ici parce que c'est ici que vit le périmètre (`scoping`) ; le modèle,
    lui, est dans `common`, qui ne connaît pas les établissements (ADR-002).
    """

    serializer_class = AuditEntrySerializer
    permission_classes = (HasPermission.of("audit.read"),)
    queryset = AuditEntry.objects.none()  # pour le générateur de schéma
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "action": ["exact", "startswith"],
        "target_type": ["exact"],
        "target_id": ["exact"],
        "actor": ["exact"],
        "created_at": ["gte", "lte"],
    }
    search_fields: ClassVar[list[str]] = ["target_label", "actor__full_name", "action"]

    def get_queryset(self) -> QuerySet[AuditEntry]:
        user = authenticated_user(self.request)
        base = AuditEntry.objects.select_related("actor").order_by("-created_at")
        if is_unscoped(user):
            return base

        etablissements = set(staff_restaurant_ids(user))
        zones = Restaurant.objects.filter(pk__in=etablissements).values_list("zone_id", flat=True)
        personnel = StaffMembership.objects.filter(restaurant_id__in=etablissements).values_list(
            "user_id", flat=True
        )
        # `target_id` est une chaîne (le journal survit à ce qu'il décrit, sans
        # clé étrangère) : les identifiants sont comparés sous cette forme.
        return base.filter(
            Q(target_type="restaurant", target_id__in=[str(pk) for pk in etablissements])
            | Q(target_type="zone", target_id__in=[str(pk) for pk in zones])
            | Q(target_type="staff", target_id__in=[str(pk) for pk in personnel])
            | Q(scope_restaurant_id__in=etablissements)
        )
