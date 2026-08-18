"""Jeu de données de référence initial.

El Corazón opère aujourd'hui un seul établissement, à Lomé. Cette commande pose
le pays, la ville, la zone et le restaurant correspondants — c'est-à-dire la
hiérarchie complète de l'ADR-006, remplie avec les données réelles du marché.

Idempotente : rejouable sans dupliquer, ce qui la rend utilisable aussi bien à
la première installation qu'à chaque déploiement.

Elle vit dans `restaurants` et non dans `geography` bien qu'elle commence par
la géographie : elle crée aussi l'établissement, et `geography` n'a pas le
droit de connaître `restaurants` — c'est l'inverse. La placer là-bas créait un
cycle dans le graphe de l'ADR-002, ce qu'un test d'architecture a signalé.
"""

from __future__ import annotations

from typing import Any

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, Weekday
from common.money import Money

# Centre de Lomé.
LOME_CENTRE = Point(1.2255, 6.1319, srid=4326)

# Contour approximatif de la zone desservie. Provisoire et volontairement
# rectangulaire : le vrai contour doit être tracé par l'exploitation, sur carte.
LOME_ZONE = Polygon(
    ((1.15, 6.08), (1.30, 6.08), (1.30, 6.22), (1.15, 6.22), (1.15, 6.08)),
    srid=4326,
)


class Command(BaseCommand):
    help = "Crée le pays, la ville, la zone de livraison et le restaurant initiaux."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        country, _ = Country.objects.update_or_create(
            iso_code="TG",
            defaults={
                "name": "Togo",
                "currency": "XOF",
                "phone_prefix": "+228",
                "timezone": "Africa/Lome",
                "default_language": "fr",
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Pays : {country}"))

        city, _ = City.objects.update_or_create(
            country=country,
            slug="lome",
            defaults={"name": "Lomé", "centroid": LOME_CENTRE},
        )
        self.stdout.write(self.style.SUCCESS(f"Ville : {city}"))

        zone, _ = DeliveryZone.objects.update_or_create(
            city=city,
            name="Lomé — centre et périphérie",
            defaults={
                "boundary": MultiPolygon(LOME_ZONE, srid=4326),
                # Barème provisoire, à caler avec l'exploitation. Il remplace la
                # constante contradictoire de l'ancien système (5.00 côté
                # commande contre 500.0 côté panier, sans devise).
                "base_fee": Money(500, "XOF"),
                "fee_per_km": Money(100, "XOF"),
                "free_delivery_threshold": Money(15_000, "XOF"),
                "min_order_amount": Money(1_000, "XOF"),
                "max_distance_km": 15,
                "estimated_delivery_minutes": 35,
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Zone : {zone}"))

        restaurant, _ = Restaurant.objects.update_or_create(
            slug="el-corazon-lome",
            defaults={
                "name": "El Corazón",
                "zone": zone,
                "address": "Lomé, Togo",
                "location": LOME_CENTRE,
                "phone": "+22890000000",
                "default_preparation_minutes": 20,
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Restaurant : {restaurant}"))

        # 11h–23h tous les jours, service continu.
        for weekday in Weekday:
            OpeningHours.objects.update_or_create(
                restaurant=restaurant,
                weekday=weekday,
                opens_at="11:00",
                defaults={"closes_at": "23:00"},
            )
        self.stdout.write(self.style.SUCCESS("Horaires : 11h–23h, sept jours sur sept"))

        self.stdout.write(
            self.style.WARNING(
                "\nÀ caler avec l'exploitation avant mise en service : "
                "contour réel de la zone (tracé sur carte), barème de frais, "
                "adresse et téléphone du restaurant."
            )
        )
