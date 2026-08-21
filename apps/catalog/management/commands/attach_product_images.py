"""Rattache aux articles les photos déjà déposées sur le stockage objet.

    python manage.py attach_product_images --dry-run
    python manage.py attach_product_images

`seed_full_catalog` sait poser la carte avec ou sans photos : `--with-images`
télécharge les images depuis des hébergeurs tiers et les dépose sur le stockage
objet. Le démarrage de production s'en passe délibérément (`deploy/start-api.sh`)
— cela ferait dépendre la mise en service d'un CDN et allongerait le boot de
plusieurs minutes. La carte s'affiche donc là-bas sans photos : les lignes
existent, leur colonne `image` est vide.

Or les fichiers, eux, sont déjà en place — déposés depuis un autre
environnement, ou recopiés par `migrate_media_to_cloudinary`. Le stockage étant
le même compte pour tous les environnements, il ne manque que le lien.

**Cette commande ne dépose rien.** Elle écrit un chemin relatif dans une colonne,
et seulement après avoir vérifié auprès du stockage que le fichier existe : une
colonne renseignée vers un objet absent produirait un 404 côté client, c'est-à-
dire exactement le défaut qu'on cherche à corriger. Un article dont la photo
manque reste donc sans photo, ce qui est un état affichable.

Rejouable sans dommage : ce qui est déjà rattaché est sauté, sauf `--replace`.
Un article déjà lié ne coûte aucun appel réseau, ce qui rend les démarrages
suivants gratuits.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.catalog.models import MenuItem

#: Emplacement d'une photo d'article : `upload_to="menu/"` du champ, plus le nom
#: que `seed_full_catalog` donne au fichier — le slug de l'article, en `.jpg`.
#: La convention est ici plutôt que devinée par un inventaire du stockage, que
#: `ObjectStorage.listdir` refuse justement de fournir.
GABARIT = "menu/{slug}.jpg"


class Command(BaseCommand):
    help = "Rattache aux articles les photos déjà présentes sur le stockage objet."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Liste ce qui serait rattaché, sans rien écrire en base.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Réécrit aussi les articles qui portent déjà une photo.",
        )

    def handle(self, *args: object, **options: Any) -> None:
        simulation = bool(options["dry_run"])
        remplacer = bool(options["replace"])
        stockage = MenuItem._meta.get_field("image").storage

        rattaches = sautes = absents = injoignables = 0

        for article in MenuItem.objects.order_by("slug").iterator():
            if article.image and not remplacer:
                sautes += 1
                continue

            chemin = GABARIT.format(slug=article.slug)

            # Le stockage a le dernier mot : sans ce contrôle, la commande
            # échangerait une carte sans photos contre une carte de 404.
            #
            # L'interrogation est protégée parce que cette commande tourne au
            # démarrage, sous `set -e` : un fournisseur momentanément injoignable
            # doit laisser la carte sans photos — un état affichable, que le
            # redémarrage suivant rattrapera — et non refuser le déploiement.
            try:
                present = stockage.exists(chemin)
            except Exception as exc:
                injoignables += 1
                self.stdout.write(self.style.ERROR(f"  injoignable {article.slug} — {exc}"))
                continue

            if not present:
                absents += 1
                self.stdout.write(self.style.WARNING(f"  absente     {article.slug} ({chemin})"))
                continue

            if simulation:
                rattaches += 1
                self.stdout.write(f"  à lier      {article.slug} → {chemin}")
                continue

            article.image = chemin
            article.save(update_fields=["image"])
            rattaches += 1
            self.stdout.write(f"  lié         {article.slug} → {chemin}")

        self.stdout.write("")
        verbe = "à rattacher" if simulation else "rattaché(s)"
        resume = f"{rattaches} {verbe}, {sautes} déjà lié(s), {absents} sans fichier"
        if injoignables:
            resume += f", {injoignables} non vérifiable(s)"
        style = self.style.WARNING if (absents or injoignables) else self.style.SUCCESS
        self.stdout.write(style(f"{resume}."))
