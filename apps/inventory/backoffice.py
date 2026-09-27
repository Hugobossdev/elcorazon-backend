"""Administration de l'inventaire — l'API que consommera l'application `admin`.

## Pourquoi ces routes arrivent maintenant

Les recettes, le stock, la réservation et le juge de disponibilité existaient —
et personne ne pouvait saisir une livraison autrement que par un `shell`. Tout
le mécanisme de matière était en place et dormant.

## Les règles qu'elles tiennent

* **Cinq permissions**, parce que ce sont cinq métiers (`apps.accounts.permissions`) :
  consulter, configurer, recevoir, déclarer une perte ou un écart, valider.
* **Cloisonnement par cuisine**, au troisième étage de l'ADR-005 : une ligne de
  stock hors périmètre est introuvable, et une ouverture hors périmètre est
  refusée.
* **Le référentiel d'ingrédients est une écriture d'enseigne.** Un ingrédient
  n'appartient à aucune cuisine — une tomate est une tomate à Lomé comme à
  Abidjan —, donc le cloisonnement ne peut rien en dire, et le défaut sûr est
  le siège (`assert_unscoped`), comme pour un pays ou une zone.
* **Aucune route n'écrit un stock** : toutes passent par `InventoryService`, et
  les pertes et corrections par ses déclarations, qui appliquent le plafond. Un
  test vérifie que ce module n'appelle jamais `waste` ni `adjust` directement.
* **Idempotence des écritures de valeur** : l'en-tête `Idempotency-Key` est
  exigé sur une réception, une perte et une correction.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import F, QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.accounts.models import User
from apps.inventory.models import AdjustmentRequest, Ingredient, StockItem, StockMovement
from apps.inventory.serializers import (
    AdjustmentDeclarationSerializer,
    AdjustmentRequestSerializer,
    ApprovalSerializer,
    DeclarationSerializer,
    IngredientSerializer,
    ReceiptSerializer,
    RejectionSerializer,
    StockItemSerializer,
    StockMovementSerializer,
    WasteSerializer,
)
from apps.inventory.services import Declaration, InventoryService
from apps.restaurants.scoping import assert_in_scope, is_unscoped, staff_restaurant_ids
from common.money import Money
from common.pagination import HighVolumeCursorPagination
from common.permissions import (
    HasPermission,
    HasReadWritePermission,
    assert_unscoped,
    authenticated_user,
)
from common.quantities import COST_UNIT, UNITS_IN_BASE, Quantity

__all__ = [
    "ManagedAdjustmentRequestViewSet",
    "ManagedIngredientViewSet",
    "ManagedStockItemViewSet",
    "ManagedStockMovementViewSet",
]

INVENTORY_PERMISSION = HasReadWritePermission.of(read="inventory.read", write="inventory.write")
READ = HasPermission.of("inventory.read")
RECEIVE = HasPermission.of("inventory.receive")
ADJUST = HasPermission.of("inventory.adjust")
APPROVE = HasPermission.of("inventory.approve")

IDEMPOTENCY_HEADER = "Idempotency-Key"

IDEMPOTENCY_PARAMETER = OpenApiParameter(
    name=IDEMPOTENCY_HEADER,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "Clé tirée par le client, stable sur une même tentative. Un rejeu rend "
        "l'écriture d'origine au lieu de créditer ou de débiter une seconde fois."
    ),
)


def _cle_d_idempotence(request: Request) -> str:
    """L'en-tête exigé sur les écritures de valeur.

    Exigé et non facultatif, pour la raison que la commande a déjà apprise :
    rendu optionnel, il serait omis le jour où le réseau coupe — c'est-à-dire
    le seul jour où il sert.
    """
    cle = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not cle:
        raise ValidationError({IDEMPOTENCY_HEADER: "En-tête obligatoire sur cette route."})
    if len(cle) > 64:
        raise ValidationError({IDEMPOTENCY_HEADER: "64 caractères au plus."})
    return cle


def _perimetre(user: User, queryset: QuerySet[Any], chemin: str) -> QuerySet[Any]:
    """Filtre un queryset sur les cuisines du compte — `chemin` mène à la cuisine."""
    if is_unscoped(user):
        return queryset
    return queryset.filter(**{f"{chemin}__in": staff_restaurant_ids(user)})


# ---------------------------------------------------------------- référentiel


class ManagedIngredientViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet[Ingredient],
):
    """Le référentiel d'achat de l'enseigne.

    **Pas de suppression.** Une référence employée par une recette ou portée par
    un journal ne s'efface pas : elle se retire (`is_active: false`), et son
    histoire reste lisible.
    """

    permission_classes = (INVENTORY_PERMISSION,)
    serializer_class = IngredientSerializer
    queryset = Ingredient.objects.alive().order_by("name")
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "is_active": ["exact"],
        "dimension": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["name", "slug"]

    def perform_create(self, serializer: Any) -> None:
        assert_unscoped(authenticated_user(self.request), "Le référentiel d'ingrédients")
        serializer.save()

    def perform_update(self, serializer: Any) -> None:
        assert_unscoped(authenticated_user(self.request), "Le référentiel d'ingrédients")
        serializer.save()


# ---------------------------------------------------------------------- stock


@extend_schema(
    parameters=[
        OpenApiParameter(
            name="low",
            type=bool,
            description="Ne rend que les lignes au seuil d'alerte ou en dessous.",
        )
    ],
    tags=["inventory"],
)
class ManagedStockItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet[StockItem],
):
    """Les lignes de stock d'une cuisine — ouvrir, suivre, recevoir, corriger."""

    permission_classes = (INVENTORY_PERMISSION,)
    serializer_class = StockItemSerializer
    queryset = StockItem.objects.none()
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "restaurant__slug": ["exact"],
        "ingredient": ["exact"],
    }
    search_fields: ClassVar[list[str]] = ["ingredient__name"]
    ordering_fields: ClassVar[list[str]] = ["ingredient__name", "updated_at"]
    ordering: ClassVar[list[str]] = ["ingredient__name"]

    def get_queryset(self) -> QuerySet[StockItem]:
        base = StockItem.objects.select_related("ingredient", "restaurant__zone__city__country")
        if str(self.request.query_params.get("low", "")).lower() in {"1", "true"}:
            # Même règle que `StockItem.is_low`, écrite en SQL pour filtrer avant
            # la pagination : disponible — détenu moins promis — au seuil ou en
            # dessous. Une ligne sans seuil n'est jamais en alerte.
            base = base.filter(
                low_stock_threshold_base__isnull=False,
                low_stock_threshold_base__gte=F("on_hand_base") - F("reserved_base"),
            )
        return _perimetre(authenticated_user(self.request), base, "restaurant")

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ouvre la ligne — idempotent : une ligne déjà ouverte est rendue telle quelle."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        assert_in_scope(authenticated_user(request), donnees["restaurant"].pk)

        deja = StockItem.objects.filter(
            restaurant=donnees["restaurant"], ingredient=donnees["ingredient"]
        ).exists()
        item = InventoryService.open_item(
            restaurant=donnees["restaurant"],
            ingredient=donnees["ingredient"],
            low_stock_threshold=donnees.get("low_stock_threshold"),
        )
        return Response(
            StockItemSerializer(self._recharger(item)).data,
            status=status.HTTP_200_OK if deja else status.HTTP_201_CREATED,
        )

    def perform_update(self, serializer: Any) -> None:
        # Seul le seuil change ici ; le sérialiseur a refusé le reste. Il est
        # posé champ par champ — ses deux colonnes composites — pour que la
        # sauvegarde ne réécrive pas `on_hand` d'après une lecture périmée.
        item: StockItem = serializer.instance
        if "low_stock_threshold" in serializer.validated_data:
            item.low_stock_threshold = serializer.validated_data["low_stock_threshold"]
            item.save(
                update_fields=[
                    "low_stock_threshold_base",
                    "low_stock_threshold_dimension",
                    "updated_at",
                ]
            )

    # -------------------------------------------------------------- réception

    @extend_schema(
        request=ReceiptSerializer,
        responses={201: StockMovementSerializer},
        parameters=[IDEMPOTENCY_PARAMETER],
        tags=["inventory"],
    )
    @action(detail=True, methods=["post"], permission_classes=[RECEIVE])
    def receive(self, request: Request, pk: str) -> Response:
        """Enregistre une livraison.

        Le prix du lot devient un coût par kilogramme, par litre ou par unité —
        l'unité dans laquelle le coût se tient. L'unique arrondi, au demi
        supérieur, porte sur ce coût ; il est d'au plus une demi-unité mineure
        par kilogramme.
        """
        cle = _cle_d_idempotence(request)
        corps = ReceiptSerializer(data=request.data)
        corps.is_valid(raise_exception=True)
        item = self.get_object()

        quantite: Quantity = corps.validated_data["quantity"]
        if quantite.dimension != item.on_hand.dimension:
            raise ValidationError({"quantity": _mauvaise_unite(item)})

        lot: Money | None = corps.validated_data.get("total_cost")
        cout_unitaire = None
        if lot is not None:
            diviseur = UNITS_IN_BASE[COST_UNIT[quantite.dimension]]
            # Arrondi entier au demi supérieur, sans flottant : (2a + b) // 2b.
            numerateur = lot.amount_minor * diviseur
            cout_unitaire = Money(
                (2 * numerateur + quantite.amount_base) // (2 * quantite.amount_base),
                lot.currency,
            )

        mouvement = InventoryService.receive(
            item=item,
            quantity=quantite,
            unit_cost=cout_unitaire,
            actor=authenticated_user(request),
            reference=corps.validated_data["reference"],
            request_key=cle,
        )
        return Response(StockMovementSerializer(mouvement).data, status=status.HTTP_201_CREATED)

    # ----------------------------------------------------------- déclarations

    @extend_schema(
        request=WasteSerializer,
        responses={201: DeclarationSerializer, 202: DeclarationSerializer},
        parameters=[IDEMPOTENCY_PARAMETER],
        tags=["inventory"],
    )
    @action(detail=True, methods=["post"], permission_classes=[ADJUST])
    def waste(self, request: Request, pk: str) -> Response:
        """Déclare une perte. `201` si écrite, `202` si elle attend une validation."""
        cle = _cle_d_idempotence(request)
        corps = WasteSerializer(data=request.data)
        corps.is_valid(raise_exception=True)

        declaration = InventoryService.declare_waste(
            item=self.get_object(),
            quantity=corps.validated_data["quantity"],
            reason=corps.validated_data["reason"],
            actor=authenticated_user(request),
            request_key=cle,
        )
        return _reponse_de_declaration(declaration)

    @extend_schema(
        request=AdjustmentDeclarationSerializer,
        responses={201: DeclarationSerializer, 202: DeclarationSerializer},
        parameters=[IDEMPOTENCY_PARAMETER],
        tags=["inventory"],
    )
    @action(detail=True, methods=["post"], permission_classes=[ADJUST])
    def adjust(self, request: Request, pk: str) -> Response:
        """Déclare un écart d'inventaire. `201` si écrit, `202` s'il attend une validation.

        Avec `counted`, l'écart est calculé contre le stock **détenu** au moment
        de la requête. Le calcul n'est pas fait sous le verrou de la
        déclaration — il le précède d'une lecture —, et c'est assumé : une
        commande consommée dans la même seconde décalerait l'écart d'une
        portion, que le prochain comptage rattrapera, alors qu'un calcul sous
        verrou demanderait au service de connaître la notion de comptage.
        """
        cle = _cle_d_idempotence(request)
        corps = AdjustmentDeclarationSerializer(data=request.data)
        corps.is_valid(raise_exception=True)
        item = self.get_object()

        if "counted" in corps.validated_data:
            compte: Quantity = corps.validated_data["counted"]
            if compte.dimension != item.on_hand.dimension:
                raise ValidationError({"counted": _mauvaise_unite(item)})
            ecart = compte - item.on_hand
        else:
            ecart = corps.validated_data["delta"]

        declaration = InventoryService.declare_adjustment(
            item=item,
            delta=ecart,
            reason=corps.validated_data["reason"],
            actor=authenticated_user(request),
            request_key=cle,
        )
        return _reponse_de_declaration(declaration)

    @staticmethod
    def _recharger(item: StockItem) -> StockItem:
        return StockItem.objects.select_related("ingredient", "restaurant").get(pk=item.pk)


def _mauvaise_unite(item: StockItem) -> str:
    return f"« {item.ingredient.name} » se mesure en {item.on_hand.reference_unit}."


def _reponse_de_declaration(declaration: Declaration) -> Response:
    """`201` : le mouvement est au journal. `202` : la demande attend quelqu'un d'autre."""
    en_attente = declaration.movement is None
    corps = DeclarationSerializer(
        {
            "outcome": "pending_approval" if en_attente else "applied",
            "movement": declaration.movement,
            "request": declaration.request,
        }
    )
    return Response(
        corps.data, status=status.HTTP_202_ACCEPTED if en_attente else status.HTTP_201_CREATED
    )


# ------------------------------------------------------------------- journal


class ManagedStockMovementViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet[StockMovement]
):
    """Le journal des mouvements — en lecture seule, et par curseur.

    Par curseur parce qu'il ne cesse de grandir : une pagination par numéro de
    page ferait compter la table entière à chaque écran, et décalerait les pages
    à chaque commande passée pendant la lecture.
    """

    permission_classes = (READ,)
    serializer_class = StockMovementSerializer
    queryset = StockMovement.objects.none()
    pagination_class = HighVolumeCursorPagination
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "stock_item": ["exact"],
        "kind": ["exact"],
        "stock_item__restaurant__slug": ["exact"],
        "stock_item__ingredient": ["exact"],
        "reference": ["exact"],
    }

    def get_queryset(self) -> QuerySet[StockMovement]:
        base = StockMovement.objects.select_related(
            "stock_item__ingredient", "stock_item__restaurant", "actor"
        )
        return _perimetre(authenticated_user(self.request), base, "stock_item__restaurant")


# ------------------------------------------------------------------ validation


class ManagedAdjustmentRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet[AdjustmentRequest]
):
    """La file des pertes et corrections qui attendent une seconde personne."""

    permission_classes = (READ,)
    serializer_class = AdjustmentRequestSerializer
    queryset = AdjustmentRequest.objects.none()
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "status": ["exact"],
        "kind": ["exact"],
        "stock_item": ["exact"],
        "stock_item__restaurant__slug": ["exact"],
    }
    ordering: ClassVar[list[str]] = ["-created_at"]

    def get_queryset(self) -> QuerySet[AdjustmentRequest]:
        base = AdjustmentRequest.objects.select_related(
            "stock_item__ingredient", "stock_item__restaurant", "requested_by", "decided_by"
        ).order_by("-created_at")
        return _perimetre(authenticated_user(self.request), base, "stock_item__restaurant")

    @extend_schema(
        request=ApprovalSerializer,
        responses={200: AdjustmentRequestSerializer},
        tags=["inventory"],
    )
    @action(detail=True, methods=["post"], permission_classes=[APPROVE])
    def approve(self, request: Request, pk: str) -> Response:
        """Valide : la perte ou la correction est écrite au journal."""
        corps = ApprovalSerializer(data=request.data)
        corps.is_valid(raise_exception=True)
        demande = InventoryService.approve(
            request=self.get_object(),
            actor=authenticated_user(request),
            note=corps.validated_data["note"],
        )
        return Response(AdjustmentRequestSerializer(self._recharger(demande)).data)

    @extend_schema(
        request=RejectionSerializer,
        responses={200: AdjustmentRequestSerializer},
        tags=["inventory"],
    )
    @action(detail=True, methods=["post"], permission_classes=[APPROVE])
    def reject(self, request: Request, pk: str) -> Response:
        """Refuse, en disant pourquoi. Rien ne bouge au stock."""
        corps = RejectionSerializer(data=request.data)
        corps.is_valid(raise_exception=True)
        demande = InventoryService.reject(
            request=self.get_object(),
            actor=authenticated_user(request),
            note=corps.validated_data["note"],
        )
        return Response(AdjustmentRequestSerializer(self._recharger(demande)).data)

    @staticmethod
    def _recharger(demande: AdjustmentRequest) -> AdjustmentRequest:
        return AdjustmentRequest.objects.select_related(
            "stock_item__ingredient", "stock_item__restaurant", "requested_by", "decided_by"
        ).get(pk=demande.pk)
