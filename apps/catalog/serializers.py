"""Contrats du catalogue — ADR-009, invariants C1 et S1.

**Le prix est toujours en lecture seule.** Aucun sérialiseur d'entrée ne le
porte, ni ici ni dans le panier ni dans la commande : c'est la traduction
littérale de C1, où l'implémentation précédente acceptait le prix envoyé par le
client et facturait ce qu'on lui disait de facturer.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.catalog.models import (
    Category,
    MenuItem,
    Option,
    OptionGroup,
    OptionTemplate,
    Review,
)
from apps.restaurants.models import Restaurant
from common.serializers import MoneyField

__all__ = [
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
        fields = ["id", "restaurant", "name", "slug", "emoji", "description", "sort_order"]
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


class MenuItemSerializer(serializers.ModelSerializer[MenuItem]):
    """Forme de liste — ce qu'affiche une carte de menu.

    Ni les ingrédients, ni les groupes d'options : une page de vingt articles
    porterait alors des centaines de lignes que l'écran de liste n'affiche pas,
    et le premier chargement du menu s'en trouverait ralenti sur un réseau
    mobile — le seul que ces clients utilisent.
    """

    price = MoneyField(read_only=True)
    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)
    category = serializers.SlugRelatedField[Category](slug_field="slug", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = MenuItem
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
            "is_popular",
            "vip_exclusive",
            "rating_average",
            "rating_count",
            "sort_order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class MenuItemDetailSerializer(MenuItemSerializer):
    option_groups = OptionGroupSerializer(many=True, read_only=True)

    class Meta(MenuItemSerializer.Meta):
        fields = [*MenuItemSerializer.Meta.fields, "ingredients", "calories", "option_groups"]
        read_only_fields = fields


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
            "description",
            "sort_order",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


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
