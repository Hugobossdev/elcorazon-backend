"""Contrats du catalogue — ADR-009, invariants C1 et S1.

**Le prix est toujours en lecture seule.** Aucun sérialiseur d'entrée ne le
porte, ni ici ni dans le panier ni dans la commande : c'est la traduction
littérale de C1, où l'implémentation précédente acceptait le prix envoyé par le
client et facturait ce qu'on lui disait de facturer.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from rest_framework import serializers

from apps.accounts.models import User
from apps.catalog.availability import menu_unavailabilities
from apps.catalog.models import (
    Category,
    MenuItem,
    Option,
    OptionGroup,
    OptionTemplate,
    Review,
    Variant,
)
from apps.restaurants.models import Restaurant
from common.availability import Unavailability
from common.serializers import MoneyField
from common.uploads import valider_image_publique

__all__ = [
    "CategoryReorderSerializer",
    "CategorySerializer",
    "ManagedCategorySerializer",
    "ManagedMenuItemSerializer",
    "ManagedOptionGroupSerializer",
    "ManagedOptionSerializer",
    "MenuItemDetailSerializer",
    "MenuItemSerializer",
    "OptionGroupSerializer",
    "OptionSerializer",
    "ReviewSerializer",
    "ReviewWriteSerializer",
    "StockSerializer",
]


class CategorySerializer(serializers.ModelSerializer[Category]):
    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)

    class Meta:
        model = Category
        fields = ["id", "restaurant", "name", "slug", "emoji", "image", "description", "sort_order"]
        read_only_fields = fields


class OptionSerializer(serializers.ModelSerializer[Option]):
    price_delta = MoneyField(read_only=True)

    class Meta:
        model = Option
        fields = ["id", "name", "price_delta", "is_default", "is_available", "sort_order"]
        read_only_fields = fields


class OptionGroupSerializer(serializers.ModelSerializer[OptionGroup]):
    options = OptionSerializer(many=True, read_only=True)
    is_required = serializers.BooleanField(read_only=True)

    class Meta:
        model = OptionGroup
        fields = ["id", "name", "min_select", "max_select", "is_required", "sort_order", "options"]
        read_only_fields = fields


class _JugedMenuListSerializer(serializers.ListSerializer[MenuItem]):
    """Interroge le juge **une fois pour la page**, pas une fois par article.

    Le verdict d'un article passe par sa recette et par le stock de sa cuisine.
    Posé article par article, une page de vingt plats coûterait quarante
    requêtes ; posé sur la page, deux.
    """

    def to_representation(self, data: Any) -> list[Any]:
        items = list(data.all() if hasattr(data, "all") else data)
        juges: set[uuid.UUID] = self.context.setdefault("menu_judged", set())
        verdicts: dict[uuid.UUID, Unavailability] = self.context.setdefault("menu_verdicts", {})
        a_juger = [item for item in items if item.pk not in juges]
        verdicts.update(menu_unavailabilities(a_juger))
        juges.update(item.pk for item in a_juger)
        return super().to_representation(items)


class MenuItemSerializer(serializers.ModelSerializer[MenuItem]):
    """Forme de liste — ce qu'affiche une carte de menu.

    Ni les ingrédients, ni les groupes d'options : une page de vingt articles
    porterait alors des centaines de lignes que l'écran de liste n'affiche pas,
    et le premier chargement du menu s'en trouverait ralenti sur un réseau
    mobile — le seul que ces clients utilisent.

    ## `is_available` est un verdict, pas la colonne

    Sur cette forme publique, `is_available` répond à « puis-je commander cet
    article ? » : retiré, désactivé, catégorie éteinte, épuisé, **ou sans la
    matière pour le préparer**. C'est la réponse du juge de disponibilité
    (`apps.availability`), et `unavailable_code` / `unavailable_reason` disent
    pourquoi.

    Le nom est conservé plutôt que remplacé par un `is_orderable` neuf, et
    c'est délibéré : les applications clientes déjà installées lisent ce champ
    pour griser un plat. Elles cessent donc de proposer un burger dont le pain
    manque **sans attendre leur mise à jour** — trois applications déployées ne
    se mettent pas à jour le même jour.

    La colonne brute — l'interrupteur que la cuisine manipule — reste lisible et
    inscriptible par le back-office, sur `ManagedMenuItemSerializer`. Les deux
    ne se confondent pas : la cuisine bascule un interrupteur, le client lit un
    verdict.

    La disponibilité de la **cuisine** — fermée, suspendue — n'y entre pas :
    elle est sur l'établissement (`can_order_now`), où elle se dit une fois pour
    toute la carte au lieu d'être répétée sur chaque plat.
    """

    price = MoneyField(read_only=True)
    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)
    category = serializers.SlugRelatedField[Category](slug_field="slug", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    is_available = serializers.SerializerMethodField()
    unavailable_code = serializers.SerializerMethodField()
    unavailable_reason = serializers.SerializerMethodField()
    # Les tailles **actives** seulement, dans leur ordre : une taille retirée de
    # la carte ne se propose pas, une taille épuisée se montre grisée. Elles
    # sont sur la liste, et pas seulement sur le détail, contrairement aux
    # options : il y en a peu, et sans elles une carte ne peut ni annoncer le
    # bon prix, ni savoir qu'il faut choisir avant d'ajouter, ni reprendre une
    # commande passée dans sa taille.
    variants = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        list_serializer_class = _JugedMenuListSerializer
        fields = [
            "id",
            "restaurant",
            "category",
            "category_name",
            "name",
            "slug",
            "description",
            "image",
            "price",
            "preparation_minutes",
            "allergens",
            "dietary_tags",
            "is_available",
            "unavailable_code",
            "unavailable_reason",
            "is_popular",
            "vip_exclusive",
            "rating_average",
            "rating_count",
            "sort_order",
            "variants",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_variants(self, obj: MenuItem) -> list[dict[str, Any]]:
        actives = [v for v in obj.variants.all() if v.is_active]
        actives.sort(key=lambda v: (v.sort_order, v.name))
        return VariantSerializer(actives, many=True).data  # type: ignore[return-value]

    def _verdict(self, obj: MenuItem) -> Unavailability | None:
        """Le verdict de la page s'il a été calculé, sinon celui de cet article seul.

        « Jugé » et « sans motif » sont tenus séparément, et c'est le point : le
        dictionnaire des verdicts ne contient que les articles **non**
        commandables. Y chercher un article jamais jugé rendrait `None` — donc
        « disponible » — par simple absence, dès qu'un même contexte sert à
        sérialiser deux articles l'un après l'autre.
        """
        juges: set[uuid.UUID] = self.context.setdefault("menu_judged", set())
        verdicts: dict[uuid.UUID, Unavailability] = self.context.setdefault("menu_verdicts", {})
        if obj.pk not in juges:
            verdicts.update(menu_unavailabilities([obj]))
            juges.add(obj.pk)
        return verdicts.get(obj.pk)

    def get_is_available(self, obj: MenuItem) -> bool:
        return self._verdict(obj) is None

    def get_unavailable_code(self, obj: MenuItem) -> str:
        verdict = self._verdict(obj)
        return str(verdict.code) if verdict is not None else ""

    def get_unavailable_reason(self, obj: MenuItem) -> str:
        verdict = self._verdict(obj)
        return verdict.message if verdict is not None else ""


class VariantSerializer(serializers.ModelSerializer[Variant]):
    """Une taille, telle que le client la choisit : son prix **remplace** celui
    de l'article (lot 2)."""

    price = MoneyField(read_only=True)

    class Meta:
        model = Variant
        fields = ["id", "name", "price", "is_available", "sort_order"]
        read_only_fields = fields


class MenuItemDetailSerializer(MenuItemSerializer):
    option_groups = OptionGroupSerializer(many=True, read_only=True)

    class Meta(MenuItemSerializer.Meta):
        fields = [
            *MenuItemSerializer.Meta.fields,
            "ingredients",
            "calories",
            "option_groups",
        ]
        read_only_fields = fields


class ManagedVariantSerializer(serializers.ModelSerializer[Variant]):
    """Une taille, telle que le back-office la saisit."""

    menu_item = serializers.PrimaryKeyRelatedField[MenuItem](queryset=MenuItem.objects.alive())
    price = MoneyField()

    class Meta:
        model = Variant
        fields = [
            "id",
            "menu_item",
            "name",
            "sku",
            "price",
            "is_available",
            "is_active",
            "sort_order",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Le prix se libelle dans la devise du marché, comme celui de l'article."""
        article = attrs.get("menu_item") or (self.instance.menu_item if self.instance else None)
        prix = attrs.get("price")
        if article is not None and prix is not None:
            devise = article.restaurant.currency
            if prix.currency != devise:
                raise serializers.ValidationError(
                    {
                        "price": (
                            f"Cet établissement facture en {devise} ; prix reçu en {prix.currency}."
                        )
                    }
                )
        return attrs


class ReviewAuthorSerializer(serializers.ModelSerializer[User]):
    """Auteur d'un avis, réduit à ce qu'un écran public doit montrer.

    Ni adresse électronique ni téléphone : un avis est lisible sans compte, et
    y joindre le contact de son auteur transformerait la page menu en annuaire
    de clients.
    """

    class Meta:
        model = User
        fields = ["id", "full_name", "avatar"]
        read_only_fields = fields


class ReviewSerializer(serializers.ModelSerializer[Review]):
    user = ReviewAuthorSerializer(read_only=True)

    class Meta:
        model = Review
        fields = [
            "id",
            "menu_item",
            "user",
            "rating",
            "title",
            "comment",
            "is_verified_purchase",
            "helpful_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# --------------------------------------------------------------- back-office
#
# Les sérialiseurs de lecture publique sont intégralement en lecture seule
# (`read_only_fields = fields`). Ceux-ci sont leur pendant d'écriture, et ils
# sont **séparés** plutôt qu'ouverts au cas par cas : un champ rendu inscriptible
# sur un sérialiseur public l'est pour tout le monde, et rien dans la relecture
# d'un diff ne le distingue d'un champ de lecture. Deux classes rendent la
# question visible à chaque ajout de champ — de quel côté va-t-il ?


class ManagedCategorySerializer(serializers.ModelSerializer[Category]):
    """Catégorie vue du back-office : `is_active` compris.

    La liste publique filtre les catégories inactives ; celle-ci les montre,
    sans quoi désactiver une catégorie la ferait disparaître de l'écran qui
    sert à la réactiver.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.all()
    )

    class Meta:
        model = Category
        fields = [
            "id",
            "restaurant",
            "name",
            "slug",
            "emoji",
            "image",
            "description",
            "sort_order",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        # Poids et format vérifiés ici, et non plus seulement dans l'écran.
        extra_kwargs: ClassVar[dict[str, dict[str, Any]]] = {
            "image": {"validators": [valider_image_publique]}
        }


class CategoryReorderSerializer(serializers.Serializer[Any]):
    """L'ordre voulu pour les catégories **d'un** établissement.

    ## Pourquoi une route plutôt qu'un `PATCH` par ligne

    Le back-office envoyait un `PATCH` par catégorie déplacée, en série. Sur un
    refus au quatrième, les trois premiers étaient déjà écrits : la carte
    restait dans un ordre que personne n'avait demandé, et l'écran — qui
    remettait sa liste telle qu'elle était avant — affichait alors un ordre
    différent de celui de la base. Un rangement est un geste, pas huit.

    L'ordre est donné par la **position dans la liste** et non par un numéro :
    laisser le client calculer les `sort_order` l'expose à les faire diverger
    (deux fois 3, un trou à 5), et le serveur n'aurait aucun moyen de le
    savoir.

    La liste est **complète** pour l'établissement. Une liste partielle
    laisserait les absentes à leur ancien rang, donc mêlées aux nouvelles sans
    que rien ne dise où.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        help_text="L'établissement dont on range la carte : `sort_order` lui est propre.",
    )
    categories = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        help_text="Les identifiants, dans l'ordre voulu. Le rang 1 est le premier.",
    )

    def validate_categories(self, valeur: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(valeur)) != len(valeur):
            raise serializers.ValidationError(
                "Une catégorie ne peut pas occuper deux rangs à la fois."
            )
        return valeur


class ManagedMenuItemSerializer(serializers.ModelSerializer[MenuItem]):
    """Article vu du back-office — le seul endroit où un prix s'écrit.

    `rating_average` et `rating_count` restent en lecture seule : ce sont des
    agrégats calculés depuis les avis (voir `ReviewService.refresh_rating`), et
    les rendre inscriptibles permettrait de fabriquer une note.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.all()
    )
    category = serializers.PrimaryKeyRelatedField[Category](queryset=Category.objects.all())
    price = MoneyField()
    is_deleted = serializers.BooleanField(read_only=True)
    # Contrairement à la forme publique, qui les omet pour ne pas alourdir une
    # carte de vingt articles sur un réseau mobile : le back-office travaille sur
    # un seul établissement, depuis un poste fixe, et l'écran des personnalisations
    # a besoin de savoir quelles options portent quels articles. Les lire ici
    # évite d'enchaîner une requête par article.
    option_groups = OptionGroupSerializer(many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            "id",
            "restaurant",
            "category",
            "name",
            "slug",
            "description",
            "image",
            "price",
            "preparation_minutes",
            "calories",
            "ingredients",
            "allergens",
            "dietary_tags",
            "is_available",
            "is_popular",
            "vip_exclusive",
            "tracks_stock",
            "stock_quantity",
            "rating_average",
            "rating_count",
            "sort_order",
            "option_groups",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "rating_average",
            "rating_count",
            "option_groups",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        # La borne des 5 Mo n'existait que dans le back-office : un appel
        # direct déposait n'importe quel poids au compartiment public.
        extra_kwargs: ClassVar[dict[str, dict[str, Any]]] = {
            "image": {"validators": [valider_image_publique]}
        }

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Deux cohérences qu'aucune contrainte de base ne peut porter.

        La première — la catégorie appartient au même établissement que
        l'article — parce que PostgreSQL ne sait pas comparer deux clés
        étrangères d'une même ligne sans dénormaliser le restaurant sur la
        catégorie. Sans elle, un article de Lomé se range dans une catégorie de
        Kara et disparaît de sa propre carte.

        La seconde — le prix est libellé dans la devise du marché — parce que
        la devise n'est pas choisie au niveau de l'article : elle est héritée du
        pays (ADR-006). Un article tarifé en euros dans un restaurant en francs
        CFA n'est refusé qu'au moment de l'addition au panier, c'est-à-dire chez
        le client.
        """
        instance = self.instance
        restaurant = attrs.get("restaurant") or (instance.restaurant if instance else None)
        category = attrs.get("category") or (instance.category if instance else None)

        if (
            restaurant is not None
            and category is not None
            and category.restaurant_id != (restaurant.pk)
        ):
            raise serializers.ValidationError(
                {"category": "Cette catégorie appartient à un autre établissement."}
            )

        price = attrs.get("price")
        if restaurant is not None and price is not None and price.currency != restaurant.currency:
            raise serializers.ValidationError(
                {
                    "price": (
                        f"Cet établissement facture en {restaurant.currency} ; "
                        f"prix reçu en {price.currency}."
                    )
                }
            )

        return attrs


class ManagedOptionGroupSerializer(serializers.ModelSerializer[OptionGroup]):
    menu_item = serializers.PrimaryKeyRelatedField[MenuItem](queryset=MenuItem.objects.alive())

    class Meta:
        model = OptionGroup
        fields = ["id", "menu_item", "name", "min_select", "max_select", "sort_order"]
        read_only_fields = ["id"]


class ManagedOptionSerializer(serializers.ModelSerializer[Option]):
    group = serializers.PrimaryKeyRelatedField[OptionGroup](queryset=OptionGroup.objects.all())
    price_delta = MoneyField()

    class Meta:
        model = Option
        fields = [
            "id",
            "group",
            "name",
            "price_delta",
            "is_default",
            "is_available",
            "sort_order",
        ]
        read_only_fields = ["id"]


class StockSerializer(serializers.Serializer[Any]):
    """Correction de stock à l'inventaire.

    Une **valeur absolue** et non un delta : on compte ce qu'il reste en
    réserve, on ne calcule pas ce qui a été ajouté depuis la dernière fois. Un
    delta rejoué par un réseau capricieux ajouterait deux fois ; une valeur
    absolue rejouée écrit deux fois la même chose.

    Les mouvements liés aux commandes, eux, ne passent jamais par ici : ils sont
    décomptés par `StockService`, sous verrou, au moment où la commande est
    créée.
    """

    stock_quantity = serializers.IntegerField(min_value=0)


class ReviewWriteSerializer(serializers.Serializer[Any]):
    """Entrée d'un avis.

    `menu_item` est résolu contre les articles vivants : un article
    logiquement supprimé n'accepte plus d'avis, alors qu'il reste lisible dans
    les commandes passées.

    Ni `user` ni `is_verified_purchase` n'y figurent — le premier vient du
    jeton, le second du serveur (S1). Un champ absent du sérialiseur est un
    champ qu'aucune requête ne peut forcer.
    """

    menu_item = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.alive())
    rating = serializers.IntegerField(min_value=1, max_value=5)
    title = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class ManagedOptionTemplateSerializer(serializers.ModelSerializer[OptionTemplate]):
    """Modèle d'option réutilisable de l'établissement."""

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.all()
    )
    price_delta = MoneyField()

    class Meta:
        model = OptionTemplate
        fields = [
            "id",
            "restaurant",
            "name",
            "group_name",
            "price_delta",
            "is_default",
            "is_active",
            "sort_order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ApplyTemplateSerializer(serializers.Serializer[Any]):
    """Application d'un modèle à un article.

    `group_name` est facultatif : à défaut, celui du modèle. Ni le prix ni le
    nom de l'option ne s'écrivent ici — ils viennent du modèle, sans quoi
    « appliquer un modèle » deviendrait « créer une option quelconque », et la
    bibliothèque ne garantirait plus rien.
    """

    template = serializers.PrimaryKeyRelatedField[OptionTemplate](
        queryset=OptionTemplate.objects.filter(is_active=True)
    )
    group_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")


class ManagedReviewSerializer(serializers.ModelSerializer[Review]):
    """Un avis vu de la modération : qui, sur quoi, et s'il est masqué."""

    user = ReviewAuthorSerializer(read_only=True)
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)
    restaurant_name = serializers.CharField(source="menu_item.restaurant.name", read_only=True)
    hidden_by_name = serializers.CharField(
        source="hidden_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = Review
        fields = [
            "id",
            "menu_item",
            "menu_item_name",
            "restaurant_name",
            "user",
            "rating",
            "title",
            "comment",
            "is_verified_purchase",
            "hidden_at",
            "hidden_reason",
            "hidden_by_name",
            "created_at",
        ]
        read_only_fields = fields


class ReviewHideSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)
