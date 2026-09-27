"""Ouverture d'un établissement à un instant donné.

`OpeningHours.covers()` est déjà testé sans base ; ce qui s'ajoute ici est ce
que la logique pure ne peut pas voir : la conversion dans le fuseau du pays et
le report d'une plage de nuit sur le lendemain. Les deux se corrigent en
production un dimanche soir quand on les oublie.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon

from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, Weekday
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

# Mardi 28 juillet 2026, à midi UTC.
MARDI_MIDI_UTC = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


def restaurant_in(timezone_name: str) -> Restaurant:
    """Établissement complet dans un pays au fuseau choisi."""
    country = Country.objects.create(
        iso_code=timezone_name[:2].upper(),
        name=timezone_name,
        currency="XOF",
        phone_prefix="+228",
        timezone=timezone_name,
    )
    point = Point(1.2255, 6.1319, srid=4326)
    city = City.objects.create(
        country=country,
        name=timezone_name,
        slug=timezone_name.lower().replace("/", "-"),
        centroid=point,
    )
    ring = Polygon(((1.1, 6.0), (1.4, 6.0), (1.4, 6.3), (1.1, 6.3), (1.1, 6.0)), srid=4326)
    zone = DeliveryZone.objects.create(
        city=city,
        name="Zone",
        boundary=MultiPolygon(ring, srid=4326),
        base_fee=Money(500, "XOF"),
        fee_per_km=Money(100, "XOF"),
    )
    return Restaurant.objects.create(
        name=f"El Corazón {timezone_name}",
        slug=f"ec-{city.slug}",
        zone=zone,
        address="Rue du Commerce",
        location=point,
        phone="+22890000000",
    )


class TestFuseauDuPays:
    def test_l_heure_est_lue_dans_le_fuseau_du_restaurant(self) -> None:
        """Midi UTC, c'est 14 h à Paris en été. Un serveur qui comparerait
        l'heure UTC aux horaires locaux fermerait deux heures trop tôt."""
        paris = restaurant_in("Europe/Paris")
        OpeningHours.objects.create(
            restaurant=paris,
            weekday=Weekday.TUESDAY,
            opens_at=dt.time(13, 0),
            closes_at=dt.time(15, 0),
        )

        assert paris.is_open_at(MARDI_MIDI_UTC) is True

    def test_le_meme_instant_ferme_ailleurs(self) -> None:
        """Le même créneau 13 h–15 h, à Lomé (UTC+0), ne couvre pas midi."""
        lome = restaurant_in("Africa/Lome")
        OpeningHours.objects.create(
            restaurant=lome,
            weekday=Weekday.TUESDAY,
            opens_at=dt.time(13, 0),
            closes_at=dt.time(15, 0),
        )

        assert lome.is_open_at(MARDI_MIDI_UTC) is False


class TestPlageDeNuit:
    @pytest.fixture
    def nocturne(self) -> Restaurant:
        """Ouvert le lundi de 22 h à 2 h — donc jusqu'au mardi matin."""
        lome = restaurant_in("Africa/Lome")
        OpeningHours.objects.create(
            restaurant=lome,
            weekday=Weekday.MONDAY,
            opens_at=dt.time(22, 0),
            closes_at=dt.time(2, 0),
        )
        return lome

    @pytest.mark.parametrize(
        ("moment", "attendu"),
        [
            (dt.datetime(2026, 7, 27, 21, 59, tzinfo=dt.UTC), False),  # lundi, avant
            (dt.datetime(2026, 7, 27, 22, 0, tzinfo=dt.UTC), True),  # lundi soir
            (dt.datetime(2026, 7, 27, 23, 59, tzinfo=dt.UTC), True),
            (dt.datetime(2026, 7, 28, 0, 30, tzinfo=dt.UTC), True),  # mardi, après minuit
            (dt.datetime(2026, 7, 28, 1, 59, tzinfo=dt.UTC), True),
            (dt.datetime(2026, 7, 28, 2, 0, tzinfo=dt.UTC), False),  # fermeture exclue
            (dt.datetime(2026, 7, 28, 22, 0, tzinfo=dt.UTC), False),  # mardi soir : pas de plage
        ],
    )
    def test_la_plage_deborde_sur_le_lendemain(
        self, nocturne: Restaurant, moment: dt.datetime, attendu: bool
    ) -> None:
        assert nocturne.is_open_at(moment) is attendu


class TestSeparationDesTroisEtats:
    """`is_open`, `accepts_orders` et `is_active` disent trois choses
    différentes. Les confondre empêcherait l'API d'expliquer au client
    *pourquoi* il ne peut pas commander."""

    @pytest.fixture
    def ouvert(self) -> Restaurant:
        lome = restaurant_in("Africa/Lome")
        OpeningHours.objects.create(
            restaurant=lome,
            weekday=Weekday.TUESDAY,
            opens_at=dt.time(11, 0),
            closes_at=dt.time(23, 0),
        )
        return lome

    def test_un_coup_de_feu_en_cuisine_ne_ferme_pas_l_etablissement(
        self, ouvert: Restaurant
    ) -> None:
        ouvert.accepts_orders = False
        ouvert.save(update_fields=["accepts_orders"])

        assert ouvert.is_open_at(MARDI_MIDI_UTC) is True

    def test_sans_horaire_saisi_l_etablissement_est_ferme(self) -> None:
        """Le défaut est la fermeture : un restaurant dont personne n'a saisi
        les horaires ne doit pas paraître ouvert vingt-quatre heures sur
        vingt-quatre."""
        assert restaurant_in("Africa/Lome").is_open_at(MARDI_MIDI_UTC) is False
