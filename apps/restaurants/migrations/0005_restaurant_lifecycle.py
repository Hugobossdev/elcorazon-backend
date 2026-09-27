"""Cycle de vie de l'établissement — `status`, et `is_active` qui en découle.

La reprise ne perd rien et ne publie rien de nouveau : chaque établissement déjà
actif devient « en service », chaque établissement déjà retiré devient
« suspendu ». El Corazón Lomé traverse donc la migration sans changer d'état,
et la fonction inverse rétablit exactement le booléen d'origine — ce qui rend
le retour arrière possible tant que rien n'est passé par les trois nouveaux
états.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations, models


def statut_depuis_le_booleen(apps: Any, schema_editor: Any) -> None:
    Restaurant = apps.get_model("restaurants", "Restaurant")
    Restaurant.objects.filter(is_active=True).update(status="active")
    Restaurant.objects.filter(is_active=False).update(status="inactive")


def booleen_depuis_le_statut(apps: Any, schema_editor: Any) -> None:
    """Retour arrière.

    Les états de configuration n'ont pas d'équivalent booléen : un brouillon
    n'est pas « actif », et le rendre tel le publierait. Ils redescendent donc
    sur `False`, qui est le sens le plus proche — pas encore ouvert.
    """
    Restaurant = apps.get_model("restaurants", "Restaurant")
    Restaurant.objects.filter(status="active").update(is_active=True)
    Restaurant.objects.exclude(status="active").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("restaurants", "0004_alter_restaurant_cover_image")]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Brouillon"),
                    ("configuring", "En configuration"),
                    ("ready", "Prêt à ouvrir"),
                    ("active", "En service"),
                    ("inactive", "Suspendu"),
                ],
                default="draft",
                help_text="Un établissement n'est visible du public qu'en service.",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="restaurant",
            name="is_active",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="restaurant",
            index=models.Index(fields=["status"], name="restaurants_status_idx"),
        ),
        # **Après** l'ajout de la colonne et avant tout usage : la reprise lit
        # `is_active`, qui est encore la vérité à cet instant.
        migrations.RunPython(statut_depuis_le_booleen, booleen_depuis_le_statut),
    ]
