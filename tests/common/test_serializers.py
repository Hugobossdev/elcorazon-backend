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
from common.serializers import LocationField, MoneyField


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
