"""Tests du type Money — ADR-007.

L'enjeu n'est pas la couverture de lignes : c'est de verrouiller les propriétés
dont la violation coûte de l'argent réel.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from common.money import CurrencyMismatch, Money, UnknownCurrency, exponent_of


class TestConstruction:
    def test_montant_en_unite_mineure(self) -> None:
        assert Money(1250, "XOF").amount_minor == 1250

    def test_refuse_un_flottant(self) -> None:
        """La raison d'être du type : `0.1 + 0.2 != 0.3` en binaire."""
        with pytest.raises(TypeError, match="entier"):
            Money(12.5, "EUR")  # type: ignore[arg-type]

    def test_refuse_un_booleen(self) -> None:
        """`bool` est un `int` en Python : sans garde, Money(True) passerait."""
        with pytest.raises(TypeError):
            Money(True, "XOF")  # type: ignore[arg-type]

    def test_refuse_une_devise_inconnue(self) -> None:
        with pytest.raises(UnknownCurrency):
            Money(100, "ZZZ")

    def test_le_franc_cfa_n_a_pas_de_decimale(self) -> None:
        assert exponent_of("XOF") == 0
        assert exponent_of("EUR") == 2


class TestConversionMajeure:
    @pytest.mark.parametrize(
        ("major", "currency", "expected_minor"),
        [
            ("12.50", "EUR", 1250),
            ("1250", "XOF", 1250),
            ("0", "XOF", 0),
            (Decimal("99.99"), "USD", 9999),
        ],
    )
    def test_aller(self, major: str | Decimal, currency: str, expected_minor: int) -> None:
        assert Money.from_major(major, currency).amount_minor == expected_minor

    def test_retour(self) -> None:
        assert Money(1250, "EUR").as_major == Decimal("12.50")
        assert Money(1250, "XOF").as_major == Decimal("1250")

    def test_refuse_une_precision_excessive(self) -> None:
        """Un arrondi implicite sur un prix catalogue est une perte de recette
        que personne ne remarque. Mieux vaut refuser."""
        with pytest.raises(ValueError, match="précision"):
            Money.from_major("12.505", "EUR")

    def test_refuse_des_centimes_en_franc_cfa(self) -> None:
        with pytest.raises(ValueError, match="précision"):
            Money.from_major("1250.50", "XOF")

    def test_from_major_refuse_un_flottant(self) -> None:
        with pytest.raises(TypeError):
            Money.from_major(12.5, "EUR")  # type: ignore[arg-type]


class TestArithmetique:
    def test_addition(self) -> None:
        assert Money(1000, "XOF") + Money(250, "XOF") == Money(1250, "XOF")

    def test_soustraction(self) -> None:
        assert Money(1000, "XOF") - Money(250, "XOF") == Money(750, "XOF")

    def test_multiplication_par_une_quantite(self) -> None:
        assert Money(1500, "XOF") * 3 == Money(4500, "XOF")
        assert 3 * Money(1500, "XOF") == Money(4500, "XOF")

    def test_refuse_la_multiplication_par_un_flottant(self) -> None:
        with pytest.raises(TypeError, match="entier"):
            Money(1500, "XOF") * 1.2  # type: ignore[operator]

    @pytest.mark.parametrize("operation", ["add", "sub", "lt", "ge"])
    def test_aucune_conversion_implicite(self, operation: str) -> None:
        """Un montant sans devise n'a pas de sens dès qu'il existe deux pays."""
        xof, eur = Money(1000, "XOF"), Money(1000, "EUR")
        with pytest.raises(CurrencyMismatch):
            getattr(xof, f"__{operation}__")(eur)

    def test_pourcentage(self) -> None:
        assert Money(1000, "XOF").percentage(10) == Money(100, "XOF")
        assert Money(1000, "XOF").percentage("7.5") == Money(75, "XOF")

    def test_pourcentage_arrondit_au_demi_superieur(self) -> None:
        # 333 × 10 % = 33,3 → 33
        assert Money(333, "XOF").percentage(10) == Money(33, "XOF")
        # 335 × 10 % = 33,5 → 34
        assert Money(335, "XOF").percentage(10) == Money(34, "XOF")


class TestRepartition:
    """`allocate` sert au paiement partagé : diviser sans perdre d'unité."""

    def test_partage_egal_sans_perte(self) -> None:
        parts = Money(1000, "XOF").allocate([1, 1, 1])
        assert [p.amount_minor for p in parts] == [334, 333, 333]
        assert sum(p.amount_minor for p in parts) == 1000

    def test_partage_pondere(self) -> None:
        parts = Money(1000, "XOF").allocate([2, 1, 1])
        assert [p.amount_minor for p in parts] == [500, 250, 250]

    @pytest.mark.parametrize("total", [1, 7, 99, 1000, 10_007])
    @pytest.mark.parametrize("count", [2, 3, 7])
    def test_la_somme_des_parts_egale_toujours_le_total(self, total: int, count: int) -> None:
        """Propriété centrale : aucune unité mineure ne disparaît jamais.

        Une division naïve (`total // count` répété) perdrait jusqu'à
        `count - 1` francs par commande — invisible à l'unité, considérable
        à l'échelle.
        """
        parts = Money(total, "XOF").allocate([1] * count)
        assert sum(p.amount_minor for p in parts) == total

    def test_refuse_des_poids_vides_ou_nuls(self) -> None:
        with pytest.raises(ValueError, match="poids"):
            Money(1000, "XOF").allocate([])
        with pytest.raises(ValueError, match="poids"):
            Money(1000, "XOF").allocate([0, 0])


class TestImmutabilite:
    def test_le_montant_est_gele(self) -> None:
        money = Money(1000, "XOF")
        with pytest.raises(AttributeError):
            money.amount_minor = 2000  # type: ignore[misc]

    def test_utilisable_comme_cle(self) -> None:
        assert {Money(1000, "XOF"), Money(1000, "XOF")} == {Money(1000, "XOF")}

    def test_egalite_sensible_a_la_devise(self) -> None:
        assert Money(1000, "XOF") != Money(1000, "EUR")


class TestRepresentation:
    def test_affichage(self) -> None:
        assert str(Money(1250, "EUR")) == "12.50 EUR"
        assert str(Money(1250, "XOF")) == "1250 XOF"
