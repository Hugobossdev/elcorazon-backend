"""Affectation automatique des livreurs, réglable par cuisine.

Additive, avec un défaut vrai : les cuisines existantes proposent désormais la
course d'elles-mêmes quand une commande est prête. L'affectation manuelle reste
possible, et la cuisine qui préfère la garder seule décoche le réglage.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("restaurants", "0008_fermetures_exceptionnelles")]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="auto_dispatch_couriers",
            field=models.BooleanField(
                default=True,
                help_text="Proposer automatiquement la course à un livreur compatible dès que "
                "la commande est prête.",
            ),
        ),
    ]
