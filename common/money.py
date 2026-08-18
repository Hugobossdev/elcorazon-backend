"""Montants monétaires.

Voir ADR-007. Deux règles, pour deux raisons distinctes :

1. **Un montant est un entier en unité mineure, jamais un flottant.**  Le
   flottant binaire ne représente pas exactement les décimaux (`0.1 + 0.2 !=
   0.3`).  Sur un total de commande l'écart est invisible ; sur un cumul de
   commissions livreur en fin de mois, il devient un litige.

2. **Un montant porte sa devise, et cette devise est figée sur la
   transaction.**  Une commande passée en XOF reste lisible en XOF pour
   toujours, même si le restaurant change de pays.  Un historique comptable ne
   se recalcule jamais.

Toute opération entre devises différentes lève une exception : il n'existe pas
de conversion implicite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = ["CurrencyMismatch", "Money", "UnknownCurrency", "exponent_of"]


# Nombre de décimales par devise (ISO-4217).  Le franc CFA n'en a aucune : un
# montant XOF portant des centimes est une erreur de saisie, pas un arrondi.
CURRENCY_EXPONENTS: Final[dict[str, int]] = {
    "XOF": 0,  # franc CFA (UEMOA) — devise de référence du produit
    "XAF": 0,  # franc CFA (CEMAC)
    "GNF": 0,  # franc guinéen
    "EUR": 2,
    "USD": 2,
    "GHS": 2,  # cedi ghanéen
    "NGN": 2,  # naira nigérian
}


class UnknownCurrency(ValueError):
    """Devise absente de la table des exposants."""


class CurrencyMismatch(ValueError):
    """Opération entre deux montants de devises différentes."""

    def __init__(self, left: str, right: str) -> None:
        super().__init__(
            f"Opération impossible entre {left} et {right} : "
            "aucune conversion implicite n'est autorisée."
        )


def exponent_of(currency: str) -> int:
    """Nombre de décimales d'une devise."""
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise UnknownCurrency(f"Devise inconnue : {currency!r}") from None


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """Montant immuable, exprimé en unité mineure.

    >>> Money(1250, "XOF")        # 1 250 F CFA
    >>> Money(1250, "EUR")        # 12,50 €
    """

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise TypeError(
                f"amount_minor doit être un entier, reçu {type(self.amount_minor).__name__}. "
                "Un flottant ne peut pas représenter un montant exactement."
            )
        exponent_of(self.currency)  # valide la devise

    # ------------------------------------------------------------ fabriques

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @classmethod
    def from_major(cls, amount: Decimal | str | int, currency: str) -> Money:
        """Construit depuis une unité majeure : `from_major("12.50", "EUR")`.

        Refuse une précision supérieure à celle de la devise plutôt que
        d'arrondir en silence — un arrondi implicite sur un prix catalogue est
        une perte de recette que personne ne remarque.
        """
        if isinstance(amount, float):
            raise TypeError("from_major refuse les flottants ; passer une chaîne ou un Decimal.")
        value = Decimal(amount)
        scaled = value.scaleb(exponent_of(currency))
        rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if scaled != rounded:
            raise ValueError(
                f"{value} a une précision supérieure à celle de {currency} "
                f"({exponent_of(currency)} décimale(s))."
            )
        return cls(int(rounded), currency)

    # ------------------------------------------------------------ lectures

    @property
    def as_major(self) -> Decimal:
        """Valeur en unité majeure, exacte."""
        return Decimal(self.amount_minor).scaleb(-exponent_of(self.currency))

    def __str__(self) -> str:
        return f"{self.as_major} {self.currency}"

    # ------------------------------------------------------------ arithmétique

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __mul__(self, factor: int) -> Money:
        """Multiplication par une quantité entière (une ligne de commande)."""
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError(
                "Un montant ne se multiplie que par un entier. Pour appliquer "
                "un pourcentage, utiliser percentage()."
            )
        return Money(self.amount_minor * factor, self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def percentage(self, percent: Decimal | str | int) -> Money:
        """Pourcentage du montant, arrondi au demi supérieur.

        Utilisé pour les remises et les commissions.  L'arrondi est explicite
        et unique, ce qui rend le calcul reproductible côté facturation.
        """
        if isinstance(percent, float):
            raise TypeError("percentage refuse les flottants.")
        raw = Decimal(self.amount_minor) * Decimal(percent) / Decimal(100)
        return Money(int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP)), self.currency)

    def allocate(self, weights: list[int]) -> list[Money]:
        """Répartit le montant selon des poids, **sans perdre d'unité mineure**.

        Indispensable au paiement partagé : diviser 1 000 XOF en trois donne
        333, 333 et 334 — jamais 333 × 3, qui perdrait 1 F à chaque commande.
        Le reste est distribué aux premiers bénéficiaires.
        """
        if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
            raise ValueError("Les poids doivent être positifs et de somme non nulle.")

        total_weight = sum(weights)
        shares = [self.amount_minor * w // total_weight for w in weights]
        remainder = self.amount_minor - sum(shares)

        for i in range(remainder):
            shares[i % len(shares)] += 1

        return [Money(s, self.currency) for s in shares]

    # ------------------------------------------------------------ comparaisons

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor >= other.amount_minor

    @property
    def is_zero(self) -> bool:
        return self.amount_minor == 0

    @property
    def is_positive(self) -> bool:
        return self.amount_minor > 0
