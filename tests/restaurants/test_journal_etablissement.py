"""Ouvrir, mettre en service et suspendre une cuisine laissent une trace.

Constaté au test global du 2026-09-25 : le journal consignait l'emplacement et
la zone d'un établissement, mais ni sa **création** ni ses **changements
d'état**. Suspendre une cuisine la fait disparaître de l'application cliente à
la seconde ; la mettre en service lui fait prendre des commandes. Personne ne
pouvait dire qui l'avait décidé, ni quand.

`common/audit.py` écarte les transitions d'état du journal parce qu'elles
« ont déjà leur trace ailleurs ». Ce n'est pas le cas d'un établissement :
aucune table ne garde son historique d'états, et la transition ne connaît pas
son auteur. Le journal ne double donc rien.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from apps.restaurants.states import RestaurantStatus
from common.models import AuditEntry

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def siege() -> User:
    return User.objects.create_superuser("siege-journal@elcorazon.test", "x")


@pytest.fixture
def as_siege(siege: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(siege)
    return client


def test_l_ouverture_d_un_etablissement_est_journalisee(
    as_siege: APIClient, siege: User, restaurant: Restaurant
) -> None:
    reponse = as_siege.post(
        reverse("v1:restaurants:managed-restaurant-list"),
        {
            "name": "Cuisine de Bè",
            "slug": "cuisine-de-be",
            "zone": str(restaurant.zone_id),
            "address": "Bè, Lomé",
            "location": {"lat": 6.1319, "lon": 1.2255},
            "phone": "+22890000001",
        },
        format="json",
    )
    assert reponse.status_code == 201, reponse.data

    entree = AuditEntry.objects.get(action="restaurant.create", target_id=reponse.data["id"])
    assert entree.actor == siege
    assert entree.target_label == "Cuisine de Bè"
    assert entree.after["status"] == RestaurantStatus.DRAFT


def test_un_changement_d_etat_est_journalise_avec_son_auteur(
    as_siege: APIClient, siege: User, restaurant: Restaurant
) -> None:
    avant = restaurant.status
    cible = (
        RestaurantStatus.INACTIVE
        if avant == RestaurantStatus.ACTIVE
        else RestaurantStatus.CONFIGURING
    )

    reponse = as_siege.post(
        reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
        {"status": cible},
        format="json",
    )
    assert reponse.status_code == 200, reponse.data

    entree = AuditEntry.objects.get(action="restaurant.status", target_id=str(restaurant.pk))
    assert entree.actor == siege
    assert entree.before == {"status": avant}
    assert entree.after == {"status": cible}
    assert entree.scope_restaurant_id == restaurant.pk


def test_une_transition_refusee_ne_laisse_rien(as_siege: APIClient, restaurant: Restaurant) -> None:
    Restaurant.objects.filter(pk=restaurant.pk).update(status=RestaurantStatus.DRAFT)

    reponse = as_siege.post(
        reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
        {"status": RestaurantStatus.ACTIVE},
        format="json",
    )

    assert reponse.status_code == 409
    assert not AuditEntry.objects.filter(action="restaurant.status").exists()
