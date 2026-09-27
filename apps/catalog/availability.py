"""L'article est-il commandable, en tant qu'article ?

Second des trois niveaux que compose `apps.availability` — voir
`common.availability` pour le vocabulaire commun.

## Ce que ce module sait, et ce qu'il ne peut pas savoir

Il sait lire l'article : retiré, désactivé, rangé dans une catégorie éteinte,
compté à l'unité et épuisé, porteur d'une option qu'on ne sert plus.

Il ne peut pas savoir s'il reste de quoi le **préparer**. Cette question passe
par la recette et par le stock de matière, et `catalog` n'a le droit de
connaître ni l'une ni l'autre : c'est `production` qui dépend du catalogue,
jamais l'inverse (ADR-002). Sans ce second regard, un burger dont le pain
manque resterait proposé à la carte, et le client l'apprendrait au paiement.

## Le juge, inscrit plutôt qu'importé

D'où le registre, sur le modèle exact de `apps.restaurants.readiness` : le juge
complet — `apps.availability` — **s'inscrit** ici au `ready()` de son
application, et la carte publique lui pose la question sans connaître son nom.
L'arête va de l'abonné vers l'émetteur (`availability → catalog`), elle est
déclarée, et donc vérifiée en CI.

Un juge absent — application retirée, `ready()` non appelé — laisse la carte
répondre avec ce qu'elle sait seule. C'est le bon sens de défaillance : la
carte serait moins précise, jamais permissive sur ce qui compte, puisque la
commande, elle, passe par le juge complet et refuse sous verrou.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence

from apps.catalog.models import MenuItem, Option, Variant
from common.availability import Unavailability, UnavailabilityCode

__all__ = [
    "MenuJudge",
    "customization_unavailability",
    "item_unavailability",
    "menu_unavailabilities",
    "register_menu_judge",
]

#: Rend, pour chaque article **non commandable**, pourquoi. Les commandables
#: sont absents du dictionnaire.
MenuJudge = Callable[[Sequence[MenuItem]], dict[uuid.UUID, Unavailability]]

_JUGE: list[MenuJudge] = []


def item_unavailability(
    item: MenuItem,
    *,
    options: Iterable[Option] = (),
    quantity: int = 1,
) -> Unavailability | None:
    """Ce qui empêche de commander cet article, vu du seul catalogue.

    L'ordre est celui du motif le plus actionnable : « plus au menu » avant
    « momentanément indisponible », et le stock avant les options — dire
    « option indisponible » pour un plat épuisé ferait changer d'option en vain.

    `item.category` est lue : l'appelant qui juge une liste doit l'avoir
    préchargée, sous peine d'une requête par article.
    """
    if item.is_deleted:
        return Unavailability(
            code=UnavailabilityCode.ITEM_WITHDRAWN,
            message="Cet article n'est plus au menu.",
        )

    # Une catégorie éteinte retire ses articles de la carte publique, mais pas
    # des paniers où ils attendaient déjà. Sans cette ligne, un plat du
    # « petit-déjeuner » désactivé à midi restait commandable depuis un panier
    # composé le matin.
    if not item.is_available or not item.category.is_active:
        return Unavailability(
            code=UnavailabilityCode.ITEM_UNAVAILABLE,
            message="Cet article est momentanément indisponible.",
        )

    if item.tracks_stock and item.stock_quantity < quantity:
        return Unavailability(
            code=UnavailabilityCode.OUT_OF_STOCK,
            message=(
                "Cet article est épuisé."
                if item.stock_quantity == 0
                else f"Il n'en reste que {item.stock_quantity} en stock."
            ),
            details={"remaining": item.stock_quantity},
        )

    indisponibles = sorted(option.name for option in options if not option.is_available)
    if indisponibles:
        return Unavailability(
            code=UnavailabilityCode.OPTION_UNAVAILABLE,
            message=f"Options indisponibles : {', '.join(indisponibles)}.",
        )

    return None


def customization_unavailability(
    item: MenuItem, options: Sequence[Option], variant: Variant | None = None
) -> Unavailability | None:
    """Les options retenues respectent-elles encore les règles de l'article ?

    ## Pourquoi cette question est posée deux fois

    À l'ajout au panier (`apps.carts.services.validate_selection`), et **à la
    commande**. La seconde manquait : les bornes sont en donnée
    (`min_select`, `max_select`), l'exploitation les change sans développement,
    et un panier composé avant le changement passait la commande avec une
    personnalisation que la cuisine ne sait plus préparer — « 2 accompagnements
    parmi 5 » devenu « exactement 1 ».

    La règle vit ici, une fois, et les deux moments la lisent.

    `item.option_groups` est lue : l'appelant qui juge plusieurs lignes doit
    l'avoir préchargée.
    """
    # La taille d'abord (lot 2) : elle fixe le prix, et une taille absente ou
    # étrangère rend le reste de la ligne sans objet.
    if variant is not None and variant.menu_item_id != item.pk:
        return Unavailability(
            code=UnavailabilityCode.INVALID_CUSTOMIZATION,
            message=f"La taille « {variant.name} » n'appartient pas à « {item.name} ».",
            details={"variant_id": str(variant.pk)},
        )
    actives = [v for v in item.variants.all() if v.is_active]
    if variant is None and actives:
        return Unavailability(
            code=UnavailabilityCode.INVALID_CUSTOMIZATION,
            message=f"Choisissez une taille pour « {item.name} ».",
            details={"variants": [str(v.pk) for v in actives]},
        )
    if variant is not None and not actives:
        return Unavailability(
            code=UnavailabilityCode.INVALID_CUSTOMIZATION,
            message=f"« {item.name} » ne se décline pas en tailles.",
            details={"variant_id": str(variant.pk)},
        )
    if variant is not None and not (variant.is_active and variant.is_available):
        return Unavailability(
            code=UnavailabilityCode.VARIANT_UNAVAILABLE,
            message=f"La taille « {variant.name} » n'est plus disponible.",
            details={"variant_id": str(variant.pk)},
        )

    groupes = {groupe.pk: groupe for groupe in item.option_groups.all()}

    for option in options:
        if option.group_id not in groupes:
            return Unavailability(
                code=UnavailabilityCode.INVALID_CUSTOMIZATION,
                message=f"L'option « {option.name} » n'appartient pas à cet article.",
                details={"option_id": str(option.pk)},
            )

    for groupe in groupes.values():
        retenues = sum(1 for option in options if option.group_id == groupe.pk)
        if retenues < groupe.min_select:
            return Unavailability(
                code=UnavailabilityCode.INVALID_CUSTOMIZATION,
                message=f"« {groupe.name} » exige au moins {groupe.min_select} choix.",
                details={"option_group_id": str(groupe.pk)},
            )
        if retenues > groupe.max_select:
            return Unavailability(
                code=UnavailabilityCode.INVALID_CUSTOMIZATION,
                message=f"« {groupe.name} » accepte au plus {groupe.max_select} choix.",
                details={"option_group_id": str(groupe.pk)},
            )

    return None


def register_menu_judge(judge: MenuJudge) -> MenuJudge:
    """Inscrit le juge complet, depuis le `ready()` de `apps.availability`.

    Un seul juge : en inscrire un second **remplace** le premier. Deux juges
    qui répondraient chacun pour une partie de la carte seraient exactement la
    dispersion que ce registre existe pour empêcher.
    """
    _JUGE[:] = [judge]
    return judge


def menu_unavailabilities(items: Sequence[MenuItem]) -> dict[uuid.UUID, Unavailability]:
    """Pourquoi chacun de ces articles n'est pas commandable, en un appel.

    Pensée pour une page de carte : le juge inscrit reçoit la page entière, et
    peut donc charger recettes et stocks en deux requêtes au lieu de deux par
    article.
    """
    if _JUGE:
        return _JUGE[0](items)

    verdicts: dict[uuid.UUID, Unavailability] = {}
    for item in items:
        verdict = item_unavailability(item)
        if verdict is not None:
            verdicts[item.pk] = verdict
    return verdicts
