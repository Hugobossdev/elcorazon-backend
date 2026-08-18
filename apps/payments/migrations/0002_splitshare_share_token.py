"""Jeton d'accès des parts de paiement partagé.

En **trois temps** et non en un seul, alors que la table est vide aujourd'hui.
Une `AddField` unique avec valeur par défaut évalue la fonction *une fois* et
pose la même valeur sur toutes les lignes existantes : sur une table peuplée,
la contrainte d'unicité échouerait au milieu de la migration, c'est-à-dire au
milieu d'un déploiement.

Écrire la version correcte coûte vingt lignes maintenant, et évite d'avoir à la
découvrir un jour où la table ne sera plus vide.
"""

from __future__ import annotations

from django.db import migrations, models

import apps.payments.models


def fill_tokens(apps_registry, schema_editor):  # type: ignore[no-untyped-def]
    """Un jeton distinct par ligne existante."""
    SplitShare = apps_registry.get_model("payments", "SplitShare")
    for share in SplitShare.objects.filter(share_token__isnull=True):
        share.share_token = apps.payments.models.new_share_token()
        share.save(update_fields=["share_token"])


class Migration(migrations.Migration):
    dependencies = [("payments", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="splitshare",
            name="share_token",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="splitshare",
            name="share_token",
            field=models.CharField(
                default=apps.payments.models.new_share_token, max_length=64, unique=True
            ),
        ),
    ]
