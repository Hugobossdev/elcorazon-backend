"""Géographie figée sur la commande — les colonnes.

Quatre colonnes nullables, sans index ni reprise : ceux-ci suivent dans `0006`
et `0007`, chacun dans **sa** transaction. Réunis ici, PostgreSQL refusait de
créer l'index (« pending trigger events ») sur une table qui venait de recevoir
ses clés étrangères différées dans la même transaction.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("geography", "0003_country_centroid_country_currency_symbol_and_more"),
        ("orders", "0004_idempotencykey_completed_at"),
        ("restaurants", "0007_restaurant_plafond_corrections_stock"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="country",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="orders",
                to="geography.country",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="city",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="orders",
                to="geography.city",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_zone",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="orders",
                to="geography.deliveryzone",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_zone_name",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
