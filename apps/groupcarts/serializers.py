"""Contrats du panier collaboratif — invariant C1.

Aucun montant n'est accepté en entrée, ici comme dans `carts` : le participant
dit *ce qu'il veut*, le serveur dit *ce que ça coûte*. Les sérialiseurs de sortie
décrivent un calcul et n'ont pas de table correspondante.

La sortie porte toujours **qui a ajouté quoi**. C'est ce que l'ancienne
implémentation ne savait pas dire, et c'est la seule information qui rende
l'écran d'une commande groupée lisible.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

# Réutilisés tels quels depuis le panier personnel : une option retenue et une
# quantité s'écrivent de la même façon dans les deux paniers. Les redéfinir ici
# aurait produit deux composants homonymes dans le schéma OpenAPI — et deux
# classes générées côté client, que rien n'empêcherait ensuite de diverger.
from apps.carts.serializers import QuantitySerializer, SelectedOptionSerializer
from apps.catalog.models import MenuItem, Option
from apps.orders.models import PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.serializers import MoneyField

__all__ = [
    "GroupCartCancelSerializer",
    "GroupCartConfirmSerializer",
    "GroupCartLineWriteSerializer",
    "GroupCartOpenSerializer",
    "GroupCartSerializer",
    "JoinByCodeSerializer",
    "QuantitySerializer",
]


class MemberSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="user.id", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    joined_at = serializers.DateTimeField(read_only=True)


class GroupCartLineSerializer(serializers.Serializer[Any]):
    """Ligne valorisée, attribuée à son auteur."""

    id = serializers.UUIDField(source="line.id", read_only=True)
    member = serializers.UUIDField(source="line.member_id", read_only=True)
    member_name = serializers.CharField(source="line.member.full_name", read_only=True)
    menu_item = serializers.UUIDField(source="line.menu_item_id", read_only=True)
    name = serializers.CharField(source="line.menu_item.name", read_only=True)
    image = serializers.ImageField(source="line.menu_item.image", read_only=True)
    quantity = serializers.IntegerField(source="line.quantity", read_only=True)
    notes = serializers.CharField(source="line.notes", read_only=True)
    options = SelectedOptionSerializer(many=True, read_only=True)
    unit_price = MoneyField(read_only=True)
    total = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    unavailable_reason = serializers.CharField(read_only=True)


class MemberTotalSerializer(serializers.Serializer[Any]):
    member = serializers.UUIDField(read_only=True)
    total = MoneyField(read_only=True)


class GroupCartSerializer(serializers.Serializer[Any]):
    """Le panier collaboratif tel que chaque participant le voit.

    `code` en fait partie : tout participant peut réinviter quelqu'un, ce qui est
    le comportement attendu d'un déjeuner de groupe. Le code n'ouvre qu'un panier
    éphémère, et cesse de fonctionner à la clôture.
    """

    id = serializers.UUIDField(source="group_cart.id", read_only=True)
    code = serializers.CharField(source="group_cart.code", read_only=True)
    title = serializers.CharField(source="group_cart.title", read_only=True)
    status = serializers.CharField(source="group_cart.status", read_only=True)
    restaurant = serializers.CharField(source="group_cart.restaurant.slug", read_only=True)
    restaurant_name = serializers.CharField(source="group_cart.restaurant.name", read_only=True)
    host = serializers.UUIDField(source="group_cart.host_id", read_only=True)
    host_name = serializers.CharField(source="group_cart.host.full_name", read_only=True)
    closes_at = serializers.DateTimeField(source="group_cart.closes_at", read_only=True)
    accepts_contributions = serializers.BooleanField(
        source="group_cart.accepts_contributions", read_only=True
    )
    order = serializers.UUIDField(source="group_cart.order_id", read_only=True)

    members = MemberSerializer(many=True, read_only=True)
    lines = GroupCartLineSerializer(many=True, read_only=True)
    per_member = MemberTotalSerializer(many=True, read_only=True)
    currency = serializers.CharField(read_only=True)
    subtotal = MoneyField(read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    updated_at = serializers.DateTimeField(source="group_cart.updated_at", read_only=True)


class GroupCartOpenSerializer(serializers.Serializer[Any]):
    """Ouverture d'un panier collaboratif.

    `window_minutes` est facultatif : l'échéance a une valeur par défaut en
    réglage, et l'hôte n'a pas à en choisir une pour commander un déjeuner. Ni le
    statut ni le code ne s'écrivent depuis la requête — le premier naîtrait déjà
    confirmé, le second permettrait de deviner le code d'un autre panier.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.filter(is_active=True)
    )
    title = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default="", trim_whitespace=True
    )
    window_minutes = serializers.IntegerField(required=False, allow_null=True, default=None)


class JoinByCodeSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(max_length=12, trim_whitespace=True)


class GroupCartLineWriteSerializer(serializers.Serializer[Any]):
    """Dépôt d'une ligne.

    Pas de champ `member` : la ligne est attribuée à l'appelant authentifié.
    L'accepter en entrée laisserait un participant déposer des plats au nom d'un
    autre — et c'est l'hôte qui les paierait.
    """

    menu_item = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.alive())
    quantity = serializers.IntegerField(min_value=1, max_value=99, default=1)
    options = serializers.PrimaryKeyRelatedField(
        queryset=Option.objects.all(), many=True, required=False, default=list
    )
    notes = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default="", trim_whitespace=True
    )


class GroupCartConfirmSerializer(serializers.Serializer[Any]):
    """Confirmation par l'hôte — mêmes entrées qu'une commande ordinaire.

    L'adresse est filtrée sur celles de l'appelant : sans ce filtre, un
    identifiant deviné ferait livrer chez quelqu'un d'autre. C'est le même
    contrôle que sur la création de commande, et il n'y a aucune raison qu'il soit
    plus faible parce que le panier était partagé.
    """

    address = serializers.PrimaryKeyRelatedField[Address](queryset=Address.objects.none())
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
    instructions = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default="", trim_whitespace=True
    )
    promo_code = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default="", trim_whitespace=True
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None:
            self.fields["address"].queryset = Address.objects.filter(user=request.user)  # type: ignore[attr-defined]


class GroupCartCancelSerializer(serializers.Serializer[Any]):
    """Renoncement de l'hôte.

    Nommé distinctement de `orders.CancelSerializer` bien que la forme soit
    voisine : annuler une commande engagée et refermer un panier que personne n'a
    payé ne sont pas le même geste, et fusionner les deux contrats les
    condamnerait à évoluer ensemble.
    """

    reason = serializers.CharField(
        max_length=280, required=False, allow_blank=True, default="", trim_whitespace=True
    )
