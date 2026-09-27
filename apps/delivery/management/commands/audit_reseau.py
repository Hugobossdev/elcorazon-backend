"""Audit de cohérence du réseau de cuisines — lecture seule.

    python manage.py audit_reseau            # rapport
    python manage.py audit_reseau --strict   # code de sortie 1 s'il reste une anomalie

## Ce qu'il cherche

Les incohérences que le schéma **laisse passer** — celles qu'il rend
impossibles (une ville sans pays, une cuisine sans zone, une zone sans ville)
sont tenues par des clés non nulles et n'ont pas à être cherchées :

* une cuisine rattachée à la zone **propre** d'une autre cuisine ;
* une cuisine en service dont le point de retrait tombe hors de sa zone ;
* une cuisine en service sur un marché fermé (zone, ville ou pays désactivé) ;
* une cuisine en service sans aucune plage d'ouverture — donc fermée pour
  toujours, sans que rien ne le dise ;
* une zone propre posée dans une autre ville que celle de sa cuisine ;
* deux cuisines homonymes dans la même ville ;
* un livreur affecté à une zone que sa cuisine ne dessert pas ;
* des commandes sans géographie figée (antérieures, et que la reprise n'a pas
  su situer).

## Ce qu'il ne fait pas

**Rien écrire.** Une anomalie de réseau se corrige depuis le back-office, là où
la garde de périmètre et le journal d'audit s'appliquent ; une commande qui
« réparerait » en silence déplacerait des cuisines ou retirerait des zones
qu'une personne a dessinées.

## Pourquoi dans `delivery`

C'est le seul module que le graphe de l'ADR-002 autorise à voir à la fois la
géographie, les cuisines, les commandes et la flotte. En créer un pour une
commande en lecture seule coûterait plus qu'il ne rapporte.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F, Q

from apps.delivery.models import CourierProfile
from apps.geography.models import DeliveryZone
from apps.orders.models import Order
from apps.restaurants.models import Restaurant, zone_anchoring_problem


class Command(BaseCommand):
    help = "Audite la cohérence pays → ville → zone → cuisine → commandes et flotte. N'écrit rien."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Sort en erreur (code 1) s'il reste au moins une anomalie.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        anomalies: dict[str, list[str]] = defaultdict(list)

        cuisines = list(
            Restaurant.objects.select_related("zone__city__country").prefetch_related(
                "opening_hours"
            )
        )
        for cuisine in cuisines:
            nom = f"{cuisine.name} ({cuisine.slug})"
            if (probleme := zone_anchoring_problem(cuisine.zone, cuisine.pk)) is not None:
                anomalies["Cuisine sur la zone propre d'une autre"].append(f"{nom} — {probleme}")
            if not cuisine.is_active:
                continue
            if not cuisine.zone.boundary.covers(cuisine.location):
                anomalies["Cuisine en service hors de sa zone"].append(nom)
            fermes = [
                etage
                for etage, ouvert in (
                    (f"zone « {cuisine.zone.name} »", cuisine.zone.is_active),
                    (f"ville « {cuisine.zone.city.name} »", cuisine.zone.city.is_active),
                    (
                        f"pays « {cuisine.zone.city.country.name} »",
                        cuisine.zone.city.country.is_active,
                    ),
                )
                if not ouvert
            ]
            if fermes:
                anomalies["Cuisine en service sur un marché fermé"].append(
                    f"{nom} — {', '.join(fermes)} désactivé(e)"
                )
            if not cuisine.opening_hours.all():
                anomalies["Cuisine en service sans horaires"].append(nom)

        for zone in (
            DeliveryZone.objects.filter(restaurant__isnull=False)
            .exclude(city_id=F("restaurant__zone__city_id"))
            .select_related("city", "restaurant__zone__city")
        ):
            assert zone.restaurant is not None
            anomalies["Zone propre hors de la ville de sa cuisine"].append(
                f"« {zone.name} » ({zone.city.name}) → {zone.restaurant.name} "
                f"({zone.restaurant.zone.city.name})"
            )

        homonymes = (
            Restaurant.objects.values("zone__city__name", "name")
            .annotate(nombre=Count("id"))
            .filter(nombre__gt=1)
        )
        for ligne in homonymes:
            anomalies["Cuisines homonymes dans une même ville"].append(
                f"« {ligne['name']} » × {ligne['nombre']} à {ligne['zone__city__name']}"
            )

        for livreur in CourierProfile.objects.select_related(
            "user", "restaurant__zone"
        ).prefetch_related("service_zones"):
            cuisine = livreur.restaurant
            for zone in livreur.service_zones.all():
                desservie = zone.restaurant_id == cuisine.pk or (
                    zone.restaurant_id is None and zone.city_id == cuisine.zone.city_id
                )
                if not desservie:
                    anomalies["Livreur affecté hors de la desserte de sa cuisine"].append(
                        f"{livreur.user.full_name} ({cuisine.name}) → « {zone.name} »"
                    )

        sans_geo = Order.objects.filter(
            Q(country__isnull=True) | Q(city__isnull=True) | Q(delivery_zone__isnull=True)
        ).count()
        if sans_geo:
            anomalies["Commandes sans géographie figée"].append(
                f"{sans_geo} commande(s) sans pays, ville ou zone — antérieures à la "
                "géographie figée, adresse qu'aucun contour ne couvre."
            )

        self._rendre(anomalies, total_cuisines=len(cuisines))

        if options["strict"] and anomalies:
            raise CommandError(f"{sum(len(v) for v in anomalies.values())} anomalie(s) de réseau.")

    def _rendre(self, anomalies: dict[str, list[str]], *, total_cuisines: int) -> None:
        self.stdout.write(f"Réseau audité : {total_cuisines} cuisine(s).")
        if not anomalies:
            self.stdout.write(self.style.SUCCESS("Aucune anomalie."))
            return
        for rubrique, lignes in anomalies.items():
            self.stdout.write(self.style.WARNING(f"\n{rubrique} — {len(lignes)}"))
            for ligne in lignes:
                self.stdout.write(f"  • {ligne}")
