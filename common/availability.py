"""Le vocabulaire de « ce n'est pas commandable » — un seul, pour tout le produit.

## Pourquoi ce module existe

« Ce client peut-il commander cet article maintenant ? » se décompose en trois
questions qui ne vivent pas au même endroit :

* **la cuisine** — publiée, ouverte, prenant les commandes (`restaurants`) ;
* **l'article** — au menu, disponible, en stock, ses options servies (`catalog`) ;
* **la matière** — de quoi le préparer (`production`, `inventory`).

Chacune reste où vivent ses données, et `apps.availability` les compose. Ce qui
doit en revanche être **commun**, c'est la forme de la réponse : un motif stable
et une phrase. Sans ce vocabulaire partagé, chaque niveau inventerait le sien,
et l'application cliente devrait apprendre trois façons de dire non.

Il vit dans `common` parce que `restaurants` — le plus bas des trois dans le
graphe — doit pouvoir le parler sans rien importer au-dessus de lui.

## Un verdict, jamais une collection de booléens

L'ancienne forme exposait `is_open`, `accepts_orders`, `can_order_now` et
laissait l'appelant recomposer. C'est le mécanisme exact qui avait fait
diverger la zone de livraison entre le devis et la commande (voir
`apps.geography.resolution`) : deux appelants, deux compositions, deux
réponses individuellement cohérentes.

Un `Unavailability` est **la** réponse, avec son motif. L'absence de motif veut
dire « commandable ».
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common.exceptions import BusinessRuleViolation

__all__ = [
    "AddressNotServed",
    "KitchenNotOrderable",
    "Unavailability",
    "UnavailabilityCode",
]


class UnavailabilityCode(StrEnum):
    """Motifs stables — le client s'appuie sur eux, jamais sur la phrase.

    Rangés du plus général au plus précis : c'est l'ordre dans lequel les juges
    les examinent, pour que le motif rendu soit le plus actionnable. Dire
    « ajoutez moins de burgers » à quelqu'un dont la cuisine est fermée ne lui
    apprendrait rien.
    """

    # --- la géographie -------------------------------------------------------
    #: Aucune cuisine en service ne dessert ce point. Ce n'est **pas** une
    #: panne : le serveur a répondu, et sa réponse est « personne ici ». Une
    #: erreur réseau ne doit jamais se lire ainsi — voir `KitchenContextService`
    #: côté client.
    NO_KITCHEN_AVAILABLE = "no_kitchen_available"
    #: Une cuisine existe, mais cette adresse sort de ses zones ou de son rayon.
    ADDRESS_NOT_SERVED = "address_not_served"

    # --- la cuisine ---------------------------------------------------------
    #: Jamais mise en service, ou marché (pays, ville, zone) désactivé.
    KITCHEN_UNPUBLISHED = "kitchen_unpublished"
    #: Retirée du service par l'exploitation (`status = inactive`). Distincte de
    #: la non-publication : le client la connaissait, elle a disparu, et
    #: « elle n'existe pas » serait faux.
    KITCHEN_SUSPENDED = "kitchen_suspended"
    #: Fermée par une fermeture exceptionnelle datée — un jour férié, des
    #: travaux. Passe avant `KITCHEN_CLOSED` : elle a une fin annoncée.
    KITCHEN_TEMPORARILY_CLOSED = "kitchen_temporarily_closed"
    #: Hors des plages d'ouverture — revenir plus tard.
    KITCHEN_CLOSED = "kitchen_closed"
    #: Ouverte, mais la prise de commande est suspendue — un coup de feu, une
    #: panne. Distinct de la fermeture : l'attente se compte en minutes.
    KITCHEN_PAUSED = "kitchen_paused"

    # --- l'article ----------------------------------------------------------
    #: Retiré du menu.
    ITEM_WITHDRAWN = "item_withdrawn"
    #: Désactivé par la cuisine, ou sa catégorie l'est.
    ITEM_UNAVAILABLE = "item_unavailable"
    #: Une option retenue n'est plus servie.
    OPTION_UNAVAILABLE = "option_unavailable"
    #: La taille retenue n'est plus servie — retirée de la carte ou épuisée.
    VARIANT_UNAVAILABLE = "variant_unavailable"
    #: Les options retenues ne respectent plus les règles de l'article — une
    #: option d'un autre plat, ou un groupe dont les bornes ont changé depuis
    #: l'ajout au panier.
    INVALID_CUSTOMIZATION = "invalid_customization"
    #: Plat fini compté à l'unité, et il n'en reste pas assez.
    OUT_OF_STOCK = "out_of_stock"

    # --- la matière ---------------------------------------------------------
    #: Un ingrédient suivi manque pour le préparer.
    INGREDIENT_SHORTAGE = "ingredient_shortage"


@dataclass(frozen=True, slots=True)
class Unavailability:
    """Pourquoi ce n'est pas commandable.

    `details` porte ce qui rend la phrase actionnable sans avoir à la relire :
    le nombre de portions encore possibles, le stock restant. Rien qui ne
    doive être montré au client n'y figure — le nom d'un ingrédient manquant,
    par exemple, intéresse la cuisine, pas celui qui commande.
    """

    code: UnavailabilityCode
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


class KitchenNotOrderable(BusinessRuleViolation):
    """Commande refusée parce que la cuisine ne peut pas la prendre.

    Distincte du refus d'articles : le geste attendu du client n'est pas le
    même. Un panier dont un plat est épuisé se corrige ; une cuisine fermée ne
    se corrige pas, elle s'attend — ou se change.
    """

    code = "kitchen_not_orderable"
    title = "Cuisine indisponible"

    def __init__(self, unavailability: Unavailability) -> None:
        self.unavailability = unavailability
        super().__init__(
            unavailability.message,
            unavailable_code=str(unavailability.code),
            **unavailability.details,
        )


class AddressNotServed(BusinessRuleViolation):
    """Commande refusée parce que l'adresse sort de la desserte de la cuisine.

    Le refus existait, sous le code générique `business_rule_violation` —
    indiscernable d'un minimum de commande non atteint. Or les gestes attendus
    diffèrent : on ne corrige pas une adresse en ajoutant un article.

    Les données contextuelles (`distance_km`, `max_distance_km`) voyagent
    toujours avec lui, et `unavailable_code` porte le motif stable, comme pour
    la cuisine.
    """

    code = "address_not_served"
    title = "Adresse non desservie"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(
            detail, unavailable_code=str(UnavailabilityCode.ADDRESS_NOT_SERVED), **extra
        )
