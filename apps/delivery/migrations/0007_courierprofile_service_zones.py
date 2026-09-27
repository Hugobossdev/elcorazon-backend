"""Zones de service d'un livreur.

Additive : une table de liaison. Aucun livreur n'y figure au déploiement, ce qui
veut dire « toutes les zones de sa cuisine » — exactement le comportement
d'avant.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0006_une_seule_course_engagee_par_livreur"),
        ("geography", "0003_country_centroid_country_currency_symbol_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courierprofile",
            name="service_zones",
            field=models.ManyToManyField(
                blank=True,
                help_text="Vide : toutes les zones desservies par sa cuisine.",
                related_name="couriers",
                to="geography.deliveryzone",
            ),
        ),
    ]
