"""Le point d'enlèvement rendu sur la commande du client.

Pourquoi ce champ existe
------------------------

L'écran de suivi du client montre trois repères — d'où part le repas, où il en
est, où il va. Il n'en tenait que deux : la commande ne disait pas d'où partait
le repas, et l'application suppléait le troisième par une **constante écrite en
dur** dans son code. Cette constante désigne le premier établissement ; elle
devient fausse au deuxième, sans qu'aucune erreur ne s'affiche — le client voit
simplement un restaurant au mauvais endroit.

La course du livreur porte déjà ce point (`AssignmentSerializer.pickup_location`),
mais le client ne voit jamais sa course : le contrat le lui interdit. C'est donc
à la commande de le porter.

Le champ est en **lecture seule**, comme tout le reste de `OrderSerializer` : la
position d'un établissement se règle au back-office, pas depuis un téléphone.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.orders.models import Order
from apps.restaurants.models import Restaurant

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


def test_le_detail_rend_le_point_denlevement(
    as_customer: APIClient, order: Order, restaurant: Restaurant
) -> None:
    response = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))

    assert response.status_code == status.HTTP_200_OK
    assert response.data["restaurant_location"] == {
        "lat": restaurant.location.y,
        "lon": restaurant.location.x,
    }


def test_la_liste_le_rend_aussi(as_customer: APIClient, order: Order) -> None:
    """Sur les deux formes, et pas seulement sur le détail.

    L'historique ouvre le suivi d'une commande en cours sans repasser par le
    détail : le point manquant sur la liste ferait apparaître le repère du
    restaurant une seconde après les autres, ou pas du tout.
    """
    response = as_customer.get(reverse("v1:orders:order-list"))

    assert response.status_code == status.HTTP_200_OK
    assert "restaurant_location" in response.data["results"][0]


def test_lordre_des_coordonnees_est_celui_du_contrat(as_customer: APIClient, order: Order) -> None:
    """`lat`/`lon` nommés, jamais un couple positionnel.

    PostGIS stocke `Point(x=lon, y=lat)` — l'inverse de l'ordre de lecture
    humain. Un tableau `[6.13, 1.22]` aurait été relu à l'envers par au moins
    une des trois applications, et le restaurant serait apparu au large du
    Ghana.
    """
    response = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))
    point = response.data["restaurant_location"]

    assert set(point) == {"lat", "lon"}
    # Lomé : latitude ~6.13 N, longitude ~1.22 E. Une inversion se verrait ici.
    assert 0 < point["lat"] < 15
    assert 0 < point["lon"] < 5


def test_il_est_en_lecture_seule(as_customer: APIClient, order: Order) -> None:
    """Écrire dessus ne déplace pas l'établissement.

    `OrderSerializer` est intégralement en lecture seule ; le vérifier ici plutôt
    que de s'y fier évite qu'un champ ajouté un jour en écriture ouvre, par
    inadvertance, la position d'un restaurant à ses clients.
    """
    avant = Restaurant.objects.get(pk=order.restaurant_id).location

    as_customer.patch(
        reverse("v1:orders:order-detail", args=[order.pk]),
        {"restaurant_location": {"lat": 48.85, "lon": 2.35}},
        format="json",
    )

    assert Restaurant.objects.get(pk=order.restaurant_id).location == avant
