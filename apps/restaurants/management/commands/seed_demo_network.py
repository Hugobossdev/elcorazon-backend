"""Réseau de démonstration : trois marchés, trois villes, trois établissements.

Sert à vérifier l'**isolation** — que le catalogue, les prix, les commandes et
la flotte d'un établissement ne débordent pas sur un autre. Un jeu de données
homogène ne le montrerait pas : si les trois restaurants vendaient la même chose
au même prix, une fuite d'un établissement à l'autre resterait invisible. Les
trois sont donc délibérément différents, et la commande sœur
`seed_demo_network_catalog` (dans `catalog`, qui a le droit de connaître les
articles) creuse l'écart côté carte.

**Elle ne touche pas à l'existant.** El Corazón Lomé, son catalogue, ses
commandes et ses livreurs sont laissés exactement en l'état : la commande
n'écrit que sur les slugs qu'elle crée, et son `--defaire` ne supprime que
ceux-là.

Deux commandes et non une, parce que le graphe de l'ADR-002 l'impose :
`restaurants` peut connaître `geography` mais pas `catalog`, et `catalog` peut
connaître `restaurants` mais pas `geography`. Aucune application ne voit les
trois, et en inventer une pour ce besoin coûterait plus cher que deux appels.
"""

from __future__ import annotations

from typing import Any

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, RestaurantStatus, Weekday
from common.money import Money


def carre(latitude: float, longitude: float, demi_cote: float = 0.07) -> MultiPolygon:
    """Contour rectangulaire autour d'un point.

    Provisoire et volontairement grossier, comme celui du seed de référence : le
    vrai contour se trace sur carte par l'exploitation. Un carré de 0,07° de
    demi-côté couvre environ 15 km, ce qui suffit à contenir les points de test.
    """
    return MultiPolygon(
        Polygon(
            (
                (longitude - demi_cote, latitude - demi_cote),
                (longitude + demi_cote, latitude - demi_cote),
                (longitude + demi_cote, latitude + demi_cote),
                (longitude - demi_cote, latitude + demi_cote),
                (longitude - demi_cote, latitude - demi_cote),
            ),
            srid=4326,
        ),
        srid=4326,
    )


#: Les deux marchés ajoutés. Le Togo existe déjà — il n'est pas retouché.
MARCHES: list[dict[str, Any]] = [
    {
        "iso_code": "CI",
        "name": "Côte d'Ivoire",
        "currency": "XOF",
        "phone_prefix": "+225",
        "timezone": "Africa/Abidjan",
        "ville": {"slug": "abidjan", "name": "Abidjan", "lat": 5.3600, "lon": -4.0083},
        "zone": {
            "name": "Abidjan — Plateau et Cocody",
            # Barème **différent** de celui de Lomé, à dessein : un frais qui
            # sortirait du mauvais barème se verrait immédiatement.
            "base_fee": 800,
            "fee_per_km": 150,
            "free_delivery_threshold": 20_000,
            "min_order_amount": 2_000,
            "estimated_delivery_minutes": 40,
        },
        "restaurant": {
            "slug": "el-corazon-abidjan",
            "name": "El Corazón Abidjan",
            "address": "Rue des Jardins, Cocody, Abidjan",
            "lat": 5.3610,
            "lon": -4.0070,
            "phone": "+22507000000",
            "preparation": 25,
        },
        # Service continu, comme Lomé : ce n'est pas l'horaire qu'on cherche à
        # distinguer ici.
        "horaires": ("11:00", "23:00"),
    },
    {
        "iso_code": "BJ",
        "name": "Bénin",
        "currency": "XOF",
        "phone_prefix": "+229",
        "timezone": "Africa/Porto-Novo",
        "ville": {"slug": "cotonou", "name": "Cotonou", "lat": 6.3703, "lon": 2.3912},
        "zone": {
            "name": "Cotonou — centre",
            "base_fee": 600,
            "fee_per_km": 120,
            "free_delivery_threshold": None,
            "min_order_amount": None,
            "estimated_delivery_minutes": 30,
        },
        "restaurant": {
            "slug": "el-corazon-cotonou",
            "name": "El Corazón Cotonou",
            "address": "Boulevard Steinmetz, Cotonou",
            "lat": 6.3690,
            "lon": 2.3930,
            "phone": "+22990000000",
            "preparation": 15,
        },
        # Horaires **différents** : un établissement qu'on interroge en dehors
        # de sa plage doit se dire fermé alors que les autres sont ouverts.
        "horaires": ("18:00", "02:00"),
    },
]


class Command(BaseCommand):
    help = (
        "Ouvre deux marchés de démonstration (Abidjan, Cotonou) à côté de "
        "l'existant, pour vérifier l'isolation entre établissements."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--defaire",
            action="store_true",
            help=(
                "Retire les établissements, zones, villes et pays de démonstration. "
                "Refuse de supprimer ce à quoi des commandes renvoient."
            ),
        )
        parser.add_argument(
            "--publier",
            action="store_true",
            help=(
                "Met les établissements en service à la fin. Échoue si leur "
                "configuration est incomplète — c'est le but."
            ),
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        if options["defaire"]:
            self._defaire()
            return

        for marche in MARCHES:
            self._ouvrir(marche, publier=options["publier"])

        self.stdout.write(
            self.style.WARNING(
                "\nLe catalogue reste à poser : "
                "`python manage.py seed_demo_network_catalog`. "
                "Sans lui, ces établissements ne peuvent pas être mis en service."
            )
        )

    # ------------------------------------------------------------ ouverture

    def _ouvrir(self, marche: dict[str, Any], *, publier: bool) -> None:
        pays, _ = Country.objects.update_or_create(
            iso_code=marche["iso_code"],
            defaults={
                "name": marche["name"],
                "currency": marche["currency"],
                "phone_prefix": marche["phone_prefix"],
                "timezone": marche["timezone"],
                "default_language": "fr",
            },
        )

        ville_decrite = marche["ville"]
        ville, _ = City.objects.update_or_create(
            country=pays,
            slug=ville_decrite["slug"],
            defaults={
                "name": ville_decrite["name"],
                "centroid": Point(ville_decrite["lon"], ville_decrite["lat"], srid=4326),
            },
        )

        zone_decrite = marche["zone"]
        devise = marche["currency"]
        zone, _ = DeliveryZone.objects.update_or_create(
            city=ville,
            name=zone_decrite["name"],
            defaults={
                "boundary": carre(ville_decrite["lat"], ville_decrite["lon"]),
                "base_fee": Money(zone_decrite["base_fee"], devise),
                "fee_per_km": Money(zone_decrite["fee_per_km"], devise),
                "free_delivery_threshold": (
                    None
                    if zone_decrite["free_delivery_threshold"] is None
                    else Money(zone_decrite["free_delivery_threshold"], devise)
                ),
                "min_order_amount": (
                    None
                    if zone_decrite["min_order_amount"] is None
                    else Money(zone_decrite["min_order_amount"], devise)
                ),
                "max_distance_km": 15,
                "estimated_delivery_minutes": zone_decrite["estimated_delivery_minutes"],
            },
        )

        decrit = marche["restaurant"]
        etablissement, cree = Restaurant.objects.update_or_create(
            slug=decrit["slug"],
            defaults={
                "name": decrit["name"],
                "zone": zone,
                "address": decrit["address"],
                "location": Point(decrit["lon"], decrit["lat"], srid=4326),
                "phone": decrit["phone"],
                "default_preparation_minutes": decrit["preparation"],
            },
        )
        if cree:
            etablissement.status = RestaurantStatus.CONFIGURING
            etablissement.save(update_fields=["status"])

        ouvre, ferme = marche["horaires"]
        for jour in Weekday:
            OpeningHours.objects.update_or_create(
                restaurant=etablissement,
                weekday=jour,
                opens_at=ouvre,
                defaults={"closes_at": ferme},
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{etablissement.name} — {ville.name} ({pays.iso_code}), "
                f"{devise}, {ouvre}–{ferme}, "
                f"forfait {zone_decrite['base_fee']} {devise}"
            )
        )

        if publier:
            self._publier(etablissement)

    def _publier(self, etablissement: Restaurant) -> None:
        manques = etablissement.configuration_gaps()
        if manques:
            self.stdout.write(
                self.style.WARNING(
                    f"  {etablissement.name} reste en configuration :\n    - "
                    + "\n    - ".join(manques)
                )
            )
            return

        # Le brouillon ne se publie pas d'un bond : la machine impose de passer
        # par « prêt », ce qui est le moment où l'on relit la fiche.
        if etablissement.status == RestaurantStatus.CONFIGURING:
            etablissement.transition_to(RestaurantStatus.READY)
        etablissement.transition_to(RestaurantStatus.ACTIVE)
        self.stdout.write(self.style.SUCCESS(f"  {etablissement.name} : en service"))

    # -------------------------------------------------------------- retrait

    def _defaire(self) -> None:
        """Retire ce que cette commande a créé, et rien d'autre.

        La suppression est laissée aux clés étrangères `PROTECT` : un pays, une
        ville ou une zone auxquels une commande, une adresse ou un dossier
        livreur renvoient font échouer le `DELETE` en violation d'intégrité.
        C'est le comportement voulu — on ne défait pas un marché sur lequel des
        gens ont commandé —, et le message le dit plutôt que de le laisser
        sortir en trace Python.
        """
        slugs = [marche["restaurant"]["slug"] for marche in MARCHES]
        villes = [marche["ville"]["slug"] for marche in MARCHES]
        pays = [marche["iso_code"] for marche in MARCHES]

        try:
            supprimes, _ = Restaurant.objects.filter(slug__in=slugs).delete()
            DeliveryZone.objects.filter(city__slug__in=villes).delete()
            City.objects.filter(slug__in=villes).delete()
            Country.objects.filter(iso_code__in=pays).delete()
        except Exception as erreur:
            self.stderr.write(
                self.style.ERROR(
                    "Retrait impossible : des commandes, adresses ou dossiers "
                    f"livreurs renvoient à ces données. ({erreur})"
                )
            )
            raise

        self.stdout.write(
            self.style.SUCCESS(f"Réseau de démonstration retiré ({supprimes} objets).")
        )
