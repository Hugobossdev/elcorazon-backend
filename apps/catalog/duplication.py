"""Recopie d'une carte vers un établissement neuf — ADR-002.

L'arête va d'ici vers `restaurants`, comme pour `readiness` : c'est le sens
déclaré dans le graphe, et le seul qui n'introduise pas de cycle. `restaurants`
expose un point d'abonnement et reçoit un décompte ; il n'apprend rien du
catalogue.

## Ce qui est copié, et dans quel ordre

Catégories, puis articles, puis groupes d'options, puis options. L'ordre est
imposé par les clés étrangères, et chaque étage a besoin de la correspondance
établie par l'étage du dessus : un article de la cible doit pointer vers la
catégorie **de la cible**, pas vers celle de la source. C'est l'erreur que ce
module existe pour rendre impossible — un article rattaché à la catégorie de
l'autre restaurant disparaîtrait de sa propre carte tout en apparaissant dans
celle du voisin.

## Ce qui n'est pas copié

* **Les avis et les achats vérifiés.** Ils appartiennent à des clients, portent
  une date d'achat réelle, et un établissement neuf qui ouvrirait avec 4,6
  étoiles et 200 avis mentirait à son premier client.
* **Les stocks.** `stock_quantity` décrit ce qu'il y a dans une chambre froide.
  Recopié, il annoncerait douze parts d'un gâteau que personne n'a fait.
* **Les images.** Le fichier est partagé par référence : les deux articles
  pointent vers le même objet de stockage. Le dupliquer coûterait un octet pour
  un octet sans rien apporter, et le supprimer d'un côté casserait l'autre —
  ce que le champ, lui, ne fait pas, puisque rien n'efface l'objet distant.

## La disponibilité repart à zéro… ou presque

`is_available` est recopié tel quel : c'est une décision de carte (« ce plat
n'est plus proposé »), pas un état du jour. En revanche `is_popular` et
`rating_*` ne le sont pas — ils décrivent ce que les clients **de la source**
ont fait.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.catalog.models import Category, MenuItem, Option, OptionGroup, OptionTemplate, Variant

if TYPE_CHECKING:  # pragma: no cover
    from apps.restaurants.models import Restaurant

__all__ = ["SECTION_CATALOG", "copier_le_catalogue"]

SECTION_CATALOG = "catalog"


def copier_le_catalogue(source: Restaurant, cible: Restaurant) -> int:
    """Duplique la carte de `source` sur `cible`, et rend le nombre d'articles.

    Le décompte porte sur les **articles** et non sur le total des objets
    créés : c'est ce que l'exploitation compte pour savoir si la carte est bien
    arrivée. « 137 objets copiés » ne se vérifie contre rien ; « 42 articles »
    se compare à l'écran d'en face.

    Les articles supprimés ne sont pas repris : la suppression du catalogue est
    douce, et un établissement neuf n'a aucune raison de naître avec la
    corbeille d'un autre.
    """
    categories = _copier_les_categories(source, cible)
    articles = _copier_les_articles(source, cible, categories)
    _copier_les_options(articles)
    _copier_les_modeles_d_options(source, cible)
    return len(articles)


def _copier_les_categories(source: Restaurant, cible: Restaurant) -> dict[str, Category]:
    """Rend la correspondance « identifiant source → catégorie cible ».

    C'est cette correspondance, et non un `get` par slug, qui rattache ensuite
    les articles : deux catégories peuvent partager un nom d'affichage, et le
    slug n'est unique que par restaurant — le chercher dans la cible marcherait
    presque toujours, ce qui est la pire des propriétés pour un rattachement.
    """
    correspondance: dict[str, Category] = {}
    for categorie in Category.objects.filter(restaurant=source).order_by("sort_order"):
        correspondance[str(categorie.pk)] = Category.objects.create(
            restaurant=cible,
            name=categorie.name,
            slug=categorie.slug,
            emoji=categorie.emoji,
            description=categorie.description,
            sort_order=categorie.sort_order,
            is_active=categorie.is_active,
        )
    return correspondance


def _copier_les_articles(
    source: Restaurant, cible: Restaurant, categories: dict[str, Category]
) -> dict[str, MenuItem]:
    correspondance: dict[str, MenuItem] = {}
    articles = MenuItem.objects.alive().filter(restaurant=source).order_by("sort_order")

    for article in articles:
        categorie = categories.get(str(article.category_id))
        if categorie is None:
            # Un article dont la catégorie n'a pas été copiée n'a nulle part où
            # aller. Le cas ne devrait pas se produire — la contrainte de clé
            # étrangère l'empêche côté source — mais le sauter vaut mieux que
            # de créer un article orphelin qu'aucun écran n'afficherait.
            continue

        # `type: ignore[misc]` — convention du dépôt pour un champ posé par
        # `contribute_to_class` : django-stubs ne voit pas `price`, qui est une
        # `property` sur la classe construite. Même motif que `OrderService.place`.
        correspondance[str(article.pk)] = MenuItem.objects.create(  # type: ignore[misc]
            restaurant=cible,
            category=categorie,
            name=article.name,
            slug=article.slug,
            description=article.description,
            image=article.image,
            price=article.price,
            preparation_minutes=article.preparation_minutes,
            calories=article.calories,
            ingredients=list(article.ingredients),
            allergens=list(article.allergens),
            dietary_tags=list(article.dietary_tags),
            is_available=article.is_available,
            vip_exclusive=article.vip_exclusive,
            tracks_stock=article.tracks_stock,
            sort_order=article.sort_order,
        )
    return correspondance


def _copier_les_options(articles: dict[str, MenuItem]) -> None:
    """Recopie les groupes d'options et leurs options, article par article.

    Les listes sont construites puis écrites en deux `bulk_create` plutôt qu'en
    une insertion par option : une carte de quarante articles à trois groupes de
    quatre options fait cinq cents insertions unitaires, soit une duplication
    qui prend visiblement du temps dans le back-office pour aucune raison.
    """
    groupes_source = OptionGroup.objects.filter(menu_item_id__in=articles).order_by("sort_order")

    groupes_crees: dict[str, OptionGroup] = {}
    for groupe in groupes_source:
        article = articles[str(groupe.menu_item_id)]
        groupes_crees[str(groupe.pk)] = OptionGroup(
            menu_item=article,
            name=groupe.name,
            min_select=groupe.min_select,
            max_select=groupe.max_select,
            sort_order=groupe.sort_order,
        )
    OptionGroup.objects.bulk_create(groupes_crees.values())

    options = [
        Option(  # type: ignore[misc]
            group=groupes_crees[str(option.group_id)],
            name=option.name,
            price_delta=option.price_delta,
            is_default=option.is_default,
            is_available=option.is_available,
            sort_order=option.sort_order,
        )
        for option in Option.objects.filter(group_id__in=groupes_crees).order_by("sort_order")
    ]
    Option.objects.bulk_create(options)

    # Les tailles suivent l'article, avec leur prix absolu (lot 2).
    Variant.objects.bulk_create(
        Variant(  # type: ignore[misc]
            menu_item=articles[str(variante.menu_item_id)],
            name=variante.name,
            sku=variante.sku,
            price=variante.price,
            is_available=variante.is_available,
            is_active=variante.is_active,
            sort_order=variante.sort_order,
        )
        for variante in Variant.objects.filter(menu_item_id__in=articles).order_by("sort_order")
    )


def _copier_les_modeles_d_options(source: Restaurant, cible: Restaurant) -> None:
    """Recopie les modèles d'options — les suppléments réutilisables de la carte.

    Ils sont rattachés au restaurant et non à un article : sans eux, la cible
    hériterait des options déjà posées sur ses plats mais plus du vocabulaire
    qui sert à en ajouter, et le premier nouveau plat se saisirait à la main.
    """
    OptionTemplate.objects.bulk_create(
        [
            OptionTemplate(  # type: ignore[misc]
                restaurant=cible,
                name=modele.name,
                group_name=modele.group_name,
                price_delta=modele.price_delta,
                is_default=modele.is_default,
                is_active=modele.is_active,
                sort_order=modele.sort_order,
            )
            for modele in OptionTemplate.objects.filter(restaurant=source)
        ]
    )
