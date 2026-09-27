"""Contrats du carnet d'adresses et des préférences — ADR-009.

`user` n'apparaît dans aucun sérialiseur : il vient du jeton. Un champ
propriétaire acceptable en entrée serait une prise de contrôle en un paramètre
de formulaire — on écrirait une adresse dans le carnet de quelqu'un d'autre.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.geography.models import City
from apps.profiles.models import Address, CustomerPreference
from common.serializers import LocationField

__all__ = ["AddressSerializer", "CustomerPreferenceSerializer"]


class AddressSerializer(serializers.ModelSerializer[Address]):
    """Adresse de livraison.

    `location` est obligatoire, et c'est délibéré : à Lomé, l'adressage postal
    ne permet pas de trouver une porte. C'est le point — et le repère qui
    l'accompagne — dont le livreur se sert réellement, alors que `line1` sert
    surtout à l'affichage.
    """

    location = LocationField()
    city = serializers.PrimaryKeyRelatedField(
        queryset=City.objects.filter(is_active=True, country__is_active=True)
    )
    city_name = serializers.CharField(source="city.name", read_only=True)

    class Meta:
        model = Address
        fields = [
            "id",
            "label",
            "kind",
            "recipient_name",
            "recipient_phone",
            "line1",
            "line2",
            "landmark",
            "city",
            "city_name",
            "location",
            "delivery_instructions",
            "is_default",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "city_name", "created_at", "updated_at"]


class CustomerPreferenceSerializer(serializers.ModelSerializer[CustomerPreference]):
    """Préférences alimentaires et de notification.

    Le canal push transactionnel ne figure pas ici : « votre livreur arrive »
    n'est pas du marketing et ne se coupe pas. N'exposer que ce qui est
    réellement réglable évite de promettre un réglage qu'on ignorera.
    """

    class Meta:
        model = CustomerPreference
        fields = [
            "dietary_restrictions",
            "allergens",
            "marketing_push_enabled",
            "marketing_email_enabled",
            "preferred_language",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]
