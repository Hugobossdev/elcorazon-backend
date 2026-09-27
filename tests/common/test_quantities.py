"""Tests du type Quantity — le socle arithmétique de l'inventaire.

Même esprit que `test_money` : on ne cherche pas la couverture de lignes, mais
les propriétés dont la violation fausse un stock. Une quantité fausse ne se
voit pas à la ligne ; elle se voit trois semaines plus tard, quand le stock
théorique et la chambre froide ont divergé et que plus personne ne sait depuis
quand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from common.quantities import (
    Dimension,
    DimensionMismatch,
    Quantity,
    UnknownDimension,
    UnknownUnit,
    dimension_of,
    units_of,
)


class TestConstruction:
    def test_quantite_en_unite_de_base(self) -> None:
        assert Quantity(1500, Dimension.MASS).amount_base == 1500

    def test_refuse_un_flottant(self) -> None:
        """La raison d'être du type, exactement comme pour Money."""
        with pytest.raises(TypeError, match="entier"):
            Quantity(12.5, Dimension.MASS)  # type: ignore[arg-type]

    def test_refuse_un_booleen(self) -> None:
        """`bool` est un `int` : sans garde, Quantity(True) passerait."""
        with pytest.raises(TypeError):
            Quantity(True, Dimension.MASS)  # type: ignore[arg-type]

    def test_refuse_une_dimension_inconnue(self) -> None:
        with pytest.raises(UnknownDimension):
            Quantity(100, "longueur")

    def test_le_negatif_est_permis_au_niveau_du_type(self) -> None:
        """Un mouvement sortant est une quantité négative.

        L'interdire ici obligerait chaque appelant à porter le signe à côté du
        nombre, ce qui est la façon habituelle de le perdre. C'est la colonne
        de stock qui refuse le négatif, par contrainte.
        """
        assert Quantity(-500, Dimension.MASS).is_negative


class TestConversionDepuisUneUnite:
    @pytest.mark.parametrize(
        ("valeur", "unite", "base_attendue"),
        [
            ("1", "mg", 1),
            ("1", "g", 1_000),
            ("1", "kg", 1_000_000),
            ("1.5", "kg", 1_500_000),
            ("20", "g", 20_000),
            ("1", "ml", 1_000),
            ("1", "cl", 10_000),
            ("1", "l", 1_000_000),
            ("0.5", "l", 500_000),
            ("1", "unit", 1_000),
            ("0.5", "unit", 500),
            ("0", "g", 0),
        ],
    )
    def test_conversions_exactes(self, valeur: str, unite: str, base_attendue: int) -> None:
        assert Quantity.from_unit(valeur, unite).amount_base == base_attendue

    def test_refuse_un_flottant(self) -> None:
        with pytest.raises(TypeError, match="flottants"):
            Quantity.from_unit(1.5, "kg")  # type: ignore[arg-type]

    def test_refuse_une_unite_inconnue(self) -> None:
        with pytest.raises(UnknownUnit, match="once"):
            Quantity.from_unit("1", "once")

    def test_refuse_une_precision_inferieure_a_l_unite_de_base(self) -> None:
        """Un dix-millième de gramme est une faute de frappe, pas une pesée.

        L'accepter en l'arrondissant à zéro ferait disparaître une ligne de
        recette sans que rien ne le signale.
        """
        with pytest.raises(ValueError, match="précision"):
            Quantity.from_unit("0.0001", "g")

    def test_la_dimension_est_deduite_de_l_unite(self) -> None:
        assert Quantity.from_unit("1", "kg").dimension == Dimension.MASS
        assert Quantity.from_unit("1", "l").dimension == Dimension.VOLUME
        assert Quantity.from_unit("1", "unit").dimension == Dimension.COUNT

    def test_un_demi_pain_est_representable(self) -> None:
        """Le cas qui justifie les millièmes sur le dénombrement.

        Sans fraction, une demi-portion devrait s'exprimer en grammes —
        c'est-à-dire peser un pain, c'est-à-dire ne pas répondre à la question.
        """
        demi = Quantity.from_unit("0.5", "unit")
        assert demi + demi == Quantity.from_unit("1", "unit")


class TestRelecture:
    def test_aller_retour_est_l_identite(self) -> None:
        for valeur, unite in [("1.5", "kg"), ("20", "g"), ("0.25", "l"), ("3", "unit")]:
            assert Quantity.from_unit(valeur, unite).as_unit(unite) == Decimal(valeur)

    def test_relecture_dans_une_autre_unite_de_la_meme_dimension(self) -> None:
        assert Quantity.from_unit("1.5", "kg").as_unit("g") == Decimal("1500")
        assert Quantity.from_unit("2", "l").as_unit("ml") == Decimal("2000")

    def test_relecture_dans_une_autre_dimension_est_refusee(self) -> None:
        with pytest.raises(DimensionMismatch):
            Quantity.from_unit("1", "kg").as_unit("l")

    def test_unite_de_reference_par_dimension(self) -> None:
        assert Quantity.zero(Dimension.MASS).reference_unit == "g"
        assert Quantity.zero(Dimension.VOLUME).reference_unit == "ml"
        assert Quantity.zero(Dimension.COUNT).reference_unit == "unit"

    def test_affichage_lisible(self) -> None:
        assert Quantity.from_unit("1.5", "kg").humanise("kg") == "1.5 kg"
        assert str(Quantity.from_unit("20", "g")) == "20 g"


class TestArithmetique:
    def test_addition(self) -> None:
        assert Quantity.from_unit("20", "g") + Quantity.from_unit("30", "g") == Quantity.from_unit(
            "50", "g"
        )

    def test_soustraction(self) -> None:
        assert Quantity.from_unit("1", "kg") - Quantity.from_unit("200", "g") == Quantity.from_unit(
            "800", "g"
        )

    def test_les_unites_se_melangent_sans_perte(self) -> None:
        """Le point central : g et kg sont le même entier, donc additionnables."""
        total = Quantity.from_unit("1", "kg") + Quantity.from_unit("500", "g")
        assert total.as_unit("kg") == Decimal("1.5")

    @pytest.mark.parametrize(
        ("gauche", "droite"),
        [
            (("1", "kg"), ("1", "l")),
            (("1", "g"), ("1", "unit")),
            (("1", "ml"), ("1", "unit")),
        ],
    )
    def test_additionner_deux_dimensions_est_refuse(
        self, gauche: tuple[str, str], droite: tuple[str, str]
    ) -> None:
        """`20 g + 30 ml` doit lever, pas rendre 50.

        C'est le défaut que la dimension portée par la valeur rend impossible.
        """
        with pytest.raises(DimensionMismatch):
            Quantity.from_unit(*gauche) + Quantity.from_unit(*droite)

    def test_multiplication_par_un_nombre_de_portions(self) -> None:
        assert Quantity.from_unit("20", "g") * 3 == Quantity.from_unit("60", "g")
        assert 3 * Quantity.from_unit("20", "g") == Quantity.from_unit("60", "g")

    def test_multiplication_par_un_flottant_est_refusee(self) -> None:
        """On prépare un plat ou on n'en prépare pas — jamais 1,5."""
        with pytest.raises(TypeError, match="entier"):
            Quantity.from_unit("20", "g") * 1.5  # type: ignore[operator]

    def test_negation(self) -> None:
        assert -Quantity.from_unit("20", "g") == Quantity.from_unit("-20", "g")

    def test_cumul_de_mille_pesees_ne_derive_pas(self) -> None:
        """La propriété que ce type existe pour garantir.

        En flottants, additionner mille fois 0,1 g ne donne pas 100 g. Ici,
        l'égalité est exacte — et c'est ce qui permet de réconcilier un stock
        après un service chargé.
        """
        total = Quantity.zero(Dimension.MASS)
        for _ in range(1000):
            total += Quantity.from_unit("0.1", "g")
        assert total == Quantity.from_unit("100", "g")


class TestComparaisons:
    def test_ordre(self) -> None:
        assert Quantity.from_unit("1", "kg") > Quantity.from_unit("999", "g")
        assert Quantity.from_unit("1", "kg") >= Quantity.from_unit("1000", "g")
        assert Quantity.from_unit("1", "g") < Quantity.from_unit("1", "kg")
        assert Quantity.from_unit("1", "g") <= Quantity.from_unit("1", "g")

    def test_comparer_deux_dimensions_est_refuse(self) -> None:
        with pytest.raises(DimensionMismatch):
            _ = Quantity.from_unit("1", "kg") < Quantity.from_unit("1", "l")

    def test_predicats(self) -> None:
        assert Quantity.zero(Dimension.MASS).is_zero
        assert Quantity.from_unit("1", "g").is_positive
        assert Quantity.from_unit("-1", "g").is_negative


class TestTableDesUnites:
    def test_dimension_d_une_unite(self) -> None:
        assert dimension_of("kg") == Dimension.MASS
        assert dimension_of("cl") == Dimension.VOLUME
        assert dimension_of("unit") == Dimension.COUNT

    def test_dimension_d_une_unite_inconnue(self) -> None:
        with pytest.raises(UnknownUnit):
            dimension_of("lb")

    def test_unites_d_une_dimension_de_la_plus_petite_a_la_plus_grande(self) -> None:
        assert units_of(Dimension.MASS) == ["mg", "g", "kg"]
        assert units_of(Dimension.VOLUME) == ["ml", "cl", "l"]
        assert units_of(Dimension.COUNT) == ["unit"]

    def test_unites_d_une_dimension_inconnue(self) -> None:
        with pytest.raises(UnknownDimension):
            units_of("longueur")

    def test_toute_unite_connue_a_une_dimension(self) -> None:
        """Les deux tables ne peuvent pas diverger sans qu'un test le dise."""
        from common.quantities import DIMENSION_OF_UNIT, UNITS_IN_BASE

        assert set(UNITS_IN_BASE) == set(DIMENSION_OF_UNIT)

    def test_toute_dimension_a_une_unite_de_reference_valant_mille(self) -> None:
        """L'invariant qui rend l'exposant uniforme, et donc omissible."""
        from common.quantities import REFERENCE_UNIT, UNITS_IN_BASE

        assert set(REFERENCE_UNIT) == set(Dimension.ALL)
        for dimension, unite in REFERENCE_UNIT.items():
            assert dimension_of(unite) == dimension
            assert UNITS_IN_BASE[unite] == 1_000


class TestCout:
    """Le coût se tient au kilogramme, au litre, à l'unité — jamais au gramme.

    En entiers d'une devise sans décimales, le coût du gramme d'un oignon à
    500 F le kilo vaudrait 0,5 F : arrondi à 0 ou à 1, cent pour cent d'erreur
    sur la donnée même qui doit rendre la marge calculable.
    """

    def test_toute_dimension_a_une_unite_de_cout_de_sa_dimension(self) -> None:
        from common.quantities import COST_UNIT

        assert set(COST_UNIT) == set(Dimension.ALL)
        for dimension, unite in COST_UNIT.items():
            assert dimension_of(unite) == dimension

    def test_vingt_grammes_a_cinq_cents_francs_le_kilo(self) -> None:
        from common.quantities import value_minor

        assert value_minor(Quantity.from_unit("20", "g"), 500) == 10

    def test_l_unique_arrondi_porte_sur_le_resultat(self) -> None:
        """1 g à 500 F/kg vaut 0,5 F : arrondi au demi supérieur, une fois."""
        from common.quantities import value_minor

        assert value_minor(Quantity.from_unit("1", "g"), 500) == 1
        assert value_minor(Quantity.from_unit("-1", "g"), 500) == -1

    def test_un_volume_se_valorise_au_litre_et_un_denombrement_a_l_unite(self) -> None:
        from common.quantities import value_minor

        assert value_minor(Quantity.from_unit("33", "cl"), 1_000) == 330
        assert value_minor(Quantity.from_unit("30", "unit"), 75) == 2_250
        assert Quantity.from_unit("30", "unit").cost_unit == "unit"
