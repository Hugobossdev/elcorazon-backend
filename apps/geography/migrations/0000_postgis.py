"""Active PostGIS avant toute migration portant un champ géométrique.

Sous Docker Compose, l'extension n'a jamais eu à être demandée : l'image
`postgis/postgis` la pose dans `template1`, donc toute base créée depuis ce
gabarit la porte déjà. Un PostgreSQL géré — celui de Render, par exemple — est
un PostgreSQL nu. La toute première migration y échouait sur
`type "geometry" does not exist`, c'est-à-dire au premier déploiement, avant que
quoi que ce soit ne tourne.

`CreateExtension` émet `CREATE EXTENSION IF NOT EXISTS` : sur une base qui la
porte déjà — le compose, la CI, la base de test — cette migration ne fait rien.

Elle précède `geography.0001_initial` **et** `delivery.0001_initial`, qui sont
les deux seules migrations racines à déclarer un champ géométrique ; toutes les
autres en dépendent transitivement.
"""

from __future__ import annotations

from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [CreateExtension("postgis")]
