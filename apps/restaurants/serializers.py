"""Contrats des établissements — ADR-006, ADR-009.

Trois booléens sortent séparément — `is_open`, `accepts_orders`,
`can_order_now` — au lieu d'un seul « disponible ». C'est ce qui permet à
l'application de dire *pourquoi* : « fermé, ouvre à 11 h » n'est pas
« débordé, réessayez dans dix minutes », et les deux n'appellent pas le même
geste de la part du client.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import Role, User, UserType
from apps.geography.models import DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, StaffMembership
from common.serializers import LocationField, MoneyField

__all__ = [
    "ManagedOpeningHoursSerializer",
    "ManagedRestaurantSerializer",
    "NearbyQuerySerializer",
    "OpeningHoursSerializer",
    "RestaurantDetailSerializer",
    "RestaurantSerializer",
    "StaffSerializer",
]


class OpeningHoursSerializer(serializers.ModelSerializer[OpeningHours]):
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpeningHours
        fields = ["id", "weekday", "opens_at", "closes_at", "crosses_midnight"]
        read_only_fields = fields


class RestaurantSerializer(serializers.ModelSerializer[Restaurant]):
    """Forme de liste.

    `distance_m` n'apparaît que si la requête portait un point de référence :
    l'annotation est absente sinon, et inventer un `0` ou un `null` ferait
    croire à une proximité qu'on n'a pas mesurée.
    """

    location = LocationField(read_only=True)
    city = serializers.CharField(source="zone.city.name", read_only=True)
    currency = serializers.CharField(read_only=True)
    delivery_fee_from = MoneyField(source="zone.base_fee", read_only=True)
    estimated_delivery_minutes = serializers.IntegerField(
        source="zone.estimated_delivery_minutes", read_only=True
    )

    is_open = serializers.SerializerMethodField()
    can_order_now = serializers.SerializerMethodField()
    distance_m = serializers.SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "address",
            "location",
            "city",
            "phone",
            "cover_image",
            "currency",
            "delivery_fee_from",
            "estimated_delivery_minutes",
            "default_preparation_minutes",
            "is_open",
            "accepts_orders",
            "can_order_now",
            "distance_m",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def _now(self) -> dt.datetime:
        """Instant de référence, calculé **une fois** par réponse.

        Sans cette mise en cache, une liste de vingt restaurants appellerait
        vingt fois `timezone.now()` et pourrait, à la seconde près, se retrouver
        à cheval sur une ouverture — deux établissements de la même ville
        rendus dans deux états contradictoires.
        """
        now: dt.datetime = self.context.setdefault("now", timezone.now())
        return now

    def get_is_open(self, obj: Restaurant) -> bool:
        return obj.is_open_at(self._now())

    def get_can_order_now(self, obj: Restaurant) -> bool:
        return obj.is_active and obj.accepts_orders and obj.is_open_at(self._now())

    def get_distance_m(self, obj: Restaurant) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.m, 1) if distance is not None else None


class RestaurantDetailSerializer(RestaurantSerializer):
    opening_hours = OpeningHoursSerializer(many=True, read_only=True)

    class Meta(RestaurantSerializer.Meta):
        fields = [*RestaurantSerializer.Meta.fields, "email", "opening_hours"]
        read_only_fields = fields


class NearbyQuerySerializer(serializers.Serializer[Any]):
    """Point de référence facultatif de `GET /restaurants/`.

    Les deux coordonnées vont ensemble : une latitude seule ne situe rien, et
    l'accepter en silence produirait un tri par proximité à une dimension —
    faux, mais plausible à la lecture.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90, required=False)
    lon = serializers.FloatField(min_value=-180, max_value=180, required=False)

    def validate(self, attrs: dict[str, float]) -> dict[str, float]:
        if ("lat" in attrs) != ("lon" in attrs):
            raise serializers.ValidationError("lat et lon se fournissent ensemble.")
        return attrs


# --------------------------------------------------------------- back-office


class ManagedRestaurantSerializer(serializers.ModelSerializer[Restaurant]):
    """Établissement vu de l'exploitation.

    `is_active` et `accepts_orders` y sont tous les deux, et les confondre
    serait perdre l'information : le premier dit si l'établissement existe, le
    second s'il prend des commandes maintenant. Un coup de feu en cuisine se
    règle avec le second ; le premier ferait disparaître le restaurant de
    l'application.

    La devise et le fuseau n'y figurent pas : ils sont hérités du pays à
    travers la zone (ADR-006), et les rendre saisissables ici permettrait à
    deux établissements du même marché de facturer dans deux unités.
    """

    zone = serializers.PrimaryKeyRelatedField[DeliveryZone](queryset=DeliveryZone.objects.all())
    location = LocationField()
    currency = serializers.CharField(read_only=True)
    timezone = serializers.CharField(read_only=True)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "zone",
            "address",
            "location",
            "phone",
            "email",
            "cover_image",
            "currency",
            "timezone",
            "is_active",
            "accepts_orders",
            "default_preparation_minutes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "currency", "timezone", "created_at", "updated_at"]


class ManagedOpeningHoursSerializer(serializers.ModelSerializer[OpeningHours]):
    """Plage d'ouverture.

    Une plage qui franchit minuit (`22:00 → 02:00`) se saisit telle quelle :
    `closes_at < opens_at` est la représentation, et le service d'ouverture en
    tient compte. Obliger à saisir deux plages sur deux jours serait la source
    d'erreur classique du service de nuit du week-end.
    """

    restaurant = serializers.PrimaryKeyRelatedField[Restaurant](queryset=Restaurant.objects.all())
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpeningHours
        fields = ["id", "restaurant", "weekday", "opens_at", "closes_at", "crosses_midnight"]
        read_only_fields = ["id", "crosses_midnight"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Une plage vide est refusée ici plutôt qu'en base.

        La contrainte `CHECK` existe et reste la dernière ligne de défense ;
        elle sortirait en 500. `22:00 → 22:00` est une faute de saisie
        courante, qui mérite un message.
        """
        instance = self.instance
        opens_at = attrs.get("opens_at", getattr(instance, "opens_at", None))
        closes_at = attrs.get("closes_at", getattr(instance, "closes_at", None))

        if opens_at is not None and opens_at == closes_at:
            raise serializers.ValidationError(
                {"closes_at": "Une plage d'ouverture et de fermeture identiques ne couvre rien."}
            )
        return attrs


class StaffSerializer(serializers.ModelSerializer[User]):
    """Compte du personnel : ce qu'il sait faire et sur quoi.

    `permissions` est rendu à côté des rôles, en lecture seule : c'est leur
    union, et c'est la seule chose que le code consulte réellement. L'écran qui
    coche des rôles peut ainsi montrer immédiatement ce qu'ils accordent, sans
    recomposer côté client une union qui dériverait du jour où un rôle change.

    L'adresse électronique ne se modifie pas après création. Elle est
    l'identifiant de connexion : la changer depuis un écran d'administration
    serait un chemin de reprise de compte — on redirige les courriels de
    réinitialisation vers soi, et le compte suit.
    """

    password = serializers.CharField(
        write_only=True, required=False, min_length=8, trim_whitespace=False
    )
    roles = serializers.PrimaryKeyRelatedField[Role](
        many=True, queryset=Role.objects.all(), required=False
    )
    # Lisible et inscriptible sous le même nom : la lecture passe par
    # l'accesseur inverse de `Restaurant.staff`, l'écriture est reprise à la
    # main dans `create` / `update` — DRF refuse de poser un `.set()` sur une
    # relation qui passe par un modèle intermédiaire, et c'est heureux, puisque
    # le remplacement en bloc effacerait les dates de rattachement.
    restaurants = serializers.SlugRelatedField[Restaurant](
        many=True,
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        required=False,
        help_text="Établissements sur lesquels ce compte travaille.",
    )
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "phone",
            "password",
            "is_active",
            "roles",
            "restaurants",
            "permissions",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "permissions", "last_seen_at", "created_at", "updated_at"]

    def get_permissions(self, obj: User) -> list[str]:
        return sorted(obj.permission_codes())

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        if self.instance is not None:
            fields["email"].read_only = True
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError(
                {"password": "Un compte du personnel se crée avec un mot de passe."}
            )
        if "password" in attrs:
            validate_password(attrs["password"])
        return attrs

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> User:
        roles = validated_data.pop("roles", [])
        restaurants = validated_data.pop("restaurants", [])
        password = validated_data.pop("password")

        # `user_type` n'est pas un champ d'entrée : cette ressource crée des
        # comptes du personnel et rien d'autre. L'accepter du corps de la
        # requête permettrait de fabriquer un livreur validé — ou un client —
        # depuis l'écran des rôles.
        member = User.objects.create_user(
            email=validated_data.pop("email"),
            password=password,
            user_type=UserType.STAFF,
            **validated_data,
        )
        member.roles.set(roles)
        _align_memberships(member, restaurants)
        return member

    @transaction.atomic
    def update(self, instance: User, validated_data: dict[str, Any]) -> User:
        roles = validated_data.pop("roles", None)
        restaurants = validated_data.pop("restaurants", None)
        password = validated_data.pop("password", None)

        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        if password:
            instance.set_password(password)
        instance.save()

        if roles is not None:
            instance.roles.set(roles)
        if restaurants is not None:
            _align_memberships(instance, restaurants)
        return instance


def _align_memberships(member: User, restaurants: list[Restaurant]) -> None:
    """Aligne les rattachements sur la liste reçue, par différence.

    Et non « tout effacer puis tout recréer » : un rattachement porte sa date
    de création, qui dit depuis quand quelqu'un travaille là. La remise à zéro
    à chaque enregistrement d'un formulaire l'effacerait sans que personne ne
    le remarque.
    """
    voulus = {etablissement.pk for etablissement in restaurants}
    actuels = set(
        StaffMembership.objects.filter(user=member).values_list("restaurant_id", flat=True)
    )

    StaffMembership.objects.filter(user=member, restaurant_id__in=actuels - voulus).delete()
    StaffMembership.objects.bulk_create(
        StaffMembership(user=member, restaurant=etablissement)
        for etablissement in restaurants
        if etablissement.pk not in actuels
    )
