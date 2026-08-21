"""Vérifie la configuration du stockage objet et récapitule les dossiers.

Appelée au démarrage de l'environnement de développement (`docker compose`) et
lors d'un déploiement. Idempotente, et désormais **sans effet de bord** : chez
Cloudinary il n'y a rien à provisionner.

    python manage.py ensure_storage_buckets

## Pourquoi la garder alors qu'elle ne crée plus rien

Deux raisons, et la seconde est la vraie.

D'abord parce qu'elle est câblée dans `docker-compose.yml` et
`docker-compose.prod.yml` : la retirer ferait échouer deux séquences de
démarrage pour un gain nul.

Ensuite parce qu'elle a changé de métier sans changer d'utilité. Du temps de S3,
elle créait les compartiments et posait leur politique de lecture — une étape de
provisionnement. Chez Cloudinary, un dossier n'est qu'un préfixe dans
l'identifiant d'une ressource : il naît au premier dépôt, et la visibilité est
portée par chaque objet via son type de livraison, décidé à l'envoi. Il ne reste
donc rien à créer, mais il reste quelque chose à **vérifier** : que le compte est
configuré. Sans les trois identifiants, tout envoi de fichier échouera à
l'exécution, sur une erreur d'authentification que personne ne rapproche d'une
variable oubliée. Le dire au démarrage vaut mieux que de le laisser découvrir au
premier utilisateur qui envoie un avatar.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from common.storage import PRIVATE_BUCKETS, PUBLIC_BUCKETS, StorageService, bucket_name


class Command(BaseCommand):
    help = "Vérifie la configuration Cloudinary et récapitule les dossiers de stockage."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Montre les dossiers, sans vérifier la configuration.",
        )

    def handle(self, *args: object, **options: Any) -> None:
        if options["dry_run"]:
            self._annoncer()
            return

        manquantes = [
            nom
            for nom in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
            if not getattr(settings, nom, "")
        ]
        if manquantes:
            # Échouer ici, au démarrage, vaut mieux que de laisser le premier
            # envoi de fichier d'un utilisateur découvrir le problème.
            raise CommandError(
                "Stockage objet non configuré — variables manquantes : " + ", ".join(manquantes)
            )

        try:
            StorageService.ensure_buckets()
        except Exception as exc:
            raise CommandError(f"Stockage objet inaccessible : {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(f"Cloudinary configuré (compte {settings.CLOUDINARY_CLOUD_NAME}).")
        )
        self.stdout.write(
            "Aucun dossier à créer : chez Cloudinary un dossier naît au premier dépôt."
        )
        self._annoncer()

    def _annoncer(self) -> None:
        for alias in PUBLIC_BUCKETS:
            self.stdout.write(f"public  {alias:<10} → {bucket_name(alias)}   (type=upload)")
        for alias in PRIVATE_BUCKETS:
            self.stdout.write(f"privé   {alias:<10} → {bucket_name(alias)}   (type=private)")
