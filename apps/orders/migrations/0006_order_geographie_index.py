"""Géographie figée sur la commande — les index de supervision et de rapport."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0005_order_geographie_figee")]

    operations = [
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["country", "-placed_at"], name="order_country_placed_idx"),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["city", "status", "-placed_at"], name="order_city_status_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["delivery_zone", "-placed_at"], name="order_zone_placed_idx"),
        ),
    ]
