"""Tarification et manipulation du panier — invariant C1.

Le panier ne stocke aucun montant. Ce module est donc le seul endroit qui
sache ce que coûte une ligne, et il le recalcule **à chaque lecture** depuis le
catalogue. Un panier oublié une semaine affiche donc le prix du jour, pas celui
de la semaine dernière ; l'implémentation précédente facturait le second.

Le service existe ici parce qu'il porte deux décisions réelles (ADR-003) : la
validation des bornes de groupes d'options, et la fusion de deux lignes qui
désignent exactement le même choix.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from django.db import transaction
from django.db.models import Prefetch, prefetch_related_objects

from apps.accounts.models import User
from apps.availability.services import AvailabilityService, Demand
from apps.carts.models import Cart, CartLine, CartLineOption
from apps.catalog.availability import customization_unavailability, item_unavailability
from apps.catalog.models import MenuItem, Option, Variant
from apps.restaurants.models import Restaurant
from common.availability import Unavailability, UnavailabilityCode
from common.exceptions import BusinessRuleViolation
from common.money import CurrencyMismatch, Money

__all__ = [
    "CartService",
    "PriceableLine",
    "PricedCart",
    "PricedLine",
    "PricedSelection",
    "price_cart",
    "price_selection",
    "validate_selection",
]

#: Motifs qui interdisent l'**ajout** au panier, et pas seulement la commande.
_NON_SERVI = frozenset({UnavailabilityCode.ITEM_WITHDRAWN, UnavailabilityCode.ITEM_UNAVAILABLE})


class PriceableLine(Protocol):
    """Ce qu'une ligne doit porter pour être valorisée.

    Le protocole couvre la ligne du panier personnel comme celle du panier
    collaboratif. Sans lui, chacun des deux aurait sa propre boucle de calcul —
    et le jour où l'une apprend à déduire un « sans fromage », l'autre continue
    de le facturer. C1 ne dit pas seulement que le prix vient du serveur, il dit
    qu'il en vient **par un seul chemin**.
    """

    menu_item: MenuItem
    menu_item_id: uuid.UUID
    variant: Variant | None
    quantity: int
    notes: str

    def selected_options(self) -> list[Option]: ...


@dataclass(frozen=True, slots=True)
class PricedLine:
    """Ligne valorisée à l'instant de la lecture.

    `unavailability` est le verdict du juge (`apps.availability`) ; les deux
    champs plats qui le suivent en sont la lecture pour les sérialiseurs, qui
    ne savent pas traverser un objet facultatif sans une branche de plus.
    """

    line: PriceableLine
    options: Sequence[Option]
    unit_price: Money
    total: Money
    unavailability: Unavailability | None = None

    @property
    def is_orderable(self) -> bool:
        return self.unavailability is None

    @property
    def unavailable_code(self) -> str:
        return str(self.unavailability.code) if self.unavailability is not None else ""

    @property
    def unavailable_reason(self) -> str:
        return self.unavailability.message if self.unavailability is not None else ""


@dataclass(frozen=True, slots=True)
class PricedSelection:
    """Un ensemble de lignes valorisé — sans dire d'où elles viennent.

    C'est ce que `orders` consomme pour créer une commande : que la sélection
    vienne d'un panier personnel ou d'un panier collaboratif ne change rien au
    calcul du total, et lui faire connaître les deux origines l'aurait fait
    grossir d'une branche à chaque nouvelle façon de remplir un panier.

    `kitchen` est le verdict de la cuisine **au moment de la lecture**. Il sert
    à l'affichage — dire « fermé » sur l'écran du panier plutôt qu'au paiement.
    La création de commande ne s'y fie pas, ni aux verdicts des lignes : elle
    repose toutes les questions au moment où elle écrit
    (`AvailabilityService.can_accept_order`).
    """

    lines: Sequence[PricedLine]
    subtotal: Money
    currency: str
    kitchen: Unavailability | None = None

    @property
    def is_orderable(self) -> bool:
        """Vrai si la cuisine prend la commande et si toute ligne peut être commandée.

        Un panier partiellement commandable n'est pas commandé partiellement :
        le client doit retirer explicitement ce qui ne l'est plus. Décider à sa
        place produirait une commande qu'il n'a pas relue.
        """
        return (
            self.kitchen is None
            and bool(self.lines)
            and all(line.is_orderable for line in self.lines)
        )


@dataclass(frozen=True, slots=True)
class PricedCart:
    cart: Cart
    lines: Sequence[PricedLine]
    subtotal: Money
    currency: str
    kitchen: Unavailability | None = None

    @property
    def is_orderable(self) -> bool:
        return self.selection.is_orderable

    @property
    def unavailable_code(self) -> str:
        """Motif de la cuisine — vide si elle prend les commandes."""
        return str(self.kitchen.code) if self.kitchen is not None else ""

    @property
    def unavailable_reason(self) -> str:
        return self.kitchen.message if self.kitchen is not None else ""

    @property
    def selection(self) -> PricedSelection:
        """Vue « sélection » du panier, pour la création de commande."""
        return PricedSelection(
            lines=self.lines,
            subtotal=self.subtotal,
            currency=self.currency,
            kitchen=self.kitchen,
        )


def price_selection(
    lines: Iterable[PriceableLine],
    currency: str,
    restaurant: Restaurant | None = None,
) -> PricedSelection:
    """Valorise un ensemble de lignes depuis le catalogue.

    Le prix unitaire est celui de l'article **plus** les écarts des options
    retenues — un supplément fromage se paie, un « sans fromage » peut se
    déduire. Aucune de ces valeurs ne vient de la requête.

    C'est le seul endroit du projet qui sache ce que coûte une ligne, et il
    ignore délibérément à quel type de panier elle appartient.

    ## Ce que la ligne peut être commandée, il ne le décide pas

    Il le **demande** au juge de disponibilité, en une fois pour tout le panier.
    Le juge ligne à ligne qui vivait ici ne voyait ni la catégorie éteinte, ni
    la matière, ni la cuisine fermée — et il était le seul avis que la création
    de commande consultait.

    `restaurant`, s'il est fourni, fait aussi juger la cuisine.
    """
    subtotal = Money.zero(currency)
    valorisees: list[tuple[PriceableLine, list[Option], Money, Money]] = []

    for line in lines:
        options = line.selected_options()
        # La taille **remplace** le prix de base (lot 2, prix absolu) ; les
        # options s'ajoutent ensuite, qu'il y ait une taille ou non.
        unit = line.variant.price if line.variant is not None else line.menu_item.price
        for option in options:
            unit += option.price_delta

        total = unit * line.quantity
        subtotal += total
        valorisees.append((line, options, unit, total))

    verdicts = AvailabilityService.demands(
        [
            Demand(
                menu_item=line.menu_item,
                quantity=line.quantity,
                options=options,
                variant=line.variant,
            )
            for line, options, _, _ in valorisees
        ]
    )

    # La personnalisation se juge aussi à la lecture du panier, et plus
    # seulement à l'ajout et à la commande : une taille éteinte après coup, ou
    # des bornes d'options changées, doivent se voir **sur la ligne** — pas au
    # moment de payer. Même règle que la commande (`customization_unavailability`).
    prefetch_related_objects(
        [line.menu_item for line, *_ in valorisees], "option_groups", "variants"
    )
    verdicts = [
        verdict
        if verdict is not None
        else customization_unavailability(line.menu_item, options, line.variant)
        for (line, options, _, _), verdict in zip(valorisees, verdicts, strict=True)
    ]

    priced = [
        PricedLine(line=line, options=options, unit_price=unit, total=total, unavailability=verdict)
        for (line, options, unit, total), verdict in zip(valorisees, verdicts, strict=True)
    ]

    return PricedSelection(
        lines=priced,
        subtotal=subtotal,
        currency=currency,
        kitchen=AvailabilityService.kitchen(restaurant) if restaurant is not None else None,
    )


def price_cart(cart: Cart) -> PricedCart:
    """Valorise le panier personnel d'un client."""
    currency = cart.restaurant.currency
    selection = price_selection(cart.lines.all(), currency, restaurant=cart.restaurant)
    return PricedCart(
        cart=cart,
        lines=selection.lines,
        subtotal=selection.subtotal,
        currency=currency,
        kitchen=selection.kitchen,
    )


def validate_selection(
    menu_item: MenuItem, options: Sequence[Option], variant: Variant | None = None
) -> None:
    """Vérifie que les options retenues respectent les bornes de leurs groupes.

    Les bornes sont en donnée (`min_select`, `max_select`) et non en code :
    l'exploitation crée « 2 accompagnements parmi 5 » sans développement. La
    contrepartie est que la validation doit être générique — et elle vit dans
    le catalogue (`customization_unavailability`), que la commande relit aussi :
    les bornes ont pu changer entre l'ajout et le paiement.
    """
    verdict = customization_unavailability(menu_item, options, variant)
    if verdict is not None:
        raise BusinessRuleViolation(verdict.message, **verdict.details)

    for option in options:
        # Refus **à l'écriture**, et non seulement au moment de commander.
        # Le juge de disponibilité marque déjà la ligne dont une option s'est éteinte
        # après coup — c'est le cas qu'on ne peut pas empêcher. Retenir une
        # option déjà indisponible, en revanche, se refuse tout de suite :
        # l'accepter laissait composer un panier qui ne pouvait plus être
        # commandé, et le client ne l'apprenait qu'à la validation.
        if not option.is_available:
            raise BusinessRuleViolation(
                f"L'option « {option.name} » n'est pas disponible.",
                option_id=str(option.pk),
            )


class CartService:
    @staticmethod
    def cart_for(user: User, restaurant: Restaurant) -> Cart:
        cart, _ = Cart.objects.get_or_create(user=user, restaurant=restaurant)
        return cart

    @staticmethod
    def load(cart: Cart) -> Cart:
        """Recharge un panier avec tout ce que la valorisation demande.

        Sans ces préchargements, un panier de dix lignes déclenche une requête
        par ligne pour l'article, puis une par ligne pour ses options, puis une
        par option pour son groupe.
        """
        return (
            Cart.objects.select_related("restaurant__zone__city__country")
            .prefetch_related(
                # Le juge de la cuisine lit les plages d'ouverture ; les
                # précharger ici évite de les relire à la valorisation puis à la
                # création de commande, qui reçoit ce même établissement.
                "restaurant__opening_hours",
                Prefetch(
                    "lines",
                    # La catégorie est jugée avec l'article : une catégorie
                    # éteinte rend ses articles incommandables.
                    queryset=CartLine.objects.select_related(
                        "menu_item__category", "variant"
                    ).prefetch_related(
                        Prefetch(
                            "options",
                            queryset=CartLineOption.objects.select_related("option__group"),
                        )
                    ),
                ),
            )
            .get(pk=cart.pk)
        )

    @staticmethod
    @transaction.atomic
    def add_line(
        *,
        cart: Cart,
        menu_item: MenuItem,
        quantity: int,
        options: Sequence[Option],
        notes: str = "",
        variant: Variant | None = None,
    ) -> CartLine:
        """Ajoute un article, ou renforce la ligne identique si elle existe.

        « Identique » signifie même article, même jeu d'options **et** même
        note. Deux burgers de cuissons différentes restent deux lignes ; deux
        fois le même burger n'en font qu'une, de quantité 2 — sans quoi le
        panier se remplit de doublons à chaque tapotement du bouton.
        """
        CartService._assert_belongs_to_cart(cart, menu_item)
        validate_selection(menu_item, options, variant)

        existing = CartService._identical_line(cart, menu_item, options, notes, variant=variant)
        if existing is not None:
            existing.quantity += quantity
            existing.save(update_fields=["quantity", "updated_at"])
            return existing

        line = CartLine.objects.create(
            cart=cart, menu_item=menu_item, variant=variant, quantity=quantity, notes=notes
        )
        CartLineOption.objects.bulk_create(
            CartLineOption(line=line, option=option) for option in options
        )
        return line

    @staticmethod
    def _assert_belongs_to_cart(cart: Cart, menu_item: MenuItem) -> None:
        """Le panier est rattaché à un restaurant : une commande ne peut pas
        mélanger deux établissements, puisqu'elle est préparée à un endroit et
        enlevée en un seul point."""
        if menu_item.restaurant_id != cart.restaurant_id:
            raise BusinessRuleViolation(
                "Cet article appartient à une autre cuisine.",
                restaurant_id=str(cart.restaurant_id),
            )
        # Refus **à l'écriture** de ce qui n'est plus servi — retiré, désactivé,
        # ou rangé dans une catégorie éteinte —, par la même règle que le juge.
        #
        # Le stock et la matière, eux, ne bloquent pas l'ajout : ils varient
        # d'une minute à l'autre, et le panier les annonce sur la ligne avec le
        # nombre de portions encore possibles. Refuser l'ajout ferait perdre au
        # client un choix qu'une réception de marchandise rendra possible.
        verdict = item_unavailability(menu_item)
        if verdict is not None and verdict.code in _NON_SERVI:
            raise BusinessRuleViolation(f"« {menu_item.name} » n'est pas disponible.")

        try:
            menu_item.price + Money.zero(cart.restaurant.currency)
        except CurrencyMismatch as exc:
            # Un article tarifé dans une autre devise que son marché est une
            # incohérence de données ; l'accepter produirait un total faux dont
            # personne ne verrait l'origine.
            raise BusinessRuleViolation(str(exc)) from exc

    @staticmethod
    def _identical_line(
        cart: Cart,
        menu_item: MenuItem,
        options: Sequence[Option],
        notes: str,
        exclude: uuid.UUID | None = None,
        variant: Variant | None = None,
    ) -> CartLine | None:
        """Ligne du panier qui porte exactement ce choix, s'il en existe une.

        `exclude` écarte la ligne qu'on est en train de réécrire : sans lui,
        une modification se trouverait elle-même — ses options venant d'être
        enregistrées — et se fusionnerait avec elle-même, doublant sa quantité
        avant de se supprimer.
        """
        wanted = {option.pk for option in options}
        # La taille distingue deux lignes comme les options : une Petite et une
        # Grande ne fusionnent pas.
        candidates = cart.lines.filter(menu_item=menu_item, notes=notes, variant=variant)
        if exclude is not None:
            candidates = candidates.exclude(pk=exclude)
        for line in candidates.prefetch_related("options"):
            if {selection.option_id for selection in line.options.all()} == wanted:
                return line
        return None

    @staticmethod
    def set_quantity(line: CartLine, quantity: int) -> CartLine:
        line.quantity = quantity
        line.save(update_fields=["quantity", "updated_at"])
        return line

    @staticmethod
    @transaction.atomic
    def update_line(
        *,
        line: CartLine,
        options: Sequence[Option],
        quantity: int | None = None,
        notes: str | None = None,
        variant: Variant | None = None,
    ) -> CartLine:
        """Rejoue la personnalisation d'une ligne déjà au panier.

        Le client rouvre le configurateur depuis le panier, change la cuisson,
        enregistre : c'est **cette** ligne qui est réécrite, pas une nouvelle.
        Faire supprimer puis rajouter par le client aurait le même effet nominal
        mais perdrait la ligne si le second appel échouait — et le panier ne
        garde aucune trace permettant de la reconstituer.

        Les options repassent par `validate_selection` : une ligne modifiée est
        une ligne nouvellement composée, et rien ne garantit que le catalogue
        n'a pas bougé depuis l'ajout. Le prix, lui, n'est pas touché — il n'est
        stocké nulle part (C1) et se relit à la lecture suivante.

        Si la modification rend cette ligne identique à une autre du même
        panier, les deux fusionnent : c'est la règle de `add_line`, et deux
        lignes que rien ne distingue plus n'ont pas de raison de rester deux.
        """
        menu_item = line.menu_item
        CartService._assert_belongs_to_cart(line.cart, menu_item)
        validate_selection(menu_item, options, variant)

        if quantity is not None:
            line.quantity = quantity
        if notes is not None:
            line.notes = notes
        line.variant = variant
        line.save(update_fields=["quantity", "notes", "variant", "updated_at"])

        line.options.all().delete()
        CartLineOption.objects.bulk_create(
            CartLineOption(line=line, option=option) for option in options
        )

        jumelle = CartService._identical_line(
            line.cart, menu_item, options, line.notes, exclude=line.pk, variant=variant
        )
        if jumelle is not None:
            jumelle.quantity += line.quantity
            jumelle.save(update_fields=["quantity", "updated_at"])
            line.delete()
            return jumelle

        return line

    @staticmethod
    def clear(cart: Cart) -> None:
        cart.lines.all().delete()
