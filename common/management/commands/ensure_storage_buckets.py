"""Crée les compartiments de stockage manquants et pose leur politique.

Appelée au démarrage de l'environnement de développement (`docker compose`) et
lors d'un déploiement. Idempotente : la seconde exécution ne fait rien, ce qui
permet de la mettre inconditionnellement dans une commande de démarrage plutôt
que de la documenter comme une étape manuelle — les étapes manuelles se sautent.

    python manage.py ensure_storage_buckets
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from common.storage import PRIVATE_BUCKETS, PUBLIC_BUCKETS, StorageService, bucket_name


class Command(BaseCommand):
    help = "Crée les compartiments de stockage objet et applique leur politique de lecture."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Montre ce qui serait fait, sans rien créer.",
        )

    def handle(self, *args: object, **options: Any) -> None:
        if options["dry_run"]:
            self._annoncer()
            return

        try:
            crees = StorageService.ensure_buckets()
        except Exception as exc:
            # Le stockage est indisponible ou mal configuré. Échouer ici, au
            # démarrage, vaut mieux que de laisser le premier envoi de fichier
            # d'un utilisateur découvrir le problème en production.
            raise CommandError(f"Stockage objet inaccessible : {exc}") from exc

        if crees:
            self.stdout.write(self.style.SUCCESS(f"Compartiments créés : {', '.join(crees)}"))
        else:
            self.stdout.write("Tous les compartiments existent déjà.")

        self.stdout.write(
            "Politique de lecture anonyme appliquée aux compartiments publics : "
            + ", ".join(bucket_name(alias) for alias in PUBLIC_BUCKETS)
        )
        self.stdout.write(
            "Compartiments privés (aucune lecture non signée) : "
            + ", ".join(bucket_name(alias) for alias in PRIVATE_BUCKETS)
        )

    def _annoncer(self) -> None:
        for alias in PUBLIC_BUCKETS:
            self.stdout.write(f"public  {alias:<10} → {bucket_name(alias)}")
        for alias in PRIVATE_BUCKETS:
            self.stdout.write(f"privé   {alias:<10} → {bucket_name(alias)}")
