"""Contrats de la livraison — ADR-009, invariants L1 et L5.

Aucun sérialiseur d'entrée ne porte `verification_status`, `deliveries_completed`
ni `total_earnings`. Un livreur qui pourrait écrire son propre statut de dossier
se validerait lui-même ; un livreur qui pourrait écrire ses compteurs se
paierait. Ces champs n'existent pas en écriture — il n'y a donc rien à valider.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.accounts.models import User
from apps.delivery.models import (
    Assignment,
    CourierProfile,
    CourierRating,
    CourierShift,
    VehicleType,
)
from apps.delivery.states import DELIVERY_MACHINE, VERIFICATION_MACHINE
from apps.restaurants.models import Restaurant
from common.serializers import LocationField, MoneyField

__all__ = [
    "AssignmentSerializer",
    "CourierProfileSerializer",
    "CourierProvisioningSerializer",
    "CourierPublicSerializer",
    "CourierRatingSerializer",
    "CourierRatingWriteSerializer",
    "CourierShiftSerializer",
    "DeclineSerializer",
    "DeliveryTransitionSerializer",
    "DocumentsSerializer",
    "OfferSerializer",
    "OnlineSerializer",
    "VerificationSerializer",
]


class CourierPublicSerializer(serializers.ModelSerializer[CourierProfile]):
    """Ce qu'un client peut voir du livreur qui lui apporte sa commande.

    Prénom, véhicule, note — de quoi le reconnaître à la porte. Ni téléphone
    personnel, ni pièces d'identité, ni position hors course : suivre son
    livreur pendant sa livraison est un service, le suivre ensuite est une
    filature.
    """

    full_name = serializers.CharField(source="user.full_name", read_only=True)
    avatar = serializers.ImageField(source="user.avatar", read_only=True)

    class Meta:
        model = CourierProfile
        fields = ["id", "full_name", "avatar", "vehicle_type", "rating_average", "rating_count"]
        read_only_fields = fields


class CourierProfileSerializer(serializers.ModelSerializer[CourierProfile]):
    """Dossier complet — lisible par son titulaire et par le personnel.

    Les trois pièces justificatives y figurent, en **lecture seule** : c'est ce
    que l'écran de validation doit montrer avant de trancher, et l'instruire
    sans les voir n'aurait pas de sens. Elles sortent en URL signées, qui
    expirent — le stockage est privé. L'implémentation précédente les déposait
    dans un compartiment **public** (`getPublicUrl`) : une pièce d'identité y
    restait lisible par quiconque connaissait l'adresse, indéfiniment.

    Elles ne s'écrivent pas ici : c'est le livreur qui dépose ses pièces, depuis
    son application (`DocumentsSerializer`), et tout dépôt repasse le dossier en
    attente (L5).
    """

    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)
    last_location = LocationField(read_only=True)
    total_earnings = MoneyField(read_only=True)
    can_accept_orders = serializers.BooleanField(read_only=True)

    class Meta:
        model = CourierProfile
        fields = [
            "id",
            "full_name",
            "email",
            "restaurant",
            "verification_status",
            "verification_notes",
            "verified_at",
            "id_document",
            "licence_document",
            "vehicle_document",
            "vehicle_type",
            "vehicle_plate",
            "is_online",
            "can_accept_orders",
            "last_location",
            "last_location_at",
            "deliveries_completed",
            "deliveries_cancelled",
            "rating_average",
            "rating_count",
            "total_earnings",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class CourierProvisioningSerializer(serializers.Serializer[Any]):
    """Embauche d'un livreur : le compte et le dossier, en une requête.

    Deux écrans pour ce qui est un seul geste — créer le compte ici, ouvrir le
    dossier là — laisserait régulièrement des comptes de type livreur sans
    dossier, c'est-à-dire des gens qui se connectent à l'application et n'y
    trouvent rien.

    Les pièces justificatives ne sont **pas** ici : c'est le livreur qui les
    dépose, depuis son application (`POST /delivery/me/`), et c'est bien lui qui
    les a. Les numéros, eux, se relèvent d'une carte présentée à l'embauche —
    ils sont donc facultatifs mais acceptés.

    Conforme à la promesse du module : aucun champ de statut de dossier ni de
    compteur en entrée. Le dossier naît en attente, les compteurs à zéro.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        help_text="Établissement de rattachement — il doit être dans votre périmètre.",
    )
    vehicle_type = serializers.ChoiceField(choices=VehicleType.choices)
    vehicle_plate = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    national_id_number = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    licence_number = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

    def validate_email(self, value: str) -> str:
        """Normalisée et unique, comme à l'inscription.

        La normalisation n'est pas cosmétique : l'adresse est l'identifiant de
        connexion, et deux comptes ne différant que par la casse rendraient
        l'un des deux inaccessible.
        """
        normalized = value.strip().lower()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError("Un compte existe déjà avec cette adresse.")
        return normalized

    def validate_phone(self, value: str) -> str:
        if value and User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("Un compte existe déjà avec ce numéro.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Valide le mot de passe **en connaissant l'identité** du futur compte.

        Ici et non dans un validateur de champ, où c'est pourtant plus court :
        le validateur de similarité de Django compare le mot de passe aux
        attributs de l'utilisateur, et sans utilisateur à lui donner il ne
        compare rien. C'est exactement le contrôle qui manque quand un tiers
        choisit un mot de passe pour quelqu'un d'autre — « livreur123 » pour
        `nouveau.livreur@…` est le cas nominal, pas le cas tordu.
        """
        futur = User(
            email=attrs["email"],
            full_name=attrs["full_name"],
            phone=attrs.get("phone") or "",
        )
        try:
            validate_password(attrs["password"], user=futur)
        except DjangoValidationError as erreur:
            # Rattachée au champ : levée telle quelle, elle atterrirait dans
            # `non_field_errors`, où le formulaire ne l'affiche pas.
            raise serializers.ValidationError({"password": list(erreur.messages)}) from erreur
        return attrs


class AssignmentSerializer(serializers.ModelSerializer[Assignment]):
    """Course, vue par le livreur ou par le personnel."""

    order_reference = serializers.CharField(source="order.reference", read_only=True)
    restaurant_name = serializers.CharField(source="order.restaurant.name", read_only=True)
    pickup_location = LocationField(source="order.restaurant.location", read_only=True)
    delivery_address_line = serializers.CharField(
        source="order.delivery_address_line", read_only=True
    )
    delivery_landmark = serializers.CharField(source="order.delivery_landmark", read_only=True)
    delivery_location = serializers.JSONField(source="order.delivery_location", read_only=True)
    recipient_name = serializers.CharField(source="order.recipient_name", read_only=True)
    recipient_phone = serializers.CharField(source="order.recipient_phone", read_only=True)
    courier = CourierPublicSerializer(read_only=True)
    courier_fee = MoneyField(read_only=True)
    allowed_transitions = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = [
            "id",
            "order",
            "order_reference",
            "restaurant_name",
            "pickup_location",
            "delivery_address_line",
            "delivery_landmark",
            "delivery_location",
            "recipient_name",
            "recipient_phone",
            "courier",
            "status",
            "allowed_transitions",
            "courier_fee",
            "offered_at",
            "accepted_at",
            "picked_up_at",
            "delivered_at",
            "decline_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, obj: Assignment) -> list[str]:
        return sorted(DELIVERY_MACHINE.targets_from(obj.status))


class OfferSerializer(serializers.Serializer[Any]):
    courier = serializers.PrimaryKeyRelatedField[CourierProfile](
        queryset=CourierProfile.objects.all()
    )


class DeclineSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class DeliveryTransitionSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=sorted(DELIVERY_MACHINE.states))
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class VerificationSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=sorted(VERIFICATION_MACHINE.states))
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class OnlineSerializer(serializers.Serializer[Any]):
    is_online = serializers.BooleanField()


class DocumentsSerializer(serializers.Serializer[Any]):
    """Dépôt de pièces justificatives.

    Toutes facultatives : on remplace ce qu'on remplace. Mais déposer **une**
    pièce suffit à repasser le dossier en attente (L5) — un dossier validé sur
    des documents qu'on a ensuite changés n'est plus validé.
    """

    id_document = serializers.FileField(required=False)
    licence_document = serializers.FileField(required=False)
    vehicle_document = serializers.FileField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("Aucune pièce fournie.")
        return attrs


class CourierRatingSerializer(serializers.ModelSerializer[CourierRating]):
    """Une note telle qu'on la relit — pour savoir si la course est déjà notée."""

    courier = serializers.UUIDField(source="assignment.courier_id", read_only=True)
    order = serializers.UUIDField(source="assignment.order_id", read_only=True)

    class Meta:
        model = CourierRating
        fields = ["id", "order", "courier", "score", "comment", "created_at"]
        read_only_fields = fields


class CourierRatingWriteSerializer(serializers.Serializer[Any]):
    """Les deux seuls champs qu'une note accepte.

    Ni le livreur ni la course : ils se déduisent de la commande citée dans
    l'URL. Les accepter du client permettrait de noter le livreur d'un autre.
    """

    score = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class CourierShiftSerializer(serializers.ModelSerializer[CourierShift]):
    """Créneau planifié — indicatif, jamais opposable (voir `backoffice.py`).

    `courier_name` est rendu à côté de l'identifiant : un planning se lit par
    nom, et l'écran aurait sinon à charger la flotte entière pour afficher une
    ligne.
    """

    courier_name = serializers.CharField(source="courier.user.full_name", read_only=True)

    class Meta:
        model = CourierShift
        fields = [
            "id",
            "courier",
            "courier_name",
            "day_of_week",
            "start_time",
            "end_time",
            "is_available",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "courier_name", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Traduit en 400 ce que la contrainte `CHECK` refuserait en 500.

        Un créneau à cheval sur minuit n'est pas accepté : il s'écrit en deux
        lignes, sur deux jours, ce qui reste juste et se trie. L'accepter
        obligerait chaque lecture du planning à traiter le cas « fin < début »
        comme un débordement, et une seule oubliée afficherait une barre de
        longueur négative.
        """
        instance = self.instance
        debut = attrs.get("start_time") or (instance.start_time if instance else None)
        fin = attrs.get("end_time") or (instance.end_time if instance else None)

        if debut is not None and fin is not None and fin <= debut:
            raise serializers.ValidationError(
                {
                    "end_time": (
                        "La fin doit suivre le début. Un créneau qui passe minuit "
                        "s'écrit en deux lignes, sur deux jours."
                    )
                }
            )

        return attrs
