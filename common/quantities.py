"""Quantités de matière — le pendant de `common.money` pour l'inventaire.

## Pourquoi ce module existe, et pourquoi il ressemble tant à `money`

Une recette dit « 20 g de sauce » ; le stock est tenu en kilogrammes ; la
réception arrive en litres. Écrire ces conversions à la main, au point d'appel,
c'est reproduire exactement le défaut que l'ADR-007 a fermé pour les montants :

    0.1 + 0.2 != 0.3

Sur une pesée isolée, l'écart est invisible. Sur la consommation cumulée d'un
ingrédient au fil d'un service, il devient un stock théorique qui ne
correspond plus à la chambre froide — et personne ne sait à quel moment la
dérive a commencé, parce qu'aucune ligne n'est fausse individuellement.

La réponse est la même que pour l'argent, et elle doit l'être : **un entier
dans une unité de base, jamais un flottant**, et la conversion faite aux
frontières — saisie et affichage — jamais au milieu d'un calcul.

## Les trois dimensions, et leur unité de référence

| Dimension | Référence | Unité de base stockée |
|---|---|---|
| `mass`   | le gramme      | le milligramme  |
| `volume` | le millilitre  | le microlitre   |
| `count`  | l'unité        | le millième d'unité |

Le facteur est **mille pour les trois**, et ce n'est pas un hasard : c'est
l'exposant fixe qui remplace la table par devise de `money`. Trois décimales
suffisent partout en cuisine — on ne pèse pas au dixième de milligramme — et
l'uniformité évite la question « combien de décimales a cette dimension ? » à
chaque conversion.

`count` porte des millièmes pour une raison précise : une recette peut demander
**un demi pain**. Sans fraction, il faudrait l'exprimer en grammes, c'est-à-dire
peser un pain, c'est-à-dire ne pas répondre à la question posée.

## Ce que ce module refuse

Additionner une masse et un volume. Il n'existe pas de conversion implicite —
convertir des litres en kilogrammes demande une densité, qui appartient à
l'ingrédient et non au nombre. Une telle opération lève `DimensionMismatch`,
au même titre que `CurrencyMismatch` pour deux devises.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "COST_UNIT",
    "DIMENSION_OF_UNIT",
    "REFERENCE_UNIT",
    "UNITS_IN_BASE",
    "Dimension",
    "DimensionMismatch",
    "Quantity",
    "UnknownUnit",
    "dimension_of",
    "units_of",
    "value_minor",
]


class Dimension:
    """Les trois grandeurs qu'une cuisine manipule.

    Une classe de constantes plutôt qu'une énumération : la valeur voyage en
    colonne `CHAR(6)` et dans le contrat d'API, et une chaîne s'y compare sans
    conversion. `models.TextChoices` vit du côté des modèles, où Django en a
    besoin pour ses `choices` ; `common` ne doit pas imposer Django à ce qui
    n'est que de l'arithmétique.
    """

    MASS: Final = "mass"
    VOLUME: Final = "volume"
    COUNT: Final = "count"

    ALL: Final = (MASS, VOLUME, COUNT)


#: Unité dont l'unité de base est le millième. C'est elle qu'on affiche par
#: défaut, et celle dans laquelle une recette se lit le plus naturellement.
REFERENCE_UNIT: Final[dict[str, str]] = {
    Dimension.MASS: "g",
    Dimension.VOLUME: "ml",
    Dimension.COUNT: "unit",
}

#: Combien d'unités de base vaut une unité nommée.
#:
#: Les facteurs sont exacts et entiers — aucun n'est approché, ce qui est la
#: condition pour que la conversion aller-retour soit l'identité. C'est aussi
#: pourquoi l'once et la livre n'y figurent pas : elles ne tombent pas juste en
#: milligrammes, et les admettre introduirait l'arrondi que ce module existe
#: pour éviter. Le jour où un marché anglo-saxon l'exigera, ce sera une
#: conversion explicite à la saisie, pas une entrée de plus dans cette table.
UNITS_IN_BASE: Final[dict[str, int]] = {
    # masse — base : le milligramme
    "mg": 1,
    "g": 1_000,
    "kg": 1_000_000,
    # volume — base : le microlitre
    "ml": 1_000,
    "cl": 10_000,
    "l": 1_000_000,
    # dénombrement — base : le millième d'unité
    "unit": 1_000,
}

#: L'unité **dans laquelle un coût se tient** : le kilogramme, le litre, l'unité.
#:
#: Pas l'unité de référence, et c'est la correction d'un défaut réel. Un coût est
#: un montant en **entiers d'unités mineures** (`common.money`), et le franc CFA
#: n'a pas de décimales. Tenu au gramme, le coût d'un oignon à 500 F le kilo vaut
#: 0,5 F — arrondi à 0 ou à 1, soit 100 % d'erreur, sur la donnée même qui doit
#: rendre la marge calculable. Tenu au kilogramme, il vaut 500, exactement.
#:
#: L'unité reste la bonne pour les dénombrements : un œuf coûte 75 F, pas 0,075 F
#: le millième d'œuf.
COST_UNIT: Final[dict[str, str]] = {
    Dimension.MASS: "kg",
    Dimension.VOLUME: "l",
    Dimension.COUNT: "unit",
}

DIMENSION_OF_UNIT: Final[dict[str, str]] = {
    "mg": Dimension.MASS,
    "g": Dimension.MASS,
    "kg": Dimension.MASS,
    "ml": Dimension.VOLUME,
    "cl": Dimension.VOLUME,
    "l": Dimension.VOLUME,
    "unit": Dimension.COUNT,
}


class UnknownUnit(ValueError):
    """Unité absente de la table des conversions."""


class UnknownDimension(ValueError):
    """Dimension inconnue."""


class DimensionMismatch(ValueError):
    """Opération entre deux quantités de dimensions différentes."""

    def __init__(self, left: str, right: str) -> None:
        super().__init__(
            f"Opération impossible entre une quantité en {left} et une en {right} : "
            "convertir un volume en masse demande une densité, qui appartient à "
            "l'ingrédient et non au nombre."
        )


def dimension_of(unit: str) -> str:
    """Dimension d'une unité nommée."""
    try:
        return DIMENSION_OF_UNIT[unit]
    except KeyError:
        raise UnknownUnit(
            f"Unité inconnue : {unit!r}. Connues : {', '.join(sorted(UNITS_IN_BASE))}."
        ) from None


def units_of(dimension: str) -> list[str]:
    """Unités saisissables pour cette dimension, de la plus petite à la plus grande."""
    if dimension not in Dimension.ALL:
        raise UnknownDimension(f"Dimension inconnue : {dimension!r}.")
    return sorted(
        (u for u, d in DIMENSION_OF_UNIT.items() if d == dimension),
        key=lambda u: UNITS_IN_BASE[u],
    )


@dataclass(frozen=True, slots=True, order=False)
class Quantity:
    """Quantité immuable, exprimée dans l'unité de base de sa dimension.

    >>> Quantity.from_unit("1.5", "kg")
    Quantity(amount_base=1500000, dimension='mass')
    >>> Quantity.from_unit("20", "g") + Quantity.from_unit("30", "g")
    Quantity(amount_base=50000, dimension='mass')

    Le négatif est **autorisé au niveau du type** : un mouvement de stock sortant
    est une quantité négative, et l'interdire ici obligerait chaque appelant à
    porter le signe à côté du nombre — ce qui est la façon habituelle de le
    perdre. C'est la colonne de stock qui refuse le négatif, par contrainte
    `CHECK`, parce que c'est elle qui décrit ce qui est physiquement présent.
    """

    amount_base: int
    dimension: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_base, int) or isinstance(self.amount_base, bool):
            raise TypeError(
                f"amount_base doit être un entier, reçu {type(self.amount_base).__name__}. "
                "Un flottant ne peut pas représenter une quantité exactement."
            )
        if self.dimension not in Dimension.ALL:
            raise UnknownDimension(f"Dimension inconnue : {self.dimension!r}.")

    # ------------------------------------------------------------ fabriques

    @classmethod
    def zero(cls, dimension: str) -> Quantity:
        return cls(0, dimension)

    @classmethod
    def from_unit(cls, value: Decimal | str | int, unit: str) -> Quantity:
        """Construit depuis une unité nommée : `from_unit("1.5", "kg")`.

        Refuse une précision inférieure à l'unité de base plutôt que d'arrondir
        en silence. `from_unit("0.0001", "g")` est un dix-millième de gramme :
        ce n'est pas une pesée, c'est une faute de frappe, et l'accepter en
        l'arrondissant à zéro ferait disparaître une ligne de recette sans que
        rien ne le dise.
        """
        if isinstance(value, float):
            raise TypeError(
                "from_unit refuse les flottants ; passer une chaîne ou un Decimal. "
                "0.1 + 0.2 != 0.3, et un stock ne se répare pas après coup."
            )
        facteur = UNITS_IN_BASE.get(unit)
        if facteur is None:
            raise UnknownUnit(
                f"Unité inconnue : {unit!r}. Connues : {', '.join(sorted(UNITS_IN_BASE))}."
            )

        exact = Decimal(value) * facteur
        arrondi = exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if exact != arrondi:
            raise ValueError(
                f"{value} {unit} a une précision supérieure à l'unité de base de "
                f"{DIMENSION_OF_UNIT[unit]} ; la plus petite quantité représentable "
                f"est {Decimal(1) / facteur} {unit}."
            )
        return cls(int(arrondi), DIMENSION_OF_UNIT[unit])

    # ------------------------------------------------------------ lectures

    def as_unit(self, unit: str) -> Decimal:
        """Valeur dans une unité nommée, exacte.

        Lève si l'unité n'est pas de la même dimension : demander une masse en
        litres est une erreur d'appel, pas une conversion à deviner.
        """
        cible = dimension_of(unit)
        if cible != self.dimension:
            raise DimensionMismatch(self.dimension, cible)
        return Decimal(self.amount_base) / UNITS_IN_BASE[unit]

    @property
    def reference_unit(self) -> str:
        """Unité d'affichage par défaut de la dimension."""
        return REFERENCE_UNIT[self.dimension]

    @property
    def as_reference(self) -> Decimal:
        """Valeur dans l'unité de référence — grammes, millilitres ou unités."""
        return self.as_unit(self.reference_unit)

    def humanise(self, unit: str | None = None) -> str:
        """Rend « 1.5 kg », pour un écran ou un message d'erreur."""
        unit = unit or self.reference_unit
        return f"{self.as_unit(unit).normalize():f} {unit}"

    def __str__(self) -> str:
        return self.humanise()

    # ------------------------------------------------------------ arithmétique

    def _check(self, other: Quantity) -> None:
        if self.dimension != other.dimension:
            raise DimensionMismatch(self.dimension, other.dimension)

    def __add__(self, other: Quantity) -> Quantity:
        self._check(other)
        return Quantity(self.amount_base + other.amount_base, self.dimension)

    def __sub__(self, other: Quantity) -> Quantity:
        self._check(other)
        return Quantity(self.amount_base - other.amount_base, self.dimension)

    def __mul__(self, factor: int) -> Quantity:
        """Multiplication par une quantité entière — les portions d'une ligne.

        Le facteur est entier parce que c'est toujours un **nombre de
        portions** : trois burgers, deux parts. Multiplier une recette par 1,5
        n'a pas de sens en production — on prépare un plat ou on n'en prépare
        pas — et l'autoriser rouvrirait la porte à l'arrondi.
        """
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError(
                "Une quantité ne se multiplie que par un entier : c'est un "
                "nombre de portions, jamais une fraction."
            )
        return Quantity(self.amount_base * factor, self.dimension)

    __rmul__ = __mul__

    def __neg__(self) -> Quantity:
        return Quantity(-self.amount_base, self.dimension)

    # ------------------------------------------------------------ comparaisons

    def __lt__(self, other: Quantity) -> bool:
        self._check(other)
        return self.amount_base < other.amount_base

    def __le__(self, other: Quantity) -> bool:
        self._check(other)
        return self.amount_base <= other.amount_base

    def __gt__(self, other: Quantity) -> bool:
        self._check(other)
        return self.amount_base > other.amount_base

    def __ge__(self, other: Quantity) -> bool:
        self._check(other)
        return self.amount_base >= other.amount_base

    @property
    def cost_unit(self) -> str:
        """Unité dans laquelle se tient le coût de cette quantité — voir `COST_UNIT`."""
        return COST_UNIT[self.dimension]

    @property
    def is_zero(self) -> bool:
        return self.amount_base == 0

    @property
    def is_positive(self) -> bool:
        return self.amount_base > 0

    @property
    def is_negative(self) -> bool:
        return self.amount_base < 0


def value_minor(quantity: Quantity, cost_minor: int) -> int:
    """Valeur d'une quantité, en unité mineure, pour un coût tenu par `COST_UNIT`.

    `value_minor(20 g, 500)` — vingt grammes d'un oignon à 500 F le kilo — vaut
    10. Le calcul est fait en entiers jusqu'au bout, et l'unique arrondi, au
    demi supérieur, porte sur le résultat : c'est la règle de `common.money`,
    appliquée une fois, jamais au milieu d'une somme.

    La valeur suit le signe de la quantité : une perte vaut une valeur négative,
    et c'est à l'appelant d'en prendre la valeur absolue s'il compare à un
    plafond.
    """
    diviseur = UNITS_IN_BASE[COST_UNIT[quantity.dimension]]
    exact = Decimal(quantity.amount_base) * cost_minor / diviseur
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))
