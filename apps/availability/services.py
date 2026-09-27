"""Le juge unique de « peut-on commander ? » — incohérence I2 de l'audit.

## Ce qui existait, et pourquoi c'était un défaut

Sept questions composent « ce client peut-il commander cet article
maintenant ? », et sept lieux y répondaient. Chacun était juste ; aucun ne
composait. Deux conséquences, toutes deux vérifiées :

* **la cuisine fermée encaissait.** La règle d'ouverture n'existait que dans un
  sérialiseur d'affichage ; la création de commande ne la lisait pas. Un test
  l'a prouvé avant la correction : 201, une commande en base, sur un
  établissement sans aucune plage d'ouverture ;
* **la rupture d'ingrédient ne retirait rien de la carte.** La réservation de
  matière refusait bien la commande, mais au paiement — la carte, elle, ne
  pouvait pas savoir, le catalogue n'ayant pas le droit de connaître les
  recettes.

## Ce que fait ce module

Il **compose**, il ne décide rien lui-même :

    la cuisine    apps.restaurants.availability   publiée, ouverte, disponible
    l'article     apps.catalog.availability       au menu, actif, en stock, options
    la matière    apps.production.services        de quoi le préparer

Chaque règle reste où vivent ses données. Ce module est le seul endroit qui les
enchaîne, dans un ordre fixe, et rend **un** motif — jamais une collection de
booléens que l'appelant recomposerait.

## Ce qu'il ne juge pas encore

**La capacité de production.** Il n'existe aujourd'hui ni poste, ni charge, ni
file : aucune donnée ne permet de dire qu'une cuisine est saturée. Ajouter ici
une question qui répondrait toujours « oui » ferait croire qu'elle est posée.
Elle viendra avec les postes de travail, et c'est ici qu'elle se branchera.

**La zone de livraison, à la carte.** Elle dépend de l'adresse, que la carte ne
connaît pas. Elle a son lieu unique, `apps.restaurants.delivery.check_delivery`,
et c'est `can_accept_order` qui la compose avec le reste au moment de commander.

## `can_accept_order` — la règle du moment critique

Les verdicts ci-dessus informent : la carte, le panier, le devis. La commande,
elle, doit **trancher**, et elle tranchait en quatre endroits d'`OrderService`
écrits à la suite — la cuisine, les articles, le numéro, la zone. Chaque appel
était juste ; leur ordre et leur complétude n'étaient garantis par rien
d'autre que la relecture. `can_accept_order` les tient en un lieu, dans un
ordre fixe, et c'est elle que la création de commande appelle.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from django.contrib.gis.geos import Point
from django.db.models import prefetch_related_objects

from apps.catalog.availability import customization_unavailability, item_unavailability
from apps.catalog.models import MenuItem, Option, Variant
from apps.geography.services import DeliveryQuote
from apps.production.services import MaterialService, MaterialShortage, ProducedLine
from apps.restaurants.availability import kitchen_unavailability, lock_kitchen_for_order
from apps.restaurants.delivery import DeliveryAvailability, check_delivery
from apps.restaurants.models import Restaurant
from common.availability import (
    KitchenNotOrderable,
    Unavailability,
    UnavailabilityCode,
)
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["AvailabilityService", "Demand", "OrderAcceptance"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Demand:
    """Ce que le client veut : un article, combien, avec quelles options.

    Détaché des lignes de panier : le panier personnel et le panier
    collaboratif ont chacun la leur, et ce module ne doit dépendre d'aucun des
    deux — c'est le panier qui interroge le juge, jamais l'inverse.
    """

    menu_item: MenuItem
    quantity: int = 1
    options: Sequence[Option] = field(default_factory=tuple)
    variant: Variant | None = None


@dataclass(frozen=True, slots=True)
class OrderAcceptance:
    """Le verdict de `can_accept_order` — et ce qu'il a établi pour écrire.

    `kitchen` est la cuisine **relue** au moment du jugement, pas celle qu'on a
    passée : c'est elle que la commande doit employer (délai de préparation,
    devise), sans quoi elle écrirait d'après une instance périmée.
    """

    kitchen: Restaurant
    refusal: BusinessRuleViolation | None
    delivery: DeliveryAvailability | None = None
    line_verdicts: Sequence[Unavailability | None] = ()

    @property
    def accepted(self) -> bool:
        return self.refusal is None

    @property
    def quote(self) -> DeliveryQuote:
        """Le devis de la course. N'existe que pour une commande acceptée."""
        if self.delivery is None or self.delivery.quote is None:
            raise RuntimeError("Aucun devis : la commande n'a pas été acceptée.")
        return self.delivery.quote


class AvailabilityService:
    # ---------------------------------------------------------------- cuisine

    @staticmethod
    def kitchen(restaurant: Restaurant, at: dt.datetime | None = None) -> Unavailability | None:
        """La cuisine peut-elle prendre une commande ? `None` si oui."""
        return kitchen_unavailability(restaurant, at)

    # --------------------------------------------------------------- commande

    @staticmethod
    def can_accept_order(
        *,
        restaurant: Restaurant,
        demands: Sequence[Demand],
        delivery_point: Point,
        subtotal: Money,
        at: dt.datetime | None = None,
    ) -> OrderAcceptance:
        """**La** règle : cette cuisine peut-elle accepter cette commande, maintenant ?

        ## L'ordre, du plus général au plus précis

        1. la cuisine existe ;
        2. à 4. elle est en service, ouverte, et prend les commandes — relue
           **sous verrou partagé** (`lock_kitchen_for_order`) : une pause
           déclarée pendant que le client paie est vue, ou attend la fin de la
           commande ;
        5. le panier est cohérent : non vide, d'une seule cuisine, dans sa
           devise ;
        6. l'adresse est dans sa desserte ;
        7. à 9. chaque article est au menu, disponible, en stock, préparable, et
           ses options respectent encore les règles de l'article ;
        10. le barème de la zone accepte le panier (minimum de commande).

        Dire « ajoutez un article » à quelqu'un qui habite hors zone, ou « ce
        plat est épuisé » à quelqu'un dont la cuisine est fermée, ne lui
        apprendrait rien : le premier refus rendu est le plus actionnable.

        ## Ce qu'elle ne fait pas

        **Écrire.** Les prix sont relus du catalogue par `price_selection`
        (C1), le stock de plats finis se décompte et la matière se réserve
        **sous verrou** dans `OrderService` : ce sont eux, et non cette lecture,
        qui tranchent entre deux commandes simultanées pour le dernier pain. La
        lecture informe, la réservation tranche — et une commande refusée ici
        n'a rien réservé.

        Ne lève pas : rend un verdict. `assert_can_accept_order` lève.
        """
        try:
            cuisine = lock_kitchen_for_order(restaurant)
        except Restaurant.DoesNotExist:
            acceptance = OrderAcceptance(
                kitchen=restaurant,
                refusal=KitchenNotOrderable(
                    Unavailability(
                        code=UnavailabilityCode.KITCHEN_UNPUBLISHED,
                        message="Cette cuisine n'existe plus.",
                    )
                ),
            )
            _journaliser(acceptance, lignes=len(demands))
            return acceptance

        acceptance = _juger_la_commande(
            cuisine=cuisine,
            demands=demands,
            delivery_point=delivery_point,
            subtotal=subtotal,
            at=at,
        )
        _journaliser(acceptance, lignes=len(demands))
        return acceptance

    @staticmethod
    def assert_can_accept_order(
        *,
        restaurant: Restaurant,
        demands: Sequence[Demand],
        delivery_point: Point,
        subtotal: Money,
        at: dt.datetime | None = None,
    ) -> OrderAcceptance:
        """`can_accept_order`, qui **lève** le refus au lieu de le rendre."""
        acceptance = AvailabilityService.can_accept_order(
            restaurant=restaurant,
            demands=demands,
            delivery_point=delivery_point,
            subtotal=subtotal,
            at=at,
        )
        if acceptance.refusal is not None:
            raise acceptance.refusal
        return acceptance

    # ---------------------------------------------------------------- panier

    @staticmethod
    def demands(demands: Sequence[Demand]) -> list[Unavailability | None]:
        """Le verdict de chaque ligne d'un panier, dans l'ordre des lignes.

        ## Le catalogue d'abord, la matière ensuite

        Un article retiré du menu n'a pas à être pesé. Seules les lignes que le
        catalogue accepte sont soumises à la matière — ce qui évite aussi
        d'annoncer « rupture » pour un plat qui n'est de toute façon plus servi.

        ## Les lignes se cumulent

        Deux burgers et un sandwich partagent le même pain : chaque ligne
        tiendrait seule, le panier non. C'est le seul cas que la carte ne peut
        pas voir, et c'est pourquoi le panier est jugé **entier**, pas ligne à
        ligne.

        Deux requêtes pour la matière par cuisine concernée, quel que soit le
        nombre de lignes.
        """
        verdicts: list[Unavailability | None] = [
            item_unavailability(demand.menu_item, options=demand.options, quantity=demand.quantity)
            for demand in demands
        ]

        par_cuisine: dict[uuid.UUID, list[int]] = defaultdict(list)
        for rang, (demand, verdict) in enumerate(zip(demands, verdicts, strict=True)):
            if verdict is None:
                par_cuisine[demand.menu_item.restaurant_id].append(rang)

        for restaurant_id, rangs in par_cuisine.items():
            manques = MaterialService.shortages(
                restaurant_id=restaurant_id,
                lines=[
                    ProducedLine(
                        menu_item_id=demands[rang].menu_item.pk,
                        quantity=demands[rang].quantity,
                        option_ids=tuple(option.pk for option in demands[rang].options),
                    )
                    for rang in rangs
                ],
            )
            for rang, manque in zip(rangs, manques, strict=True):
                if manque is not None:
                    verdicts[rang] = _rupture(manque, detaillee=True)

        return verdicts

    # ------------------------------------------------------------------ carte

    @staticmethod
    def menu(items: Sequence[MenuItem]) -> dict[uuid.UUID, Unavailability]:
        """Pourquoi chacun de ces articles n'est pas commandable — les autres sont absents.

        Une portion, sans option : c'est la question que pose la carte. Chaque
        article est jugé **seul** — personne ne commande la carte entière, et
        cumuler les besoins de vingt plats annoncerait des ruptures qu'aucun
        client ne rencontrerait.

        Le motif de rupture ne dit pas combien de portions restent : la carte
        est publique, et l'état d'un stock n'a pas à s'y lire. Le panier, lui,
        le dit à celui qui est en train de commander.
        """
        verdicts: dict[uuid.UUID, Unavailability] = {}
        par_cuisine: dict[uuid.UUID, list[MenuItem]] = defaultdict(list)

        for item in items:
            verdict = item_unavailability(item)
            if verdict is not None:
                verdicts[item.pk] = verdict
            else:
                par_cuisine[item.restaurant_id].append(item)

        for restaurant_id, articles in par_cuisine.items():
            manques = MaterialService.shortages(
                restaurant_id=restaurant_id,
                lines=[ProducedLine(menu_item_id=item.pk, quantity=1) for item in articles],
                independent=True,
            )
            for item, manque in zip(articles, manques, strict=True):
                if manque is not None:
                    verdicts[item.pk] = _rupture(manque, detaillee=False)

        return verdicts


def _juger_la_commande(
    *,
    cuisine: Restaurant,
    demands: Sequence[Demand],
    delivery_point: Point,
    subtotal: Money,
    at: dt.datetime | None,
) -> OrderAcceptance:
    """Le corps de `can_accept_order`, sur une cuisine déjà relue."""

    def refus(
        motif: BusinessRuleViolation,
        *,
        livraison: DeliveryAvailability | None = None,
        lignes: Sequence[Unavailability | None] = (),
    ) -> OrderAcceptance:
        return OrderAcceptance(
            kitchen=cuisine, refusal=motif, delivery=livraison, line_verdicts=lignes
        )

    # 2 à 4 — la cuisine.
    verdict_cuisine = kitchen_unavailability(cuisine, at)
    if verdict_cuisine is not None:
        return refus(KitchenNotOrderable(verdict_cuisine))

    # 5 — un panier cohérent.
    if not demands:
        return refus(BusinessRuleViolation("Le panier est vide."))
    if any(demand.menu_item.restaurant_id != cuisine.pk for demand in demands):
        return refus(
            BusinessRuleViolation(
                "Le panier contient des articles d'une autre cuisine : une commande "
                "est préparée en un seul lieu.",
                restaurant_id=str(cuisine.pk),
            )
        )
    if subtotal.currency != cuisine.currency:
        return refus(
            BusinessRuleViolation(
                f"Le panier est libellé en {subtotal.currency}, la cuisine facture en "
                f"{cuisine.currency}.",
                cart_currency=subtotal.currency,
            )
        )

    # 6 — la desserte. Le barème (10) est chiffré par le même appel, mais son
    # refus attend que les articles aient été jugés : un minimum de commande
    # se calcule sur un panier dont on sait qu'il est commandable.
    livraison = check_delivery(point=delivery_point, restaurant=cuisine, subtotal=subtotal)
    if livraison.unavailable_code == UnavailabilityCode.ADDRESS_NOT_SERVED:
        return refus(
            livraison.refusal or BusinessRuleViolation(livraison.reason or ""),
            livraison=livraison,
        )

    # 7 à 9 — les articles, leur matière, leurs personnalisations.
    verdicts = AvailabilityService.demands(demands)
    prefetch_related_objects([demand.menu_item for demand in demands], "option_groups", "variants")
    verdicts = [
        verdict
        if verdict is not None
        else customization_unavailability(demand.menu_item, demand.options, demand.variant)
        for demand, verdict in zip(demands, verdicts, strict=True)
    ]
    bloquees = [
        (demand, verdict)
        for demand, verdict in zip(demands, verdicts, strict=True)
        if verdict is not None
    ]
    if bloquees:
        noms = [demand.menu_item.name for demand, _ in bloquees]
        return refus(
            BusinessRuleViolation(
                f"Certains articles ne sont plus commandables : {', '.join(noms)}. "
                "Retirez-les du panier.",
                unavailable=noms,
                # Le motif de chaque ligne, dans le même ordre : « épuisé » et
                # « plus au menu » n'appellent pas le même geste, et le client
                # ne doit pas avoir à deviner lequel s'applique.
                unavailable_codes=[str(verdict.code) for _, verdict in bloquees],
            ),
            livraison=livraison,
            lignes=verdicts,
        )

    # 10 — le barème.
    if livraison.quote is None:
        return refus(
            livraison.refusal
            or BusinessRuleViolation(
                livraison.reason or "Cette adresse n'est desservie par aucune zone de livraison."
            ),
            livraison=livraison,
            lignes=verdicts,
        )

    return OrderAcceptance(
        kitchen=cuisine, refusal=None, delivery=livraison, line_verdicts=verdicts
    )


def _journaliser(acceptance: OrderAcceptance, *, lignes: int) -> None:
    """Pourquoi la commande a été acceptée ou refusée — sans donnée personnelle.

    Ni adresse, ni coordonnées, ni client : la cuisine, le motif et le nombre
    de lignes suffisent à relire un refus, et le `request_id` que pose
    `common.observabilite` relie la ligne au reste de la requête.
    """
    refus = acceptance.refusal
    contexte = refus.extra if refus is not None else {}
    logger.info(
        "order.acceptance",
        extra={
            "kitchen": acceptance.kitchen.slug,
            "accepted": refus is None,
            "refusal_code": refus.code if refus is not None else None,
            "unavailable_code": contexte.get("unavailable_code"),
            "unavailable_codes": contexte.get("unavailable_codes"),
            "lines": lignes,
        },
    )


def _rupture(manque: MaterialShortage, *, detaillee: bool) -> Unavailability:
    """Traduit un manque de matière en motif lisible par le client.

    Les ingrédients en cause ne sont **pas** nommés : « rupture de pain » est
    une information de cuisine, pas un choix offert au client. Le nombre de
    portions possibles, lui, est ce qui lui permet d'agir — ramener trois
    burgers à deux plutôt que de tout retirer.
    """
    if manque.shared:
        return Unavailability(
            code=UnavailabilityCode.INGREDIENT_SHORTAGE,
            message=("Le stock ne permet pas de préparer tout le panier : réduisez les quantités."),
            details={"portions_possible": manque.portions_possible} if detaillee else {},
        )

    if manque.portions_possible == 0 or not detaillee:
        return Unavailability(
            code=UnavailabilityCode.INGREDIENT_SHORTAGE,
            message="Cet article est en rupture pour le moment.",
            details={"portions_possible": 0} if detaillee else {},
        )

    return Unavailability(
        code=UnavailabilityCode.INGREDIENT_SHORTAGE,
        message=(f"Il n'est possible d'en préparer que {manque.portions_possible} pour le moment."),
        details={"portions_possible": manque.portions_possible},
    )
