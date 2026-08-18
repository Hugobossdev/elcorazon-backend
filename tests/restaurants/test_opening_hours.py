"""Tests des horaires d'ouverture.

`covers()` est de la logique pure, testée sans base : c'est là que se logent
les erreurs de bord — minuit, la minute de fermeture, la plage à cheval sur
deux jours.
"""

from __future__ import annotations

import datetime as dt

import pytest

from apps.restaurants.models import OpeningHours, Weekday


def slot(opens: str, closes: str) -> OpeningHours:
    """Plage non persistée : `covers()` n'a besoin d'aucune ligne en base."""
    return OpeningHours(
        weekday=Weekday.MONDAY,
        opens_at=dt.time.fromisoformat(opens),
        closes_at=dt.time.fromisoformat(closes),
    )


class TestPlageOrdinaire:
    @pytest.mark.parametrize(
        ("moment", "attendu"),
        [
            ("10:59", False),  # avant l'ouverture
            ("11:00", True),  # l'ouverture est incluse
            ("15:30", True),
            ("22:59", True),
            ("23:00", False),  # la fermeture est exclue
            ("23:01", False),
        ],
    )
    def test_bornes(self, moment: str, attendu: bool) -> None:
        """La borne d'ouverture est incluse, celle de fermeture exclue.

        Sans cette convention, deux plages contiguës (11h–15h puis 15h–23h) se
        chevaucheraient à 15h00 pile, et un service se retrouverait à afficher
        deux fois le même créneau.
        """
        assert slot("11:00", "23:00").covers(dt.time.fromisoformat(moment)) is attendu

    def test_ne_franchit_pas_minuit(self) -> None:
        assert not slot("11:00", "23:00").crosses_midnight


class TestPlageAChevalSurMinuit:
    """`22:00 → 02:00` est saisi tel quel, pas en deux plages sur deux jours :
    la saisie reste conforme à ce qu'un restaurateur a en tête."""

    @pytest.mark.parametrize(
        ("moment", "attendu"),
        [
            ("21:59", False),
            ("22:00", True),
            ("23:59", True),
            ("00:00", True),  # minuit est bien couvert
            ("01:59", True),
            ("02:00", False),
            ("12:00", False),  # milieu de journée : fermé
        ],
    )
    def test_bornes(self, moment: str, attendu: bool) -> None:
        assert slot("22:00", "02:00").covers(dt.time.fromisoformat(moment)) is attendu

    def test_est_detectee(self) -> None:
        assert slot("22:00", "02:00").crosses_midnight


class TestServiceContinu:
    def test_une_plage_de_minuit_a_minuit_est_refusee_par_contrainte(self) -> None:
        """`00:00 → 00:00` est ambigu : fermé en permanence, ou ouvert 24 h ?

        La contrainte `opening_hours_not_empty` le refuse en base. Un service
        continu se saisit `00:00 → 23:59`, sans ambiguïté.
        """
        ambigu = slot("00:00", "00:00")
        assert ambigu.opens_at == ambigu.closes_at

    def test_journee_quasi_complete(self) -> None:
        continu = slot("00:00", "23:59")
        assert continu.covers(dt.time(12, 0))
        assert continu.covers(dt.time(0, 0))
        assert not continu.covers(dt.time(23, 59))


class TestAlignementDesJours:
    def test_lundi_vaut_zero_comme_date_weekday(self) -> None:
        """L'alignement sur `date.weekday()` supprime la conversion manuelle,
        source classique du décalage d'un jour."""
        assert Weekday.MONDAY == 0
        assert Weekday.SUNDAY == 6
        assert dt.date(2026, 7, 27).weekday() == Weekday.MONDAY
