"""Cartes des établissements de démonstration.

Commande sœur de `seed_demo_network`, qui pose la géographie et les
établissements. Elles sont séparées parce que le graphe de l'ADR-002 l'impose :
`restaurants` connaît `geography` mais pas `catalog`, et `catalog` connaît
`restaurants` mais pas `geography`. Aucune application ne voit les trois, et en
inventer une pour ce seul besoin coûterait plus cher que deux appels.

## Ce que ces données servent à prouver

L'**isolation**. Trois catalogues identiques ne montreraient rien : une fuite
d'un établissement à l'autre passerait inaperçue. Les écarts sont donc choisis
pour qu'une confusion se voie tout de suite :

* un même article, `poulet-braise`, existe à Lomé **et** à Abidjan, à deux prix
  différents (2 500 contre 3 200) : un prix qui traverserait la frontière se
  lirait à l'euro près ;
* Cotonou ne le vend pas du tout et propose un article que personne d'autre n'a
  (`poisson-braise`) : une carte qui déborderait ferait apparaître un plat là
  où il n'existe pas ;
* les catégories portent les mêmes slugs (`plats`) dans les trois
  établissements, ce qui est légal — la contrainte d'unicité est *par
  restaurant* — et vérifie précisément que le cloisonnement se fait sur la clé
  du restaurant et non sur le slug.

**El Corazón Lomé n'est pas touché.** Ses cinquante articles restent en place :
cette commande n'écrit que sur les deux slugs de démonstration, et se contente
de *lire* Lomé pour rappeler l'écart de prix.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Category, MenuItem
from apps.restaurants.models import Restaurant
from common.money import Money

#: Cartes de démonstration, par slug d'établissement.
CARTES: dict[str, dict[str, Any]] = {
    "el-corazon-abidjan": {
        "devise": "XOF",
        "categories": [
            {"slug": "plats", "name": "Plats", "emoji": "🍽️", "ordre": 1},
            {"slug": "boissons", "name": "Boissons", "emoji": "🥤", "ordre": 2},
        ],
        "articles": [
            {
                "slug": "poulet-braise",
                "name": "Poulet braisé",
                "categorie": "plats",
                # Le même plat qu'à Lomé, à un prix différent : c'est l'écart
                # qui rend une fuite de prix visible.
                "prix": 3_200,
                "populaire": True,
                "preparation": 25,
            },
            {
                "slug": "attieke-poisson",
                "name": "Attiéké poisson",
                "categorie": "plats",
                "prix": 2_800,
                "populaire": True,
                "preparation": 20,
            },
            {
                "slug": "bissap",
                "name": "Bissap",
                "categorie": "boissons",
                "prix": 700,
                "populaire": False,
                "preparation": 5,
            },
        ],
    },
    "el-corazon-cotonou": {
        "devise": "XOF",
        "categories": [
            {"slug": "plats", "name": "Plats", "emoji": "🍽️", "ordre": 1},
        ],
        "articles": [
            {
                # Article que **personne d'autre** ne vend : s'il apparaît sur
                # la carte d'un autre établissement, le cloisonnement est cassé.
                "slug": "poisson-braise",
                "name": "Poisson braisé",
                "categorie": "plats",
                "prix": 2_900,
                "populaire": True,
                "preparation": 30,
            },
        ],
    },
}


class Command(BaseCommand):
    help = (
        "Pose des cartes différenciées sur les établissements de démonstration, "
        "pour vérifier qu'un catalogue ne déborde pas sur un autre."
    )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for slug, carte in CARTES.items():
            etablissement = Restaurant.objects.filter(slug=slug).first()
            if etablissement is None:
                self.stderr.write(
                    self.style.ERROR(
                        f"{slug} n'existe pas. Lancez d'abord `python manage.py seed_demo_network`."
                    )
                )
                continue
            self._poser(etablissement, carte)

        self._rappeler_les_ecarts()

    def _poser(self, etablissement: Restaurant, carte: dict[str, Any]) -> None:
        categories: dict[str, Category] = {}
        for decrite in carte["categories"]:
            categorie, _ = Category.objects.update_or_create(
                restaurant=etablissement,
                slug=decrite["slug"],
                defaults={
                    "name": decrite["name"],
                    "emoji": decrite["emoji"],
                    "sort_order": decrite["ordre"],
                    "is_active": True,
                },
            )
            categories[decrite["slug"]] = categorie

        devise = carte["devise"]
        for index, decrit in enumerate(carte["articles"]):
            MenuItem.objects.update_or_create(
                restaurant=etablissement,
                slug=decrit["slug"],
                defaults={
                    "category": categories[decrit["categorie"]],
                    "name": decrit["name"],
                    "price": Money(decrit["prix"], devise),
                    "preparation_minutes": decrit["preparation"],
                    "is_available": True,
                    "is_popular": decrit["populaire"],
                    "sort_order": index,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{etablissement.name} : {len(carte['categories'])} catégorie(s), "
                f"{len(carte['articles'])} article(s)"
            )
        )

    def _rappeler_les_ecarts(self) -> None:
        """Affiche les prix d'un article commun, établissement par établissement.

        C'est la vérification qu'on veut pouvoir faire d'un coup d'œil : le même
        slug, trois lignes, trois montants — ou une ligne manquante là où
        l'article n'est pas vendu.
        """
        self.stdout.write("\nÉcarts volontaires — un prix qui déborderait se verrait :")

        for slug in ("poulet-braise", "poisson-braise"):
            articles = (
                MenuItem.objects.filter(slug=slug)
                .select_related("restaurant")
                .order_by("restaurant__name")
            )
            if not articles:
                continue
            lignes = ", ".join(
                f"{article.restaurant.name} = {article.price}" for article in articles
            )
            self.stdout.write(f"  {slug} : {lignes}")
