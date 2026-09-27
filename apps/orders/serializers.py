"""Contrats de la commande — ADR-009, invariants C1 et C2.

Aucun montant n'est accepté en entrée. Le sérialiseur de création ne porte que
des **choix** — quel panier, quelle adresse, quel moyen de paiement — et le
serveur en déduit tout le reste. C'est C1 rendu inexprimable plutôt que
vérifié : il n'existe pas de champ à valider.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.geography.models import Country, DeliveryZone
from apps.orders.models import Order, OrderLine, OrderNote, OrderStatusEvent, PaymentMethod
from apps.orders.states import ORDER_MACHINE
from apps.profiles.models import Address
from apps.promotions.serializers import PromotionSerializer
from apps.restaurants.models import Restaurant
from apps.restaurants.states import KNOWN_TO_CUSTOMERS
from common.serializers import LocationField, MoneyField

__all__ = [
    "CancelSerializer",
    "KitchenOrderSerializer",
    "OrderCreateSerializer",
    "OrderDetailSerializer",
    "OrderPreviewSerializer",
    "OrderQuoteSerializer",
    "OrderSerializer",
    "StaffCancelSerializer",
    "StatusTransitionSerializer",
]


class OrderLineSerializer(serializers.ModelSerializer[OrderLine]):
    unit_price = MoneyField(read_only=True)
    line_total = MoneyField(read_only=True)

    class Meta:
        model = OrderLine
        fields = [
            "id",
            "menu_item",
            "item_name",
            "variant_name",
            "item_image",
            "unit_price",
            "quantity",
            "line_total",
            "options",
            "notes",
        ]
        read_only_fields = fields


class KitchenLineSerializer(serializers.ModelSerializer[OrderLine]):
    """Une ligne telle qu'on la prépare — **sans les prix**.

    Ce qu'il faut pour cuisiner tient dans quatre champs : quoi, combien,
    comment, et ce que le client a demandé en plus. Le prix unitaire et le
    total de ligne n'y ont pas leur place : ils appartiennent à la facture, pas
    au poste, et les rendre gonflerait une charge que le poste relit à chaque
    événement du service.
    """

    class Meta:
        model = OrderLine
        fields = ["id", "item_name", "quantity", "options", "notes"]
        read_only_fields = fields


class KitchenOrderSerializer(serializers.ModelSerializer[Order]):
    """Une commande vue du poste de cuisine.

    ## Pourquoi un contrat de plus

    Le poste lisait la **forme de liste** (`OrderSerializer`), qui ne porte pas
    les lignes : chaque carte affichait « 3 article(s) » et rien d'autre. Un
    écran de cuisine qui ne dit pas ce qu'il faut cuisiner n'a pas d'usage —
    c'est le défaut le plus visible de la vague précédente.

    Lui servir la **forme détaillée** aurait été pire : elle porte les montants,
    les événements de statut, l'adresse et le téléphone du client, que le poste
    n'a aucune raison de connaître, et elle les porte pour chaque commande du
    service, relue à chaque événement temps réel.

    D'où cette forme-ci : ce qu'on cuisine, dans quel ordre, et depuis combien
    de temps ça attend. Rien sur le client, rien sur l'argent.
    """

    lines = KitchenLineSerializer(many=True, read_only=True)
    allowed_transitions = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "reference",
            "status",
            "allowed_transitions",
            "placed_at",
            "estimated_delivery_at",
            "items_count",
            "delivery_instructions",
            "lines",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, obj: Order) -> list[str]:
        return sorted(ORDER_MACHINE.targets_from(obj.status))

    @extend_schema_field(serializers.IntegerField)
    def get_items_count(self, obj: Order) -> int:
        """La somme des quantités — le premier chiffre qui intéresse une cuisine."""
        annote = getattr(obj, "items_count", None)
        if annote is not None:
            return int(annote)
        return sum(ligne.quantity for ligne in obj.lines.all())


class OrderStatusEventSerializer(serializers.ModelSerializer[OrderStatusEvent]):
    class Meta:
        model = OrderStatusEvent
        fields = ["id", "from_status", "to_status", "reason", "created_at"]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer[Order]):
    """Forme de liste — l'historique du client.

    `allowed_transitions` est rendu par le serveur plutôt que déduit côté
    client : la table des transitions est déjà déclarée une fois (ADR-010), et
    la recopier dans trois applications Flutter garantirait qu'elles divergent.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True)
    # Point d'enlèvement, pour la carte de suivi du client.
    #
    # L'écran de suivi montre trois repères — le restaurant, le livreur, le
    # client — et n'en tenait que deux : la commande ne disait pas d'où part le
    # repas. L'application le suppléait par une constante écrite en dur
    # (`AppConstants.restaurantLatitude`), qui désigne le premier établissement
    # et deviendra fausse au deuxième. La course du livreur porte déjà ce point
    # (`AssignmentSerializer.pickup_location`) ; le client, lui, ne voit jamais
    # sa course. C'est donc à la commande de le porter.
    restaurant_location = LocationField(source="restaurant.location", read_only=True)
    # La géographie **figée** de la commande (`Order.country`, `city`,
    # `delivery_zone`) — et non celle où la cuisine est rattachée aujourd'hui.
    # Vides pour une commande antérieure que la reprise n'a pas su situer.
    country = serializers.SlugRelatedField[Country](
        slug_field="iso_code", read_only=True, allow_null=True
    )
    city = serializers.CharField(source="city.name", read_only=True, allow_null=True, default=None)
    city_slug = serializers.CharField(
        source="city.slug", read_only=True, allow_null=True, default=None
    )
    delivery_zone = serializers.PrimaryKeyRelatedField[DeliveryZone](
        read_only=True, allow_null=True
    )
    delivery_zone_name = serializers.CharField(read_only=True)
    subtotal = MoneyField(read_only=True)
    delivery_fee = MoneyField(read_only=True)
    discount = MoneyField(read_only=True)
    total = MoneyField(read_only=True)
    # Ce qui a réellement été encaissé d'avance, reporté par `payments`.
    #
    # Nul tant que rien n'a été réglé — ce qui est le cas de toute commande
    # payable à la livraison. Le **moyen** de paiement ne le dit pas : il
    # annonce une intention, pas un encaissement, et les confondre faisait dire
    # au back-office « rien n'a été encaissé, il n'y a rien à rembourser » sur
    # une commande déjà payée.
    amount_paid = MoneyField(read_only=True, allow_null=True)
    allowed_transitions = serializers.SerializerMethodField()
    lines_count = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "reference",
            "restaurant",
            "restaurant_name",
            "restaurant_location",
            "country",
            "city",
            "city_slug",
            "delivery_zone",
            "delivery_zone_name",
            "status",
            "allowed_transitions",
            "lines_count",
            "items_count",
            "subtotal",
            "delivery_fee",
            "discount",
            "total",
            "amount_paid",
            "payment_method",
            "delivery_address_line",
            "delivery_landmark",
            "delivery_location",
            "recipient_name",
            "recipient_phone",
            "placed_at",
            "estimated_delivery_at",
            "delivered_at",
            "cancelled_at",
            "cancellation_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, obj: Order) -> list[str]:
        return sorted(ORDER_MACHINE.targets_from(obj.status))

    @extend_schema_field(serializers.IntegerField)
    def get_lines_count(self, obj: Order) -> int:
        """Nombre de **lignes** — trois produits distincts font trois lignes."""
        return self._compte(obj, "lines_count", lambda lignes: len(lignes))

    @extend_schema_field(serializers.IntegerField)
    def get_items_count(self, obj: Order) -> int:
        """Nombre d'**articles** — la somme des quantités.

        Deux burgers, une pizza et trois donuts font six articles et trois
        lignes. C'est le premier chiffre qui intéresse une cuisine, et le
        second qui intéresse une facture ; les deux sont rendus, l'appelant
        choisit celui qu'il affiche.
        """
        return self._compte(
            obj, "items_count", lambda lignes: sum(ligne.quantity for ligne in lignes)
        )

    @staticmethod
    def _compte(
        obj: Order, annotation: str, depuis_les_lignes: Callable[[QuerySet[OrderLine]], int]
    ) -> int:
        """Lit l'annotation de la requête, ou retombe sur les lignes chargées.

        **Ces deux compteurs existent parce que la forme de liste ne porte pas
        `lines`**, et ne doit pas les porter : renvoyer les lignes de vingt
        commandes pour n'en afficher que le nombre multiplierait par dix le
        poids de chaque page. Le back-office affichait donc « 0 article » sur
        toutes les commandes, avec un bandeau « aucun article trouvé » — sur des
        commandes qui en contenaient.

        L'annotation est posée par les vues (`_avec_compteurs`) : le compte se
        fait alors en base, en une requête pour toute la page. Le repli n'est
        utilisé que pour un objet isolé — la réponse d'une transition de statut,
        par exemple — où il n'y a qu'une commande et où les lignes sont déjà là.
        """
        annote = getattr(obj, annotation, None)
        if annote is not None:
            return int(annote)
        return depuis_les_lignes(obj.lines.all())


class OrderDetailSerializer(OrderSerializer):
    lines = OrderLineSerializer(many=True, read_only=True)
    status_events = OrderStatusEventSerializer(many=True, read_only=True)
    delivery_instructions = serializers.CharField(read_only=True)

    class Meta(OrderSerializer.Meta):
        fields = [
            *OrderSerializer.Meta.fields,
            "delivery_instructions",
            "lines",
            "status_events",
        ]
        read_only_fields = fields


class OrderCreateSerializer(serializers.Serializer[Any]):
    """Passage de commande.

    `restaurant` désigne le panier à valider — il y en a un par établissement
    entamé. `promo_code` est facultatif : c'est une chaîne que le serveur
    évalue, jamais un montant que le client annonce.
    """

    # Une cuisine suspendue reste désignable : c'est le juge qui la refuse, avec
    # son motif (`kitchen_suspended`), et non la validation du champ — voir
    # `KNOWN_TO_CUSTOMERS`.
    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.filter(status__in=KNOWN_TO_CUSTOMERS)
    )
    address = serializers.PrimaryKeyRelatedField[Address](queryset=Address.objects.none())
    payment_method = serializers.ChoiceField(choices=PaymentMethod.choices)
    instructions = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )
    # Une chaîne, et rien d'autre : ce qu'elle vaut est décidé par le serveur
    # (F4). Un montant de remise envoyé par le client serait la même faille que
    # le prix envoyé par le client (C1).
    promo_code = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # L'adresse est cherchée **dans le carnet de l'appelant**. Avec un
        # `queryset` global, un identifiant deviné ferait livrer la commande
        # chez quelqu'un d'autre — et le message d'erreur dirait au passage si
        # l'adresse existe. Ici, celle d'autrui est simplement invalide.
        request = self.context.get("request")
        if request is not None:
            self.fields["address"].queryset = Address.objects.filter(user=request.user)  # type: ignore[attr-defined]


class OrderPreviewSerializer(serializers.Serializer[Any]):
    """Demande de devis avant commande.

    Le panier est désigné par son restaurant et lu **côté serveur** : laisser
    le client annoncer son sous-total permettrait de franchir un minimum de
    commande avec un montant qui n'est pas le sien.

    L'adresse est facultative. Fournie, les frais de livraison sont exacts —
    calculés depuis la zone qui couvre le point d'arrivée. Omise, ce sont ceux
    de la zone de l'établissement, ce qui suffit à dire si un code
    « livraison offerte » vaut quelque chose.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug", queryset=Restaurant.objects.filter(is_active=True)
    )
    address = serializers.PrimaryKeyRelatedField[Address](
        queryset=Address.objects.none(), required=False, allow_null=True
    )
    promo_code = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None:
            self.fields["address"].queryset = Address.objects.filter(user=request.user)  # type: ignore[attr-defined]


class OrderQuoteSerializer(serializers.Serializer[Any]):
    """Décomposition d'un total, avant de s'engager.

    Rendre le détail et pas seulement le total : un client qui voit
    « 4 200 F » sans savoir ce qui vient des frais et ce qui vient de la remise
    n'a aucun moyen de vérifier qu'on ne s'est pas trompé.
    """

    subtotal = MoneyField(read_only=True)
    delivery_fee = MoneyField(read_only=True)
    discount = MoneyField(read_only=True)
    total = MoneyField(read_only=True)
    promotion = PromotionSerializer(read_only=True, allow_null=True)
    is_orderable = serializers.BooleanField(read_only=True)
    # Pourquoi `is_orderable` est faux — vides sinon. Le code est stable
    # (`common.availability.UnavailabilityCode`) ; la phrase est affichable.
    unavailable_code = serializers.CharField(read_only=True)
    unavailable_reason = serializers.CharField(read_only=True)


class StatusTransitionSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=sorted(ORDER_MACHINE.states))
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class CancelSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class StaffCancelSerializer(serializers.Serializer[Any]):
    """Annulation par l'exploitation — le motif n'est pas facultatif.

    Le client annule sa propre commande ; l'opérateur annule celle d'un tiers,
    qui sera remboursé et rappellera pour savoir pourquoi. Le champ obligatoire
    est ce qui rend le journal des annulations exploitable au lieu d'être une
    liste de dates.
    """

    reason = serializers.CharField(max_length=500, allow_blank=False)


class OrderNoteSerializer(serializers.ModelSerializer[OrderNote]):
    """Une note interne — lue et écrite par le personnel seul."""

    author_name = serializers.CharField(source="author.full_name", read_only=True, default=None)

    class Meta:
        model = OrderNote
        fields = ["id", "author_name", "content", "created_at"]
        read_only_fields = ["id", "author_name", "created_at"]

    def validate_content(self, value: str) -> str:
        texte = value.strip()
        if not texte:
            raise serializers.ValidationError("Une note vide n'apprend rien à personne.")
        return texte
