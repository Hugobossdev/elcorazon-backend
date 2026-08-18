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
from django.db.models import Prefetch

from apps.accounts.models import User
from apps.carts.models import Cart, CartLine, CartLineOption
from apps.catalog.models import MenuItem, Option
from apps.restaurants.models import Restaurant
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
    quantity: int
    notes: str

    def selected_options(self) -> list[Option]: ...


@dataclass(frozen=True, slots=True)
class PricedLine:
    """Ligne valorisée à l'instant de la lecture."""

    line: PriceableLine
    options: Sequence[Option]
    unit_price: Money
    total: Money
    is_orderable: bool
    unavailable_reason: str


@dataclass(frozen=True, slots=True)
class PricedSelection:
    """Un ensemble de lignes valorisé — sans dire d'où elles viennent.

    C'est ce que `orders` consomme pour créer une commande : que la sélection
    vienne d'un panier personnel ou d'un panier collaboratif ne change rien au
    calcul du total, et lui faire connaître les deux origines l'aurait fait
    grossir d'une branche à chaque nouvelle façon de remplir un panier.
    """

    lines: Sequence[PricedLine]
    subtotal: Money
    currency: str

    @property
    def is_orderable(self) -> bool:
        """Vrai si toute ligne peut être commandée.

        Un panier partiellement commandable n'est pas commandé partiellement :
        le client doit retirer explicitement ce qui ne l'est plus. Décider à sa
        place produirait une commande qu'il n'a pas relue.
        """
        return bool(self.lines) and all(line.is_orderable for line in self.lines)

    @property
    def unavailable_names(self) -> list[str]:
        """Articles qui bloquent la commande, pour le message de refus."""
        return [priced.line.menu_item.name for priced in self.lines if not priced.is_orderable]


@dataclass(frozen=True, slots=True)
class PricedCart:
    cart: Cart
    lines: Sequence[PricedLine]
    subtotal: Money
    currency: str

    @property
    def is_orderable(self) -> bool:
        return self.selection.is_orderable

    @property
    def selection(self) -> PricedSelection:
        """Vue « sélection » du panier, pour la création de commande."""
        return PricedSelection(lines=self.lines, subtotal=self.subtotal, currency=self.currency)


def _unavailability(item: MenuItem, options: Iterable[Option], quantity: int) -> str:
    """Ce qui empêche de commander cette ligne — chaîne vide si rien.

    Le motif est rendu au client plutôt qu'un simple booléen : « plus au menu »
    et « momentanément indisponible » n'appellent pas le même geste, et un
    panier qui refuse sans dire pourquoi se termine par un appel au support.

    La rupture de stock est annoncée **ici**, avec le nombre restant. Le refus
    ferme est celui de la création de commande, qui décompte sous verrou ; ce
    que le panier apporte, c'est de le faire savoir avant le paiement plutôt
    qu'après.
    """
    if item.is_deleted:
        return "Cet article n'est plus au menu."
    if not item.is_available:
        return "Cet article est momentanément indisponible."
    if item.tracks_stock and item.stock_quantity < quantity:
        return f"Il n'en reste que {item.stock_quantity} en stock."
    indisponibles = sorted(option.name for option in options if not option.is_available)
    if indisponibles:
        return f"Options indisponibles : {', '.join(indisponibles)}."
    return ""


def price_selection(lines: Iterable[PriceableLine], currency: str) -> PricedSelection:
    """Valorise un ensemble de lignes depuis le catalogue.

    Le prix unitaire est celui de l'article **plus** les écarts des options
    retenues — un supplément fromage se paie, un « sans fromage » peut se
    déduire. Aucune de ces valeurs ne vient de la requête.

    C'est le seul endroit du projet qui sache ce que coûte une ligne, et il
    ignore délibérément à quel type de panier elle appartient.
    """
    subtotal = Money.zero(currency)
    priced: list[PricedLine] = []

    for line in lines:
        options = line.selected_options()
        unit = line.menu_item.price
        for option in options:
            unit += option.price_delta

        total = unit * line.quantity
        subtotal += total
        reason = _unavailability(line.menu_item, options, line.quantity)
        priced.append(
            PricedLine(
                line=line,
                options=options,
                unit_price=unit,
                total=total,
                is_orderable=not reason,
                unavailable_reason=reason,
            )
        )

    return PricedSelection(lines=priced, subtotal=subtotal, currency=currency)


def price_cart(cart: Cart) -> PricedCart:
    """Valorise le panier personnel d'un client."""
    currency = cart.restaurant.currency
    selection = price_selection(cart.lines.all(), currency)
    return PricedCart(
        cart=cart, lines=selection.lines, subtotal=selection.subtotal, currency=currency
    )


def validate_selection(menu_item: MenuItem, options: Sequence[Option]) -> None:
    """Vérifie que les options retenues respectent les bornes de leurs groupes.

    Les bornes sont en donnée (`min_select`, `max_select`) et non en code :
    l'exploitation crée « 2 accompagnements parmi 5 » sans développement. La
    contrepartie est que la validation doit être générique — c'est celle-ci.
    """
    groups = {group.pk: group for group in menu_item.option_groups.all()}

    for option in options:
        if option.group_id not in groups:
            raise BusinessRuleViolation(
                f"L'option « {option.name} » n'appartient pas à cet article.",
                option_id=str(option.pk),
            )

    for group in groups.values():
        retenues = [option for option in options if option.group_id == group.pk]
        if len(retenues) < group.min_select:
            raise BusinessRuleViolation(
                f"« {group.name} » exige au moins {group.min_select} choix.",
                option_group_id=str(group.pk),
            )
        if len(retenues) > group.max_select:
            raise BusinessRuleViolation(
                f"« {group.name} » accepte au plus {group.max_select} choix.",
                option_group_id=str(group.pk),
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
                Prefetch(
                    "lines",
                    queryset=CartLine.objects.select_related("menu_item").prefetch_related(
                        Prefetch(
                            "options",
                            queryset=CartLineOption.objects.select_related("option__group"),
                        )
                    ),
                )
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
    ) -> CartLine:
        """Ajoute un article, ou renforce la ligne identique si elle existe.

        « Identique » signifie même article, même jeu d'options **et** même
        note. Deux burgers de cuissons différentes restent deux lignes ; deux
        fois le même burger n'en font qu'une, de quantité 2 — sans quoi le
        panier se remplit de doublons à chaque tapotement du bouton.
        """
        CartService._assert_belongs_to_cart(cart, menu_item)
        validate_selection(menu_item, options)

        existing = CartService._identical_line(cart, menu_item, options, notes)
        if existing is not None:
            existing.quantity += quantity
            existing.save(update_fields=["quantity", "updated_at"])
            return existing

        line = CartLine.objects.create(
            cart=cart, menu_item=menu_item, quantity=quantity, notes=notes
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
                "Cet article appartient à un autre restaurant.",
                restaurant_id=str(cart.restaurant_id),
            )
        if menu_item.is_deleted or not menu_item.is_available:
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
        cart: Cart, menu_item: MenuItem, options: Sequence[Option], notes: str
    ) -> CartLine | None:
        wanted = {option.pk for option in options}
        for line in cart.lines.filter(menu_item=menu_item, notes=notes).prefetch_related("options"):
            if {selection.option_id for selection in line.options.all()} == wanted:
                return line
        return None

    @staticmethod
    def set_quantity(line: CartLine, quantity: int) -> CartLine:
        line.quantity = quantity
        line.save(update_fields=["quantity", "updated_at"])
        return line

    @staticmethod
    def clear(cart: Cart) -> None:
        cart.lines.all().delete()
