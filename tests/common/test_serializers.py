"""Champs de sérialisation transverses — ADR-007, ADR-009.

Le contrat testé ici est celui que trois applications Flutter liront des
années durant. Une chaîne devenue nombre, ou une latitude passée en longitude,
sont des ruptures silencieuses : personne ne voit d'erreur, les prix et les
positions sont simplement faux.
"""

from __future__ import annotations

import pytest
from rest_framework.exceptions import ValidationError

from common.money import Money
from common.quantities import Quantity
from common.serializers import LocationField, MoneyField, QuantityField


class TestMontantEnSortie:
    def test_forme_du_contrat(self) -> None:
        assert MoneyField().to_representation(Money(1250, "XOF")) == {
            "amount": "1250",
            "currency": "XOF",
        }

    def test_le_montant_sort_en_chaine(self) -> None:
        """`JSON.parse` convertit tout nombre en double : l'exactitude défendue
        jusqu'en base se perdrait au dernier mètre."""
        assert isinstance(MoneyField().to_representation(Money(1250, "EUR"))["amount"], str)

    def test_l_unite_reste_mineure(self) -> None:
        """1250 EUR en unité mineure valent 12,50 € — la division appartient au
        client, qui connaît l'exposant de la devise."""
        assert MoneyField().to_representation(Money(1250, "EUR"))["amount"] == "1250"


class TestMontantEnEntree:
    def test_relit_ce_qu_il_ecrit(self) -> None:
        field = MoneyField()
        assert field.to_internal_value(field.to_representation(Money(1250, "XOF"))) == Money(
            1250, "XOF"
        )

    @pytest.mark.parametrize("payload", ["1250", 1250, {"amount": "1250"}, {"currency": "XOF"}])
    def test_refuse_ce_qui_n_est_pas_un_couple(self, payload: object) -> None:
        with pytest.raises(ValidationError):
            MoneyField().to_internal_value(payload)

    def test_refuse_une_unite_majeure(self) -> None:
        """`12.50` là où on attend `1250` est une erreur d'intégration. La
        convertir en silence facturerait cent fois trop, ou cent fois trop
        peu."""
        with pytest.raises(ValidationError):
            MoneyField().to_internal_value({"amount": "12.50", "currency": "EUR"})

    def test_refuse_une_devise_inconnue(self) -> None:
        with pytest.raises(ValidationError):
            MoneyField().to_internal_value({"amount": "1250", "currency": "ZZZ"})


class TestPosition:
    def test_sortie_nommee(self) -> None:
        from django.contrib.gis.geos import Point

        assert LocationField().to_representation(Point(1.2255, 6.1319, srid=4326)) == {
            "lat": 6.1319,
            "lon": 1.2255,
        }

    def test_l_ordre_postgis_est_inverse_de_l_ordre_humain(self) -> None:
        """PostGIS attend `Point(x=lon, y=lat)`. Le nommage supprime l'erreur
        que produit inévitablement un couple positionnel."""
        point = LocationField().to_internal_value({"lat": 6.1319, "lon": 1.2255})

        assert (point.x, point.y) == (1.2255, 6.1319)
        assert point.srid == 4326

    @pytest.mark.parametrize(
        "payload",
        [
            {"lat": 91, "lon": 0},
            {"lat": 0, "lon": 181},
            {"lat": "nord", "lon": 0},
            {"lat": 6.13},
            [6.13, 1.22],
        ],
    )
    def test_refuse_une_position_impossible(self, payload: object) -> None:
        with pytest.raises(ValidationError):
            LocationField().to_internal_value(payload)


class TestQuantite:
    """`{"amount": "1.5", "unit": "kg"}` — le pendant de `MoneyField` pour la matière."""

    def test_en_entree_l_unite_est_libre_et_la_conversion_exacte(self) -> None:
        assert QuantityField().to_internal_value({"amount": "1.5", "unit": "kg"}) == (
            Quantity.from_unit("1500", "g")
        )

    def test_un_nombre_json_se_lit_par_sa_representation_ecrite(self) -> None:
        """`Decimal(0.1)` vaut 0.1000000000000000055…, que `from_unit` refuserait
        à juste titre ; c'est `0.1`, tel qu'écrit, qui est reçu."""
        assert QuantityField().to_internal_value({"amount": 0.1, "unit": "kg"}) == (
            Quantity.from_unit("100", "g")
        )

    def test_en_sortie_toujours_l_unite_de_reference(self) -> None:
        """Deux clients qui trient ne comparent pas « 1.5 kg » et « 900 g »."""
        sortie = QuantityField().to_representation(Quantity.from_unit("1.5", "kg"))

        assert sortie == {"amount": "1500", "unit": "g", "dimension": "mass"}

    def test_les_millieme_d_unite_sortent_en_decimal(self) -> None:
        sortie = QuantityField().to_representation(Quantity.from_unit("0.5", "unit"))

        assert sortie == {"amount": "0.5", "unit": "unit", "dimension": "count"}

    @pytest.mark.parametrize(
        "donnee",
        [
            "1.5 kg",
            {"amount": "1.5"},
            {"amount": "beaucoup", "unit": "kg"},
            {"amount": "NaN", "unit": "kg"},
            {"amount": "1", "unit": "livre"},
            {"amount": "0.0001", "unit": "g"},
        ],
    )
    def test_ce_qui_n_est_pas_une_quantite_exacte_est_refuse(self, donnee: object) -> None:
        """Une précision plus fine que l'unité de base est **refusée**, pas
        arrondie : l'arrondir à zéro ferait disparaître une ligne de recette
        sans que rien ne le dise."""
        with pytest.raises(ValidationError):
            QuantityField().to_internal_value(donnee)
