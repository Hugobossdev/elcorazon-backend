"""Production — la nomenclature : ce qu'il faut de matière pour faire un plat.

## Le pont qui manquait

L'inventaire sait ce que la cuisine détient (`apps.inventory`), le catalogue
sait ce qu'elle vend (`apps.catalog`), et **rien ne reliait les deux**. Passer
une commande en « préparation » était une écriture de colonne : aucune matière
consommée, aucun coût connu, aucune rupture propagée. La recette est ce pont, et
c'est la seule raison d'être de ce module.

Le graphe de dépendances (`tests/architecture/test_dependency_graph.py`) l'avait
réservé : `inventory` ne connaît ni le catalogue ni les commandes — un ingrédient
ne sait pas dans quels plats il entre —, et c'est `production` qui dépend des
deux. L'inverse aurait fait dépendre la chambre froide de la carte.

## Pourquoi une recette vise un plat **ou** une option

Le supplément fromage consomme du fromage ; « sans oignon » n'en consomme pas.
Une personnalisation n'est donc pas un décor de prix : elle déplace la matière,
et le cahier des charges le demande explicitement (« Groupe : Retirer — sans
oignon, sans tomate »).

Deux tables séparées auraient dupliqué les lignes, la validation et le calcul.
Une cible exclusive — `menu_item` **ou** `option`, jamais les deux, jamais aucun
— reprend le motif que `AreaMembership` emploie déjà pour « un périmètre est un
pays ou une ville », avec la même contrainte `CHECK` et la même raison : un
état impossible doit être **irreprésentable**, pas seulement évité.

## Le signe, et la matière qu'on ne crée pas

Une ligne de recette d'option porte un **delta signé**, à l'image de
`Option.price_delta` qui vaut déjà « −200 F » pour « sans fromage ». Retirer
l'oignon, c'est `−20 g` sur la recette de base.

Ce que le signe ne fait **pas** : rendre de la matière. Décommander un oignon ne
le remet pas en chambre froide, cela évite de le sortir. Le plancher est donc
posé à zéro **par ligne de commande**, dans `ProductionService.requirements` —
et non ici, parce qu'une ligne isolée à `−20 g` est parfaitement légitime : elle
ne devient fautive qu'une fois composée avec sa recette de base.

## Ce que ce module ne fait pas encore

Il ne **verse pas** de version. Modifier une recette ne réécrit pas l'histoire
pour autant : le coût passé vit dans `StockMovement`, qui est un journal en
ajout seul. C'est la ligne consommée qui fait foi, jamais la recette telle
qu'elle est aujourd'hui — donc le versionnement serait du confort d'édition, pas
une garantie d'intégrité, et il attendra qu'on le demande.
"""

from __future__ import annotations

from django.db import models

from apps.catalog.models import MenuItem, Option
from apps.inventory.models import Ingredient
from common.fields import QuantityField
from common.models import TimeStampedModel, UUIDModel

__all__ = ["Recipe", "RecipeIngredient"]


class Recipe(UUIDModel, TimeStampedModel):
    """La nomenclature d'un plat ou d'une option — sa liste de matière.

    En suppression **matérielle**, contrairement au plat qu'elle décrit : une
    recette ne porte aucune écriture comptable. Ce qui a été consommé est au
    journal des mouvements, avec son coût du jour, et n'a pas besoin d'elle pour
    rester lisible.
    """

    # `CASCADE` des deux côtés : une recette sans sa cible ne veut rien dire.
    # Le plat, lui, est en suppression logique — la recette survit donc au
    # retrait du plat de la carte, et ne disparaît qu'à sa suppression réelle.
    menu_item = models.OneToOneField(
        MenuItem,
        on_delete=models.CASCADE,
        related_name="recipe",
        null=True,
        blank=True,
    )
    option = models.OneToOneField(
        Option,
        on_delete=models.CASCADE,
        related_name="recipe",
        null=True,
        blank=True,
    )

    # Le mode opératoire, à destination du poste de cuisine. Il n'entre dans
    # aucun calcul : c'est du texte que l'on montre, et le confondre avec une
    # donnée structurée serait promettre une automatisation qui n'existe pas.
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "recette"
        constraints = [
            # Exactement une cible. Le `XOR` de Django rendrait « au plus une » ;
            # la double négation dit « exactement une », et c'est ce qu'on veut :
            # une recette orpheline serait de la matière que personne ne
            # consomme, et une recette à deux cibles serait comptée deux fois.
            models.CheckConstraint(
                condition=(
                    models.Q(menu_item__isnull=False, option__isnull=True)
                    | models.Q(menu_item__isnull=True, option__isnull=False)
                ),
                name="recipe_targets_exactly_one",
            ),
        ]

    def __str__(self) -> str:
        cible = self.menu_item or self.option
        return f"Recette — {cible}"


class RecipeIngredient(UUIDModel):
    """Une ligne de nomenclature : cet ingrédient, cette quantité.

    `PROTECT` sur l'ingrédient : supprimer une référence encore employée par une
    recette rendrait le plat incalculable en silence — le service dirait « il ne
    faut rien » au lieu de « je ne sais pas ». Le retrait passe donc par la
    désactivation (`Ingredient.is_active`), qui laisse l'histoire lisible.
    """

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(
        Ingredient, on_delete=models.PROTECT, related_name="recipe_lines"
    )

    # Signée : négative sur une recette d'option qui **retire** de la matière.
    # La dimension doit être celle de l'ingrédient, ce qu'aucune contrainte
    # `CHECK` ne peut exprimer — elle vit dans une autre table. C'est
    # `ProductionService.set_ingredient` qui la tient, et un test qui le
    # verrouille.
    quantity = QuantityField()

    class Meta:
        verbose_name = "ligne de recette"
        verbose_name_plural = "lignes de recette"
        ordering = ["ingredient__name"]
        constraints = [
            # Une recette dit une fois ce qu'elle prend d'un ingrédient. Deux
            # lignes pour la même référence se liraient comme un remplacement
            # alors qu'elles s'additionnent — c'est ainsi qu'on double une
            # quantité sans le voir.
            models.UniqueConstraint(
                fields=["recipe", "ingredient"],
                name="one_recipe_line_per_ingredient",
            ),
            # Zéro n'est pas une quantité, c'est une ligne qu'on a oublié de
            # supprimer. La laisser passer ferait figurer un ingrédient dans la
            # liste des allergènes hérités d'un plat qui n'en contient pas.
            models.CheckConstraint(
                condition=~models.Q(quantity_base=0),
                name="recipe_line_quantity_not_zero",
            ),
        ]
        indexes = [
            # « Quels plats emploient cet ingrédient ? » — la question que pose
            # une rupture, et celle que pose un rappel de lot.
            models.Index(fields=["ingredient"]),
        ]

    def __str__(self) -> str:
        return f"{self.ingredient.name} — {self.quantity}"
