"""Séquence des références de commande.

Une séquence PostgreSQL plutôt qu'un compteur applicatif : deux commandes
simultanées obtiendraient le même numéro avec un `COUNT`, et le second `INSERT`
échouerait sur l'unicité de `reference` — un client sur deux verrait une erreur
aux heures de pointe, c'est-à-dire précisément quand il ne faut pas.
"""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("orders", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="CREATE SEQUENCE IF NOT EXISTS order_reference_seq START WITH 1 INCREMENT BY 1;",
            reverse_sql="DROP SEQUENCE IF EXISTS order_reference_seq;",
        ),
    ]
