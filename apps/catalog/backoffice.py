"""Administration du catalogue — l'API que consomme l'application `admin`.

Le back-office Django reste l'outil d'exploitation (retrouver, corriger,
valider) ; ces routes-ci sont ce que l'application Flutter appelle pour tenir la
carte au quotidien : ouvrir un article, changer un prix, refaire le stock du
matin.

Trois choses les distinguent des routes publiques du même domaine :

* elles **montrent ce que le client ne voit pas** — catégorie désactivée,
  article indisponible ou archivé. Filtrer ici comme on filtre côté client
  ferait disparaître de l'écran l'objet même qu'on vient y réactiver ;
* elles exigent **deux permissions distinctes** : `catalog.read` pour consulter,
  `catalog.write` pour modifier. Un opérateur consulte la carte sans pouvoir la
  changer ;
* elles sont **cloisonnées par établissement** (ADR-005, troisième étage). La
  lecture est filtrée — un article hors périmètre est introuvable, pas
  interdit — et l'écriture est refusée explicitement, puisqu'une création
  désigne son établissement dans le corps de la requête et n'a donc aucun objet
  existant à cacher.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.catalog.models import Category, MenuItem, Option, OptionGroup, OptionTemplate
from apps.catalog.serializers import (
    ApplyTemplateSerializer,
    ManagedCategorySerializer,
    ManagedMenuItemSerializer,
    ManagedOptionGroupSerializer,
    ManagedOptionSerializer,
    ManagedOptionTemplateSerializer,
    OptionGroupSerializer,
    StockSerializer,
)
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.exceptions import BusinessRuleViolation
from common.permissions import HasReadWritePermission, authenticated_user

__all__ = [
    "ManagedCategoryViewSet",
    "ManagedMenuItemViewSet",
    "ManagedOptionGroupViewSet",
    "ManagedOptionTemplateViewSet",
    "ManagedOptionViewSet",
]

#: Lire le catalogue et le modifier ne sont pas le même métier.
CATALOG_PERMISSION = HasReadWritePermission.of(read="catalog.read", write="catalog.write")


class _ScopedCatalogViewSet[Model: (Category, MenuItem, OptionGroup, Option, OptionTemplate)](
    ModelViewSet[Model]
):
    """Facteur commun des quatre ressources : permissions et cloisonnement.

    Le chemin qui mène de l'objet à son établissement change d'une ressource à
    l'autre — direct sur une catégorie, à deux jointures sur une option — d'où
    `restaurant_path`. Le reste est identique, et le répéter quatre fois
    laisserait la quatrième copie diverger le jour où l'on ajoute une règle.
    """

    # En n-uplet et non en liste : `permission_classes` est déclarée comme
    # variable d'instance sur `APIView`, et l'annoter `ClassVar` — ce que
    # réclamerait une liste mutable — est refusé par le vérificateur de types.
    # Un n-uplet est immuable, donc les deux outils sont satisfaits, et DRF ne
    # fait que l'itérer.
    permission_classes = (CATALOG_PERMISSION,)

    #: Chemin ORM du restaurant depuis cette ressource.
    restaurant_path: str = "restaurant"

    def filter_queryset_by_scope(self, queryset: QuerySet[Model]) -> QuerySet[Model]:
        user = authenticated_user(self.request)
        if is_unscoped(user):
            return queryset
        return queryset.filter(**{f"{self.restaurant_path}__in": staff_restaurant_ids(user)})


class ManagedCategoryViewSet(_ScopedCatalogViewSet[Category]):
    """Catégories, actives comme désactivées."""

    serializer_class = ManagedCategorySerializer
    queryset = Category.objects.select_related("restaurant").order_by("sort_order", "name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "restaurant__slug": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name"]

    def get_queryset(self) -> QuerySet[Category]:
        return self.filter_queryset_by_scope(Category.objects.select_related("restaurant"))

    def perform_create(self, serializer: Any) -> None:
        assert_in_scope(
            authenticated_user(self.request), serializer.validated_data["restaurant"].pk
        )
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        # Le périmètre est vérifié sur la **cible** autant que sur la source :
        # sans cela, un opérateur déplacerait une catégorie de son
        # établissement vers un autre, où il n'écrit pas.
        restaurant = serializer.validated_data.get("restaurant")
        if restaurant is not None:
            assert_in_scope(authenticated_user(self.request), restaurant.pk)
        serializer.save()


@extend_schema(
    parameters=[
        OpenApiParameter(
            name="archived",
            type=bool,
            description=(
                "Rend les articles archivés au lieu des articles vivants. "
                "Un article archivé reste lisible depuis les commandes passées."
            ),
        )
    ],
    tags=["catalog"],
)
class ManagedMenuItemViewSet(_ScopedCatalogViewSet[MenuItem]):
    """Articles — création, prix, disponibilité, stock, archivage.

    `DELETE` **archive** au lieu d'effacer : des commandes passées renvoient à
    l'article, et un effacement réel rendrait un historique illisible. C'est le
    comportement de `SoftDeleteModel`, et l'action `restore` en est le retour.
    """

    serializer_class = ManagedMenuItemSerializer
    queryset = MenuItem.objects.select_related("restaurant", "category").prefetch_related(
        "option_groups__options"
    )
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "restaurant__slug": ["exact"],
        "category": ["exact"],
        "is_available": ["exact"],
        "tracks_stock": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "description"]
    ordering_fields: ClassVar[list[str]] = ["sort_order", "name", "price_minor", "stock_quantity"]
    ordering: ClassVar[list[str]] = ["sort_order", "name"]

    def get_queryset(self) -> QuerySet[MenuItem]:
        base = MenuItem.objects.select_related("restaurant", "category").prefetch_related(
            "option_groups__options"
        )
        # Le défaut est la carte vivante ; les archives se demandent. L'inverse
        # — tout rendre — ferait remonter dans l'écran de la carte des articles
        # retirés il y a deux ans, mêlés à ceux du jour.
        #
        # `restore` fait exception sans qu'on le lui demande : elle porte sur un
        # article archivé **par définition**, et le lui cacher rendrait l'action
        # inatteignable — un article supprimé par erreur le resterait.
        archived = self.action == "restore" or str(
            self.request.query_params.get("archived", "")
        ).lower() in {"1", "true"}
        base = base.exclude(deleted_at__isnull=archived)
        return self.filter_queryset_by_scope(base)

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

    @extend_schema(
        request=StockSerializer, responses={200: ManagedMenuItemSerializer}, tags=["catalog"]
    )
    @action(detail=True, methods=["post"], permission_classes=[CATALOG_PERMISSION])
    def stock(self, request: Request, pk: str) -> Response:
        """Fixe le stock restant — geste d'inventaire.

        Distincte du `PATCH` général, et pas seulement par commodité : c'est le
        geste le plus fréquent de la journée, il se donne à un poste qui n'a
        pas à toucher aux prix, et il mérite d'apparaître tel quel dans le
        journal d'accès plutôt que noyé dans « modification d'article ».
        """
        serializer = StockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item = self.get_object()
        item.stock_quantity = serializer.validated_data["stock_quantity"]
        item.save(update_fields=["stock_quantity", "updated_at"])
        return Response(ManagedMenuItemSerializer(item).data)

    @extend_schema(responses={200: ManagedMenuItemSerializer}, tags=["catalog"])
    @action(detail=True, methods=["post"], permission_classes=[CATALOG_PERMISSION])
    def restore(self, request: Request, pk: str) -> Response:
        """Remet au menu un article archivé."""
        item = self.get_object()
        item.deleted_at = None
        item.save(update_fields=["deleted_at", "updated_at"])
        return Response(ManagedMenuItemSerializer(item).data)

    @extend_schema(
        request=ApplyTemplateSerializer,
        responses={201: OptionGroupSerializer},
        tags=["catalog"],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="apply-template",
        permission_classes=[CATALOG_PERMISSION],
    )
    def apply_template(self, request: Request, pk: str) -> Response:
        """Applique un modèle de la bibliothèque à cet article.

        **Copie, ne référence pas.** L'option créée porte le nom et le prix du
        modèle *au moment de l'application*, et vit ensuite sa propre vie :
        corriger le modèle ne repricera aucun article déjà en vitrine, ni aucun
        panier en cours de composition. C'est ce qui permet d'avoir une
        bibliothèque sans faire du prix une donnée partagée (C1).

        Le groupe est créé s'il n'existe pas encore sur l'article — le nom seul
        le désigne, puisque c'est ainsi que l'exploitation le nomme.
        """
        serializer = ApplyTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item = self.get_object()
        template = serializer.validated_data["template"]

        # Le modèle et l'article doivent appartenir au même établissement :
        # sans ce contrôle, un opérateur appliquerait la bibliothèque d'une
        # enseigne à la carte d'une autre.
        if template.restaurant_id != item.restaurant_id:
            raise BusinessRuleViolation(
                "Ce modèle appartient à un autre établissement.",
                template_restaurant=template.restaurant.slug,
            )

        nom_groupe = serializer.validated_data["group_name"] or template.group_name or "Options"
        group, _ = OptionGroup.objects.get_or_create(
            menu_item=item, name=nom_groupe, defaults={"min_select": 0, "max_select": 1}
        )

        if group.options.filter(name=template.name).exists():
            raise BusinessRuleViolation(
                "Cette option figure déjà dans ce groupe.",
                option_name=template.name,
            )

        # `MoneyField` est composite (`contribute_to_class`) : django-stubs ne
        # le voit pas comme un attribut de modèle, comme ailleurs dans le projet.
        Option.objects.create(  # type: ignore[misc]
            group=group,
            name=template.name,
            price_delta=template.price_delta,
            is_default=template.is_default,
            sort_order=template.sort_order,
        )

        group.refresh_from_db()
        # Forme de lecture — elle porte les options du groupe : l'appelant vient
        # d'en ajouter une, la lui renvoyer lui évite un aller-retour.
        return Response(OptionGroupSerializer(group).data, status=status.HTTP_201_CREATED)


class ManagedOptionGroupViewSet(_ScopedCatalogViewSet[OptionGroup]):
    """Groupes d'options — « Cuisson », « Suppléments ».

    Les bornes `min_select` / `max_select` sont ce qui fait la règle de
    validation du panier : elles vivent en donnée pour que l'exploitation crée
    « 2 accompagnements parmi 5 » sans développement (ADR-003).
    """

    serializer_class = ManagedOptionGroupSerializer
    queryset = OptionGroup.objects.select_related("menu_item")
    restaurant_path = "menu_item__restaurant"
    filterset_fields: ClassVar[dict[str, list[str]]] = {"menu_item": ["exact"]}

    def get_queryset(self) -> QuerySet[OptionGroup]:
        return self.filter_queryset_by_scope(OptionGroup.objects.select_related("menu_item"))

    def perform_create(self, serializer: Any) -> None:
        assert_in_scope(
            authenticated_user(self.request),
            serializer.validated_data["menu_item"].restaurant_id,
        )
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        item = serializer.validated_data.get("menu_item")
        if item is not None:
            assert_in_scope(authenticated_user(self.request), item.restaurant_id)
        serializer.save()


class ManagedOptionViewSet(_ScopedCatalogViewSet[Option]):
    """Options d'un groupe, et leur écart de prix — potentiellement négatif."""

    serializer_class = ManagedOptionSerializer
    queryset = Option.objects.select_related("group__menu_item")
    restaurant_path = "group__menu_item__restaurant"
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "group": ["exact"],
        "is_available": ["exact"],
    }

    def get_queryset(self) -> QuerySet[Option]:
        return self.filter_queryset_by_scope(Option.objects.select_related("group__menu_item"))

    def perform_create(self, serializer: Any) -> None:
        assert_in_scope(
            authenticated_user(self.request),
            serializer.validated_data["group"].menu_item.restaurant_id,
        )
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        group = serializer.validated_data.get("group")
        if group is not None:
            assert_in_scope(authenticated_user(self.request), group.menu_item.restaurant_id)
        serializer.save()


class ManagedOptionTemplateViewSet(_ScopedCatalogViewSet[OptionTemplate]):
    """Bibliothèque d'options réutilisables — `/catalog/manage/option-templates/`.

    Une bibliothèque, pas des options en service : ce que l'exploitation range
    ici ne coûte rien tant qu'il n'est pas appliqué à un article. L'application
    **copie** (voir `ManagedMenuItemViewSet.apply_template`), si bien que
    corriger un modèle ne change aucun prix déjà en vitrine.
    """

    serializer_class = ManagedOptionTemplateSerializer
    queryset = OptionTemplate.objects.select_related("restaurant")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "restaurant__slug": ["exact"],
        "group_name": ["exact"],
        "is_active": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "group_name"]

    def get_queryset(self) -> QuerySet[OptionTemplate]:
        return self.filter_queryset_by_scope(OptionTemplate.objects.select_related("restaurant"))

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
