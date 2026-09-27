"""Géographie figée sur la commande — la reprise des commandes existantes.

Renseigne les commandes antérieures **au mieux**, et le dit :

* pays et ville viennent de la cuisine **telle qu'elle est rattachée au moment
  de la migration**. Une cuisine déplacée auparavant d'une ville à l'autre
  attribue donc toute son histoire à sa ville actuelle — c'est précisément le
  défaut que ces colonnes ferment pour l'avenir, et il n'existe aucune autre
  source pour le passé ;
* la zone est retrouvée par la **même règle** que `resolve_zone` (zone propre à
  la cuisine d'abord, puis priorité, puis plus petite surface), appliquée à
  l'adresse figée sur la commande, **zones désactivées comprises** : une zone
  retirée depuis a pu tarifer la course. Une adresse qu'aucun contour ne couvre
  plus garde une zone vide, plutôt qu'une zone inventée.

Rien n'est supprimé ni réécrit en dehors de ces quatre colonnes. Le retour
arrière retire les colonnes ; la reprise n'a rien à défaire.
"""

from __future__ import annotations

from typing import Any

from django.contrib.gis.db.models.functions import Area
from django.contrib.gis.geos import Point
from django.db import migrations
from django.db.models import BooleanField, Case, Q, Value, When


def renseigner_la_geographie(apps: Any, schema_editor: Any) -> None:
    Order = apps.get_model("orders", "Order")
    Restaurant = apps.get_model("restaurants", "Restaurant")
    DeliveryZone = apps.get_model("geography", "DeliveryZone")

    # Pays et ville : une mise à jour par cuisine, pas par commande.
    for cuisine in Restaurant.objects.select_related("zone__city").iterator():
        Order.objects.filter(restaurant_id=cuisine.pk, city__isnull=True).update(
            city_id=cuisine.zone.city_id, country_id=cuisine.zone.city.country_id
        )

    # La zone : une requête spatiale par commande, sur l'index GiST du contour.
    commandes = Order.objects.filter(delivery_zone__isnull=True).only(
        "pk", "restaurant_id", "city_id", "delivery_location"
    )
    for commande in commandes.iterator(chunk_size=500):
        position = commande.delivery_location or {}
        try:
            point = Point(float(position["lon"]), float(position["lat"]), srid=4326)
        except (KeyError, TypeError, ValueError):
            continue

        zone = (
            DeliveryZone.objects.filter(boundary__covers=point)
            .filter(
                Q(restaurant__isnull=True, city_id=commande.city_id)
                | Q(restaurant_id=commande.restaurant_id)
            )
            .annotate(
                surface=Area("boundary"),
                propre=Case(
                    When(restaurant__isnull=False, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
            )
            .order_by("-propre", "-priority", "surface")
            .only("pk", "name")
            .first()
        )
        if zone is not None:
            Order.objects.filter(pk=commande.pk).update(
                delivery_zone_id=zone.pk, delivery_zone_name=zone.name
            )


class Migration(migrations.Migration):
    dependencies = [
        ("geography", "0003_country_centroid_country_currency_symbol_and_more"),
        ("orders", "0006_order_geographie_index"),
        ("restaurants", "0007_restaurant_plafond_corrections_stock"),
    ]

    operations = [migrations.RunPython(renseigner_la_geographie, migrations.RunPython.noop)]
