"""Crée l'extension PostGIS avant toute table portant une colonne géométrique.

`geography/0001` et `delivery/0001` déclarent tous deux des champs GeoDjango et
n'ont aucune dépendance l'un envers l'autre : le graphe de migrations n'impose
donc pas lequel passe en premier. Poser l'extension dans l'un des deux laisserait
l'autre échouer dès que Django choisit l'ordre inverse.

D'où `run_before` plutôt qu'une opération glissée dans une migration existante :
la contrainte est déclarée ici, dans le seul fichier qui en porte la
responsabilité, sans toucher au code engendré par `makemigrations`.

Sous Docker Compose, l'image `postgis/postgis` fournit déjà l'extension et
l'opération ne fait rien — elle émet `CREATE EXTENSION IF NOT EXISTS`. Ce fichier
existe pour les bases managées (Render, RDS), qui livrent un PostgreSQL nu.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    run_before = [
        ("geography", "0001_initial"),
        ("delivery", "0001_initial"),
    ]

    # `GIS_ENABLED=False` sert le sous-ensemble non géospatial de la suite sur un
    # poste dépourvu de GDAL ; la base n'y est pas PostGIS, et réclamer
    # l'extension y ferait échouer `migrate` pour rien.
    operations = [CreateExtension("postgis")] if settings.GIS_ENABLED else []
