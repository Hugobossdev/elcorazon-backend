"""Recopie vers Cloudinary les fichiers déposés du temps de MinIO.

    python manage.py migrate_media_to_cloudinary --dry-run
    python manage.py migrate_media_to_cloudinary --source http://minio:9000

## Ce qu'elle fait, et surtout ce qu'elle ne fait pas

Elle **ne touche ni à MinIO ni à la base**. Elle lit les octets à la source, les
dépose chez Cloudinary sous *le même chemin relatif*, et s'arrête là.

C'est possible parce que la colonne ne contient qu'un chemin — `menu/brownie.jpg`
— et jamais une URL complète. Le même chemin désignant désormais un objet
Cloudinary, les lignes existantes restent valides sans qu'on les réécrive. Une
migration de données qui n'écrit rien en base est une migration qu'on peut
rejouer, interrompre, et recommencer sans dégât.

Si malgré tout le stockage rendait un chemin différent de celui demandé — ce qui
n'arrive qu'en cas de collision — la commande met la ligne à jour et le signale.
Le cas est traité parce qu'il est silencieux : une colonne pointant sur un
fichier absent ne se voit qu'à l'affichage, longtemps après.

## Idempotence

Un fichier déjà présent chez Cloudinary est sauté. La commande peut donc être
relancée après une coupure, et ne recopie que ce qui manque.

## Périmètre

Tous les champs fichier de tous les modèles installés, trouvés par introspection
plutôt qu'énumérés : un champ ajouté demain est couvert sans qu'on y pense.
C'est la même mécanique que `common/files.py`, et pour la même raison — une liste
écrite à la main est une liste qui vieillit mal.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from django.apps import apps
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandParser
from django.db.models import FileField, Model

from common.storage import bucket_name

#: Point d'accès du MinIO d'origine. `minio:9000` est le nom du service dans le
#: réseau Docker ; depuis l'hôte, c'est `localhost:9000`.
SOURCE_PAR_DEFAUT = "http://minio:9000"


class Command(BaseCommand):
    help = "Recopie vers Cloudinary les fichiers encore hébergés par MinIO."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--source",
            default=SOURCE_PAR_DEFAUT,
            help=f"Point d'accès MinIO d'origine (défaut : {SOURCE_PAR_DEFAUT}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Liste ce qui serait recopié, sans rien déposer.",
        )

    def handle(self, *args: object, **options: Any) -> None:
        source = str(options["source"]).rstrip("/")
        simulation = bool(options["dry_run"])

        recopies = sautes = echecs = 0

        for modele, champs in self._champs_fichier():
            for champ in champs:
                for instance in modele.objects.exclude(**{champ: ""}).exclude(
                    **{f"{champ}__isnull": True}
                ):
                    fichier = getattr(instance, champ)
                    if not fichier:
                        continue

                    resultat = self._traiter(
                        instance=instance,
                        champ=champ,
                        source=source,
                        simulation=simulation,
                    )
                    if resultat == "recopie":
                        recopies += 1
                    elif resultat == "saute":
                        sautes += 1
                    else:
                        echecs += 1

        self.stdout.write("")
        resume = f"{recopies} recopié(s), {sautes} déjà présent(s), {echecs} en échec."
        if echecs:
            self.stdout.write(self.style.WARNING(resume))
        else:
            self.stdout.write(self.style.SUCCESS(resume))

    # -- une ligne, un champ --------------------------------------------

    def _traiter(self, *, instance: Model, champ: str, source: str, simulation: bool) -> str:
        fichier = getattr(instance, champ)
        chemin = fichier.name
        stockage = fichier.storage
        alias = getattr(stockage, "bucket_alias", "products")
        etiquette = f"{instance._meta.label}.{champ} → {chemin}"

        try:
            if stockage.exists(chemin):
                self.stdout.write(f"  déjà là   {etiquette}")
                return "saute"
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  ÉCHEC     {etiquette} — {exc}"))
            return "echec"

        adresse = f"{source}/{bucket_name(alias)}/{quote(chemin)}"

        if simulation:
            self.stdout.write(f"  à copier  {etiquette}  (depuis {adresse})")
            return "recopie"

        try:
            reponse = httpx.get(adresse, timeout=60, follow_redirects=True)
            reponse.raise_for_status()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  ÉCHEC     {etiquette} — lecture : {exc}"))
            return "echec"

        try:
            ecrit = stockage.save(chemin, ContentFile(reponse.content))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  ÉCHEC     {etiquette} — dépôt : {exc}"))
            return "echec"

        if ecrit != chemin:
            # Collision : le stockage a suffixé le nom. La colonne doit suivre,
            # sans quoi elle désignerait un fichier qui n'existe pas.
            setattr(instance, champ, ecrit)
            instance.save(update_fields=[champ])
            self.stdout.write(
                self.style.WARNING(f"  RENOMMÉ   {etiquette} → {ecrit} (colonne mise à jour)")
            )
        else:
            self.stdout.write(f"  copié     {etiquette}")

        return "recopie"

    # -- introspection --------------------------------------------------

    def _champs_fichier(self) -> list[tuple[type[Model], list[str]]]:
        """Modèles portant au moins un champ fichier, et lesquels."""
        trouves: list[tuple[type[Model], list[str]]] = []
        for modele in apps.get_models():
            champs = [
                champ.name for champ in modele._meta.get_fields() if isinstance(champ, FileField)
            ]
            if champs:
                trouves.append((modele, champs))
        return trouves
