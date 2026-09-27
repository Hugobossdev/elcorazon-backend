"""Les trois paliers que l'application cliente affichait jusqu'ici.

Repris tels quels — Standard dès 0, Fidèle dès 200, VIP dès 500 — pour que la
bascule vers une donnée serveur ne change rien de ce que voit un client, sinon
la base du calcul : les points cumulés gagnés plutôt que le solde (voir
`LoyaltyTier`). Ils s'éditent ensuite dans l'administration.

`get_or_create` sur le nom : rejouer la migration sur une base où un palier a
déjà été saisi à la main ne le duplique pas. Le retour arrière ne supprime rien
— un palier modifié entre-temps n'est plus celui que cette migration a posé.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations

PALIERS = (("Standard", 0), ("Fidèle", 200), ("VIP", 500))


def poser_les_paliers(apps: Any, schema_editor: Any) -> None:
    LoyaltyTier = apps.get_model("loyalty", "LoyaltyTier")
    for nom, seuil in PALIERS:
        if not LoyaltyTier.objects.filter(threshold=seuil).exists():
            LoyaltyTier.objects.get_or_create(name=nom, defaults={"threshold": seuil})


class Migration(migrations.Migration):
    dependencies = [
        ("loyalty", "0003_loyaltytier"),
    ]

    operations = [
        migrations.RunPython(poser_les_paliers, migrations.RunPython.noop),
    ]
