"""Tests géospatiaux — ADR-006.

Ils exigent PostGIS : c'est le moteur qui répond, pas Python. Une couverture de
zone testée avec une approximation en Python ne prouverait rien sur ce que fera
la base en production.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon

from apps.geography.models import City, Country, DeliveryZone
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

# Lomé, Togo — le marché réel du produit.
LOME = Point(1.2255, 6.1319, srid=4326)


@pytest.fixture
def togo() -> Country:
    return Country.objects.create(
        iso_code="TG",
        name="Togo",
        currency="XOF",
        phone_prefix="+228",
        timezone="Africa/Lome",
    )


@pytest.fixture
def lome(togo: Country) -> City:
    return City.objects.create(country=togo, name="Lomé", slug="lome", centroid=LOME)


@pytest.fixture
def zone_centre(lome: City) -> DeliveryZone:
    """Un carré d'environ 4 km de côté autour du centre de Lomé."""
    square = Polygon(
        ((1.20, 6.11), (1.25, 6.11), (1.25, 6.15), (1.20, 6.15), (1.20, 6.11)),
        srid=4326,
    )
    return DeliveryZone.objects.create(
        city=lome,
        name="Centre",
        boundary=MultiPolygon(square, srid=4326),
        base_fee=Money(500, "XOF"),
        fee_per_km=Money(100, "XOF"),
    )


class TestHierarchie:
    def test_la_devise_vient_du_pays(self, zone_centre: DeliveryZone) -> None:
        """Deux établissements d'un même pays ne peuvent pas facturer dans deux
        devises différentes : la devise est une propriété du marché."""
        assert zone_centre.city.country.currency == "XOF"

    def test_le_slug_de_ville_est_unique_par_pays(self, togo: Country) -> None:
        from django.db.utils import IntegrityError

        City.objects.create(country=togo, name="Kara", slug="kara", centroid=LOME)
        with pytest.raises(IntegrityError):
            City.objects.create(country=togo, name="Kara bis", slug="kara", centroid=LOME)


class TestCouvertureGeospatiale:
    def test_un_point_interieur_est_couvert(self, zone_centre: DeliveryZone) -> None:
        assert DeliveryZone.objects.filter(pk=zone_centre.pk, boundary__covers=LOME).exists()

    def test_un_point_exterieur_ne_l_est_pas(self, zone_centre: DeliveryZone) -> None:
        agoe = Point(1.35, 6.22, srid=4326)  # banlieue nord, hors du carré
        assert not DeliveryZone.objects.filter(pk=zone_centre.pk, boundary__covers=agoe).exists()

    def test_resolution_de_la_zone_depuis_un_point(self, zone_centre: DeliveryZone) -> None:
        """La requête réellement exécutée au passage de commande."""
        found = DeliveryZone.objects.filter(boundary__covers=LOME, is_active=True).first()
        assert found == zone_centre

    def test_une_zone_peut_etre_discontinue(self, lome: City) -> None:
        """Une zone réelle est fréquemment coupée en deux par un fleuve ou une
        voie ferrée, d'où `MultiPolygon` plutôt que `Polygon`."""
        ouest = Polygon(
            ((1.10, 6.11), (1.15, 6.11), (1.15, 6.15), (1.10, 6.15), (1.10, 6.11)), srid=4326
        )
        est = Polygon(
            ((1.30, 6.11), (1.35, 6.11), (1.35, 6.15), (1.30, 6.15), (1.30, 6.11)), srid=4326
        )
        zone = DeliveryZone.objects.create(
            city=lome,
            name="Périphérie",
            boundary=MultiPolygon(ouest, est, srid=4326),
            base_fee=Money(800, "XOF"),
            fee_per_km=Money(120, "XOF"),
        )

        assert DeliveryZone.objects.filter(
            pk=zone.pk, boundary__covers=Point(1.12, 6.13, srid=4326)
        ).exists()
        assert DeliveryZone.objects.filter(
            pk=zone.pk, boundary__covers=Point(1.32, 6.13, srid=4326)
        ).exists()
        # Le corridor entre les deux morceaux n'est pas desservi.
        assert not DeliveryZone.objects.filter(
            pk=zone.pk, boundary__covers=Point(1.22, 6.13, srid=4326)
        ).exists()


class TestDistances:
    def test_la_distance_sort_en_metres(self, zone_centre: DeliveryZone, lome: City) -> None:
        """Le type `geography` fait raisonner PostGIS sur l'ellipsoïde : la
        distance est en mètres, sans projection à choisir ni erreur croissant
        avec la latitude."""
        from django.contrib.gis.db.models.functions import Distance

        cible = Point(1.2355, 6.1319, srid=4326)  # environ 1,1 km à l'est
        result = City.objects.annotate(d=Distance("centroid", cible)).get(pk=lome.pk)

        assert 1_000 < result.d.m < 1_300


class TestBaremeDeFrais:
    def test_les_frais_portent_une_devise(self, zone_centre: DeliveryZone) -> None:
        """Remplace la constante incohérente de l'existant — 5.00 côté commande
        contre 500.0 côté panier, sans devise ni règle."""
        zone_centre.refresh_from_db()

        assert zone_centre.base_fee == Money(500, "XOF")
        assert zone_centre.fee_per_km == Money(100, "XOF")

    def test_un_montant_nu_est_refuse(self, zone_centre: DeliveryZone) -> None:
        with pytest.raises(TypeError, match="Money"):
            zone_centre.base_fee = 500  # type: ignore[assignment]

    def test_un_seuil_de_franco_optionnel(self, zone_centre: DeliveryZone) -> None:
        assert zone_centre.free_delivery_threshold is None

        zone_centre.free_delivery_threshold = Money(10_000, "XOF")
        zone_centre.save()
        zone_centre.refresh_from_db()

        assert zone_centre.free_delivery_threshold == Money(10_000, "XOF")
