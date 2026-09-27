"""Ce qu'il faut sortir du stock pour produire une commande.

## La question à laquelle ce module répond

« Trois burgers dont un sans oignon et un à double fromage : que sort-on de la
chambre froide ? » Personne ne savait y répondre jusqu'ici, et c'est ce qui
rendait le coût matière inconnu, la marge incalculable et la rupture
d'ingrédient invisible.

La réponse est un **total par ingrédient**, prêt à être réservé puis consommé
par `apps.inventory`. Ce module ne touche pas au stock : il dit ce qu'il faut,
l'inventaire dit s'il l'a. Séparer les deux permet de poser la question sans
rien engager — ce dont la disponibilité aura besoin, et le devis aussi.

## Le plancher à zéro, et où il se pose

Une option qui retire de la matière porte une quantité négative. Composée avec
sa recette de base, elle donne un besoin plus petit ; seule, elle donnerait un
besoin **négatif**, c'est-à-dire de la matière créée par une commande.

Le plancher se pose donc **par ligne de commande**, après composition et avant
multiplication par le nombre de portions. Le poser à la fin, sur le total,
laisserait un « sans oignon » effacer l'oignon d'un *autre* plat du même panier
— deux clients, une seule matière, et un stock qui dérive sans qu'aucune ligne
ne soit fausse.

## Les plats sans recette ne consomment rien

Un plat dont la nomenclature n'est pas saisie traverse sans rien décompter.
C'est délibéré : la carte existe déjà, ses recettes non, et exiger qu'elles le
soient toutes le jour du déploiement fermerait la boutique. La bascule se fait
plat par plat, et un plat sans recette se comporte exactement comme avant.

Ce choix a une contrepartie qu'il faut voir : le silence. « Rien à sortir » et
« je ne sais pas quoi sortir » se ressemblent, et seul le second est un
problème. C'est pourquoi `missing_recipes` existe — pour que l'exploitation
mesure ce qui lui échappe encore, au lieu de le découvrir sur un inventaire qui
ne tombe pas juste.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from django.db.models import Q

from apps.accounts.models import User
from apps.catalog.models import MenuItem
from apps.inventory.models import Ingredient, StockItem
from apps.inventory.services import InventoryService
from apps.production.models import Recipe, RecipeIngredient
from common.exceptions import BusinessRuleViolation
from common.quantities import DimensionMismatch, Quantity

__all__ = ["MaterialService", "MaterialShortage", "ProducedLine", "ProductionService"]


class EcritureDeStock(Protocol):
    """La forme commune à `reserve`, `consume` et `release`.

    Les trois moments d'une commande ne diffèrent que par l'écriture qu'ils
    déclenchent — le reste, du calcul des besoins à l'ordre des verrous, est
    identique. Ce protocole permet de n'écrire cette mécanique qu'une fois,
    sans passer une chaîne de caractères qu'il faudrait ensuite traduire par un
    `if`.
    """

    def __call__(
        self,
        *,
        item: StockItem,
        quantity: Quantity,
        actor: User | None,
        reference: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ProducedLine:
    """Ce qu'une ligne de panier ou de commande demande à la cuisine.

    Volontairement détachée de `CartLine` et d'`OrderLine` : la consommation part
    du panier, le retour part de la commande, et `production` ne doit dépendre
    ni de l'un ni de l'autre — le graphe de dépendances l'interdit, et il a
    raison. L'appelant traduit, ce qui tient en une compréhension de liste.
    """

    menu_item_id: uuid.UUID
    quantity: int
    option_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MaterialShortage:
    """La matière ne suffit pas pour une ligne — et dans quelle mesure.

    `portions_possible` est ce que la cuisine peut préparer de cette ligne
    **prise seule**. C'est ce qui rend le refus actionnable : « il n'est possible
    d'en préparer que deux » appelle un geste précis, « rupture » n'en appelle
    aucun.

    `shared` distingue les deux façons de manquer. Faux : la ligne seule dépasse
    le stock. Vrai : chaque ligne tiendrait seule, mais pas le panier entier —
    deux plats se disputent le même pain. Le client ne corrige pas les deux cas
    de la même façon, et le second ne se voit qu'au cumul.
    """

    portions_possible: int
    ingredient_ids: tuple[uuid.UUID, ...]
    shared: bool = False


class ProductionService:
    @staticmethod
    def set_ingredient(
        *,
        recipe: Recipe,
        ingredient_id: uuid.UUID,
        quantity: Quantity,
    ) -> RecipeIngredient:
        """Pose — ou remplace — la quantité d'un ingrédient dans une recette.

        Le seul chemin d'écriture autorisé, parce qu'il porte la règle qu'aucune
        contrainte `CHECK` ne peut exprimer : **la dimension de la ligne est
        celle de l'ingrédient**. Elle vit dans une autre table, hors de portée
        d'une contrainte de table.

        Sans cette garde, « 200 ml » de farine s'écrirait sans broncher, et la
        faute n'apparaîtrait qu'au moment d'additionner deux lignes du même
        ingrédient — ou pire, ne se verrait jamais, un entier de base en valant
        un autre.
        """
        ingredient = Ingredient.objects.get(pk=ingredient_id)
        if quantity.dimension != ingredient.dimension:
            raise DimensionMismatch(ingredient.dimension, quantity.dimension)

        if not ingredient.is_active or ingredient.is_deleted:
            raise BusinessRuleViolation(
                f"« {ingredient.name} » est retiré du référentiel : une recette ne "
                "peut plus l'employer.",
                ingredient_id=str(ingredient_id),
            )

        # Le signe négatif **retire** de la matière, et n'a de sens que composé
        # avec une recette de base — celle d'une option : « sans oignon ». Sur la
        # recette d'un plat, il n'y a rien à retirer : le plancher par ligne le
        # ramènerait à zéro en silence, et la saisie fautive « −20 g » ne se
        # verrait jamais.
        if recipe.menu_item_id is not None and quantity.is_negative:
            raise BusinessRuleViolation(
                "La recette d'un plat dit ce qu'il consomme : une quantité négative "
                "n'y a pas de sens. Pour retirer un ingrédient à la demande, poser "
                "la quantité négative sur la recette de l'option « sans … ».",
                ingredient_id=str(ingredient_id),
            )

        if quantity.is_zero:
            raise BusinessRuleViolation(
                "Une ligne de recette à zéro ne veut rien dire : retirer "
                f"« {ingredient.name} » de la recette, ou lui donner une quantité.",
                ingredient_id=str(ingredient_id),
            )

        ligne, _ = RecipeIngredient.objects.update_or_create(
            recipe=recipe,
            ingredient=ingredient,
            defaults={"quantity": quantity},
        )
        return ligne

    @staticmethod
    def requirements(lines: Iterable[ProducedLine]) -> dict[uuid.UUID, Quantity]:
        """Total de matière nécessaire, par ingrédient.

        Une seule requête, quel que soit le nombre de lignes : la boucle qui suit
        ne touche plus la base. Un panier de quinze articles chargerait autrement
        trente recettes une à une, au moment précis — la confirmation de
        commande — où la transaction tient déjà des verrous.
        """
        lignes = list(lines)
        total: dict[uuid.UUID, Quantity] = {}
        for ligne, portion in zip(lignes, ProductionService.portions(lignes), strict=True):
            for ingredient_id, quantite in portion.items():
                portions = quantite * ligne.quantity
                courant = total.get(ingredient_id)
                total[ingredient_id] = portions if courant is None else courant + portions

        return total

    @staticmethod
    def portions(lines: Iterable[ProducedLine]) -> list[dict[uuid.UUID, Quantity]]:
        """La matière d'**une** portion de chaque ligne, dans l'ordre des lignes.

        C'est le cœur de la nomenclature, et le seul endroit où le plancher se
        pose : `requirements` en fait un total, la disponibilité en fait un
        nombre de portions possibles. Les deux lisent donc la même composition,
        et ne peuvent pas diverger sur ce qu'un « sans oignon » retire.

        Une seule requête, quel que soit le nombre de lignes.
        """
        lignes = list(lines)
        if not lignes:
            return []

        plats = {ligne.menu_item_id for ligne in lignes}
        options = {option_id for ligne in lignes for option_id in ligne.option_ids}

        par_plat: dict[uuid.UUID, dict[uuid.UUID, Quantity]] = defaultdict(dict)
        par_option: dict[uuid.UUID, dict[uuid.UUID, Quantity]] = defaultdict(dict)

        nomenclature = RecipeIngredient.objects.filter(
            Q(recipe__menu_item_id__in=plats) | Q(recipe__option_id__in=options)
        ).select_related("recipe")

        for ligne_recette in nomenclature:
            # `recipe_targets_exactly_one` garantit qu'une seule des deux cibles
            # est renseignée ; le vérificateur de types, lui, voit deux colonnes
            # nullables. Les deux branches le disent explicitement plutôt que de
            # déduire l'une de l'autre : si la contrainte venait à tomber, une
            # recette sans cible serait **ignorée** au lieu d'être rangée sous
            # une clé nulle, où elle contaminerait un plat au hasard.
            plat_id = ligne_recette.recipe.menu_item_id
            option_id = ligne_recette.recipe.option_id
            if plat_id is not None:
                par_plat[plat_id][ligne_recette.ingredient_id] = ligne_recette.quantity
            elif option_id is not None:
                par_option[option_id][ligne_recette.ingredient_id] = ligne_recette.quantity

        resultat: list[dict[uuid.UUID, Quantity]] = []
        for ligne in lignes:
            besoin = ProductionService._besoin_d_une_portion(ligne, par_plat, par_option)
            # Le plancher : une option ne crée pas de matière. Décommander un
            # oignon ne le remet pas en chambre froide, cela évite de l'en
            # sortir.
            resultat.append(
                {
                    ingredient_id: quantite
                    for ingredient_id, quantite in besoin.items()
                    if quantite.is_positive
                }
            )

        return resultat

    @staticmethod
    def _besoin_d_une_portion(
        ligne: ProducedLine,
        par_plat: dict[uuid.UUID, dict[uuid.UUID, Quantity]],
        par_option: dict[uuid.UUID, dict[uuid.UUID, Quantity]],
    ) -> dict[uuid.UUID, Quantity]:
        """La matière d'**une** portion, recette de base et options composées."""
        besoin: dict[uuid.UUID, Quantity] = dict(par_plat.get(ligne.menu_item_id, {}))

        for option_id in ligne.option_ids:
            for ingredient_id, delta in par_option.get(option_id, {}).items():
                courant = besoin.get(ingredient_id)
                besoin[ingredient_id] = delta if courant is None else courant + delta

        return besoin

    @staticmethod
    def remove_ingredient(*, recipe: Recipe, ingredient_id: uuid.UUID) -> bool:
        """Retire un ingrédient d'une recette. Rend `False` s'il n'y figurait pas.

        Retirer ce qui n'est pas là n'est pas une erreur : c'est l'état voulu, et
        un double clic ne doit pas afficher un échec.
        """
        supprimees, _ = RecipeIngredient.objects.filter(
            recipe=recipe, ingredient_id=ingredient_id
        ).delete()
        return supprimees > 0

    @staticmethod
    def coverage(restaurant_id: uuid.UUID) -> tuple[int, list[MenuItem]]:
        """Combien d'articles vivants la carte compte, et lesquels n'ont pas de recette.

        Rend l'écart **mesurable** : un plat sans recette ne consomme rien, ce
        qui est voulu pendant la bascule et indistinguable, depuis un stock, d'un
        plat qui ne consomme réellement rien. Tant que cette liste n'est pas
        vide, le coût matière de la cuisine est incomplet — et c'est ce qu'il
        faut lire avant de croire une marge.
        """
        vivants = list(
            MenuItem.objects.alive()
            .filter(restaurant_id=restaurant_id)
            .select_related("category")
            .order_by("category__sort_order", "sort_order", "name")
        )
        sans_recette = ProductionService.missing_recipes(item.pk for item in vivants)
        return len(vivants), [item for item in vivants if item.pk in sans_recette]

    @staticmethod
    def missing_recipes(menu_item_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """Les plats dont la nomenclature n'est pas saisie.

        Un plat sans recette ne consomme rien, ce qui est le comportement voulu
        pendant la bascule — mais indistinguable, depuis un total, d'un plat qui
        ne consomme réellement rien. Cette question rend l'écart mesurable, et
        c'est elle qu'un tableau de bord doit poser avant de croire une marge.
        """
        demandes = set(menu_item_ids)
        if not demandes:
            return set()

        connus = set(
            Recipe.objects.filter(menu_item_id__in=demandes).values_list("menu_item_id", flat=True)
        )
        return demandes - connus


class MaterialService:
    """Engage la matière d'une commande : la promettre, la sortir, la rendre.

    ## Pourquoi ce service existe, et pourquoi il est ici

    `ProductionService.requirements` dit **ce qu'il faut** ; `InventoryService`
    sait **écrire** dans un stock. Il manquait le geste qui relie les deux pour
    une commande donnée, dans une cuisine donnée.

    Il vit dans `production` et non dans `orders` pour une raison de graphe :
    `production` connaît déjà le catalogue et l'inventaire, alors qu'`orders`
    n'en connaissait qu'un. Le poser ici ajoute **une** arête au graphe
    (`orders → production`) au lieu de deux, et laisse `orders` ignorer
    jusqu'au nom de `StockItem`.

    ## Les trois moments

        création de commande   reserve    la matière est promise
        passage en préparation consume    elle sort réellement
        annulation             release    elle redevient disponible

    Réserver plutôt que consommer d'emblée n'est pas une subtilité : entre la
    commande et le feu, la matière est **promise sans être partie**. Un stock
    qui l'ignore affiche « il reste 3 kg » alors que 2,8 sont déjà dus, et c'est
    la deuxième commande qui découvre le mensonge.

    ## L'ingrédient que la cuisine ne suit pas

    Un ingrédient sans ligne de stock dans cette cuisine est **ignoré**, jamais
    un motif de refus. C'est la même règle que `MenuItem.tracks_stock` applique
    déjà aux plats finis — « le suivi est facultatif » — et que le plat sans
    recette applique à la nomenclature : la bascule se fait par étapes, et
    aucune d'elles ne doit pouvoir fermer la boutique.

    Ce qui est refusé, en revanche, c'est un ingrédient **suivi** dont le stock
    ne suffit pas : `InsufficientStock` remonte, et la commande n'est pas créée.
    C'est la rupture, et elle doit se voir.

    ## L'ordre des écritures, et l'interblocage qu'il évite

    Les ingrédients sont traités **triés par identifiant**, toujours. Deux
    commandes simultanées portant sur les mêmes matières prendraient sinon leurs
    verrous de ligne dans des ordres opposés — l'une l'oignon puis le fromage,
    l'autre l'inverse — et PostgreSQL en tuerait une pour interblocage, au
    moment précis du coup de feu où les deux arrivent ensemble.

    L'ordre total est la parade classique, et elle ne coûte qu'un `sorted()`.
    """

    @staticmethod
    def reserve(
        *,
        restaurant_id: uuid.UUID,
        lines: Iterable[ProducedLine],
        reference: str = "",
        actor: User | None = None,
    ) -> dict[uuid.UUID, Quantity]:
        """Promet la matière d'une commande. Lève `InsufficientStock` si elle manque."""
        return MaterialService._engager(
            restaurant_id=restaurant_id,
            lines=lines,
            ecrire=InventoryService.reserve,
            reference=reference,
            actor=actor,
        )

    @staticmethod
    def consume(
        *,
        restaurant_id: uuid.UUID,
        lines: Iterable[ProducedLine],
        reference: str = "",
        actor: User | None = None,
    ) -> dict[uuid.UUID, Quantity]:
        """Sort la matière promise — la cuisine a commencé.

        L'engagement est levé dans le même geste : sans cela, la matière serait
        comptée deux fois, une fois promise et une fois partie, et le disponible
        s'effondrerait sans que rien n'ait bougé.

        ## Pourquoi on libère ce qui est **promis**, et non ce qu'on sort

        Les deux coïncident dans le cas courant, et divergent chaque fois que la
        commande n'a rien réservé. Ce n'est pas une hypothèse d'école — trois
        chemins y mènent, dont deux sont certains le jour du déploiement :

        * une commande créée **avant** ce lot, préparée après ;
        * une recette saisie entre la commande et le feu ;
        * une ligne de stock ouverte entre les deux.

        Libérer aveuglément la quantité consommée ferait alors échouer une
        libération sans contrepartie — et la cuisine ne pourrait plus avancer
        une commande déjà payée, pour une écriture comptable qui la regarde à
        peine. Le refus appartient à la **commande**, jamais au feu.

        ## Ce qui n'est pas traité ici, et se verra

        Si la matière manque réellement — `on_hand` insuffisant —,
        `InsufficientStock` remonte et la transition échoue. C'est un cas que la
        réservation a normalement rendu impossible, et qui ne survient donc
        qu'après un ajustement manuel à la baisse ou sur une donnée déjà
        incohérente. Le laisser remonter est un signal juste : le stock du
        système ne décrit plus la chambre froide, et l'y forcer en silence le
        rendrait définitivement faux.

        Le modèle prévoit déjà la sortie de secours si le besoin se confirme :
        un **type de mouvement** autorisant explicitement la consommation
        saisie après coup — jamais la contrainte `on_hand >= 0` retirée.
        """

        def ecrire(
            *, item: StockItem, quantity: Quantity, actor: User | None, reference: str
        ) -> object:
            promis = item.reserved
            a_liberer = quantity if quantity <= promis else promis
            if a_liberer.is_positive:
                InventoryService.release(
                    item=item, quantity=a_liberer, actor=actor, reference=reference
                )
            return InventoryService.consume(
                item=item,
                quantity=quantity,
                actor=actor,
                reference=reference,
            )

        return MaterialService._engager(
            restaurant_id=restaurant_id,
            lines=lines,
            ecrire=ecrire,
            reference=reference,
            actor=actor,
        )

    @staticmethod
    def release(
        *,
        restaurant_id: uuid.UUID,
        lines: Iterable[ProducedLine],
        reference: str = "",
        actor: User | None = None,
    ) -> dict[uuid.UUID, Quantity]:
        """Rend une promesse — la commande est annulée avant d'être cuisinée."""
        return MaterialService._engager(
            restaurant_id=restaurant_id,
            lines=lines,
            ecrire=InventoryService.release,
            reference=reference,
            actor=actor,
        )

    @staticmethod
    def shortages(
        *,
        restaurant_id: uuid.UUID,
        lines: Sequence[ProducedLine],
        independent: bool = False,
    ) -> list[MaterialShortage | None]:
        """Ce qui manque pour chaque ligne, sans rien engager — dans l'ordre des lignes.

        ## La question que `reserve` ne pose qu'en échouant

        `reserve` refuse une commande dont la matière manque, et c'est le seul
        refus qui fasse foi : il est pris sous verrou. Mais il arrive au moment
        de payer. Cette lecture pose la même question **avant** — sur la carte,
        dans le panier — pour que le client apprenne la rupture en choisissant,
        pas en validant.

        Elle ne verrouille rien, et ne le doit pas : une page de carte ne peut
        pas tenir des verrous de stock. Entre cette lecture et la commande, une
        autre commande peut emporter le dernier pain ; c'est alors `reserve` qui
        le dira. La lecture informe, la réservation tranche.

        ## Mêmes règles que l'engagement

        Un ingrédient que la cuisine ne suit pas n'est jamais en rupture, et un
        plat sans recette ne manque de rien — exactement comme `reserve` les
        traverse. Une lecture plus stricte que l'écriture annoncerait des
        ruptures que la commande n'aurait pas refusées.

        ## `independent`

        Faux pour un panier : les lignes se cumulent, et deux plats peuvent se
        disputer le même ingrédient. Vrai pour une carte : chaque article est
        jugé seul, puisque personne ne commande toute la carte à la fois.

        Deux requêtes, quel que soit le nombre de lignes.
        """
        portions = ProductionService.portions(lines)
        ingredients = {ingredient_id for portion in portions for ingredient_id in portion}
        if not ingredients:
            return [None] * len(lines)

        disponible: dict[uuid.UUID, Quantity] = {
            item.ingredient_id: item.available
            for item in StockItem.objects.filter(
                restaurant_id=restaurant_id, ingredient_id__in=ingredients
            )
        }

        cumul: dict[uuid.UUID, Quantity] = {}
        if not independent:
            for ligne, portion in zip(lines, portions, strict=True):
                for ingredient_id, quantite in portion.items():
                    besoin = quantite * ligne.quantity
                    courant = cumul.get(ingredient_id)
                    cumul[ingredient_id] = besoin if courant is None else courant + besoin

        verdicts: list[MaterialShortage | None] = []
        for ligne, portion in zip(lines, portions, strict=True):
            # Seuls les ingrédients suivis comptent — voir la docstring.
            suivis = {i: q for i, q in portion.items() if i in disponible}
            if not suivis:
                verdicts.append(None)
                continue

            possibles = min(
                disponible[i].amount_base // quantite.amount_base for i, quantite in suivis.items()
            )
            seule = tuple(
                sorted(
                    (
                        i
                        for i, quantite in suivis.items()
                        if quantite * ligne.quantity > disponible[i]
                    ),
                    key=str,
                )
            )
            if seule:
                verdicts.append(MaterialShortage(portions_possible=possibles, ingredient_ids=seule))
                continue

            partages = tuple(
                sorted((i for i in suivis if cumul.get(i, disponible[i]) > disponible[i]), key=str)
            )
            verdicts.append(
                MaterialShortage(portions_possible=possibles, ingredient_ids=partages, shared=True)
                if partages
                else None
            )

        return verdicts

    @staticmethod
    def _engager(
        *,
        restaurant_id: uuid.UUID,
        lines: Iterable[ProducedLine],
        ecrire: EcritureDeStock,
        reference: str,
        actor: User | None,
    ) -> dict[uuid.UUID, Quantity]:
        besoins = ProductionService.requirements(lines)
        if not besoins:
            return {}

        # Une requête pour toutes les lignes de stock concernées. Les charger
        # dans la boucle ferait une requête par ingrédient, à l'intérieur de la
        # transaction de commande.
        stocks = {
            item.ingredient_id: item
            for item in StockItem.objects.filter(
                restaurant_id=restaurant_id, ingredient_id__in=besoins
            ).select_related("ingredient")
        }

        engages: dict[uuid.UUID, Quantity] = {}
        # Trié : voir l'interblocage, dans la docstring de la classe.
        for ingredient_id in sorted(besoins, key=str):
            item = stocks.get(ingredient_id)
            if item is None:
                # Cette cuisine ne suit pas cet ingrédient. On ne peut pas
                # décompter ce qu'on ne compte pas.
                continue
            ecrire(
                item=item,
                quantity=besoins[ingredient_id],
                actor=actor,
                reference=reference,
            )
            engages[ingredient_id] = besoins[ingredient_id]

        return engages
