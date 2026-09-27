"""Supprimer une zone propre qui porte encore un établissement.

Constaté à l'audit d'ADMIN, le 2026-09-25 : une cuisine peut être posée sur
l'une de ses **propres** zones (`zone_anchoring_problem` l'autorise), et la route
de suppression de ces zones appelait `instance.delete()` sans le vérifier.
`Restaurant.zone` étant `PROTECT`, la base refusait — en `ProtectedError`, donc
en 500, sans rien dire de ce qu'il fallait faire.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.geography.models import City, DeliveryZone, ZoneShape
from apps.geography.shapes import circle_to_boundary
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def as_siege() -> APIClient:
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege-suppr@elcorazon.test", "x"))
    return client


def zone_propre(city: City, restaurant: Restaurant, nom: str) -> DeliveryZone:
    centre = Point(1.2255, 6.1319, srid=4326)
    return DeliveryZone.objects.create(
        city=city,
        restaurant=restaurant,
        name=nom,
        shape=ZoneShape.CIRCLE,
        center=centre,
        radius_meters=3000,
        boundary=circle_to_boundary(centre, 3000),
        base_fee=Money(500, "XOF"),
        fee_per_km=Money(0, "XOF"),
    )


def supprimer(client: APIClient, zone: DeliveryZone) -> object:
    return client.delete(reverse("v1:restaurants:managed-restaurant-zone-detail", args=[zone.pk]))


def test_la_zone_qui_porte_l_etablissement_ne_se_supprime_pas(
    as_siege: APIClient, city: City, restaurant: Restaurant
) -> None:
    zone = zone_propre(city, restaurant, "Bè")
    Restaurant.objects.filter(pk=restaurant.pk).update(zone=zone)

    reponse = supprimer(as_siege, zone)

    assert reponse.status_code == 409, getattr(reponse, "data", reponse)
    assert restaurant.name in reponse.data["detail"]
    assert DeliveryZone.objects.filter(pk=zone.pk).exists()


def test_une_zone_propre_libre_se_supprime(
    as_siege: APIClient, city: City, restaurant: Restaurant
) -> None:
    zone = zone_propre(city, restaurant, "Tokoin")

    reponse = supprimer(as_siege, zone)

    assert reponse.status_code == 204
    assert not DeliveryZone.objects.filter(pk=zone.pk).exists()
