"""Fermetures exceptionnelles des cuisines.

Additive : une table, une contrainte, un index. Aucune donnée existante n'est
touchée — une cuisine sans fermeture se comporte exactement comme avant.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import common.identifiers


class Migration(migrations.Migration):
    dependencies = [
        ("restaurants", "0007_restaurant_plafond_corrections_stock"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KitchenClosure",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=common.identifiers.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("starts_at", models.DateTimeField()),
                ("ends_at", models.DateTimeField()),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        help_text="Montré au client : « fermeture exceptionnelle (jour férié) ».",
                        max_length=200,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "restaurant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="closures",
                        to="restaurants.restaurant",
                    ),
                ),
            ],
            options={
                "verbose_name": "fermeture exceptionnelle",
                "verbose_name_plural": "fermetures exceptionnelles",
                "ordering": ["starts_at"],
                "indexes": [
                    models.Index(
                        fields=["restaurant", "ends_at"], name="closure_restaurant_end_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("ends_at__gt", models.F("starts_at"))),
                        name="kitchen_closure_ends_after_start",
                    )
                ],
            },
        ),
    ]
