"""Bilan d'une campagne — ouvertures, commandes, chiffre attribué.

Le cahier des charges demande taux d'ouverture, de conversion et ROI (§4.2.7).
Rien ne les calculait, alors que chaque notification portait déjà
l'identifiant de sa campagne. Le test décisif est
`test_le_chiffre_d_une_autre_cuisine_ne_se_lit_pas` : le bilan agrège des
commandes, et un agrégat non cloisonné est exactement la fuite que le lot du
8 septembre a fermée pour les statistiques.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.notifications.models import Audience, Campaign, Notification
from apps.notifications.services import send_campaign
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def envoyee(customer: User) -> Campaign:
    campagne = Campaign.objects.create(
        title="−20 % ce week-end", body="Jusqu'à dimanche.", audience=Audience.ALL_CUSTOMERS
    )
    return send_campaign(campagne)


def bilan(user: User, campagne: Campaign) -> dict[str, object]:
    reponse = connecte(user).get(reverse("v1:notifications:campaign-stats", args=[campagne.pk]))
    assert reponse.status_code == 200, reponse.data
    return dict(reponse.data)


def test_une_lecture_fait_monter_le_taux_d_ouverture(
    restaurant: Restaurant, customer: User, envoyee: Campaign
) -> None:
    lecteur = personnel("marketing@elcorazon.test", restaurant, "notifications.send")
    Notification.objects.filter(user=customer, data__campaign=str(envoyee.pk)).update(
        read_at=timezone.now()
    )

    resultat = bilan(lecteur, envoyee)

    assert resultat["recipients"] == 1
    assert resultat["read"] == 1
    assert resultat["open_rate"] == 1.0


def test_une_commande_apres_l_envoi_compte_une_conversion(
    restaurant: Restaurant, customer: User, envoyee: Campaign
) -> None:
    """Le taux se lit au siège : c'est le seul compte dont le numérateur et le
    dénominateur portent sur la même population (voir le test suivant)."""
    siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
    commande = build_order(restaurant, customer)
    commande.placed_at = envoyee.sent_at + dt.timedelta(days=2)
    commande.save(update_fields=["placed_at"])

    resultat = bilan(siege, envoyee)

    assert resultat["customers_who_ordered"] == 1
    assert resultat["conversion_rate"] == 1.0
    assert resultat["revenue"] == [
        {"amount": str(commande.total.amount_minor), "currency": commande.total.currency}
    ]


def test_un_compte_cloisonne_ne_lit_pas_de_taux_de_conversion(
    restaurant: Restaurant, customer: User, envoyee: Campaign
) -> None:
    """Son numérateur est cloisonné, son dénominateur — les destinataires de
    la campagne — ne l'est pas : le rapport des deux ne veut rien dire.

    Un gérant lisait « 2 % » là où la campagne avait converti 20 % de ses
    destinataires, le reste ayant commandé ailleurs. Ses clients et son chiffre
    restent lisibles ; le taux, lui, est absent plutôt que faux.
    """
    lecteur = personnel("marketing@elcorazon.test", restaurant, "notifications.send")
    commande = build_order(restaurant, customer)
    commande.placed_at = envoyee.sent_at + dt.timedelta(days=2)
    commande.save(update_fields=["placed_at"])

    resultat = bilan(lecteur, envoyee)

    assert resultat["customers_who_ordered"] == 1
    assert resultat["conversion_rate"] is None
    assert resultat["revenue"] == [
        {"amount": str(commande.total.amount_minor), "currency": commande.total.currency}
    ]


def test_une_commande_d_avant_l_envoi_ne_compte_pas(
    restaurant: Restaurant, customer: User, envoyee: Campaign
) -> None:
    lecteur = personnel("marketing@elcorazon.test", restaurant, "notifications.send")
    commande = build_order(restaurant, customer)
    commande.placed_at = envoyee.sent_at - dt.timedelta(hours=1)
    commande.save(update_fields=["placed_at"])

    assert bilan(lecteur, envoyee)["customers_who_ordered"] == 0


def test_le_chiffre_d_une_autre_cuisine_ne_se_lit_pas(
    restaurant: Restaurant, customer: User, envoyee: Campaign
) -> None:
    ailleurs = Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=restaurant.zone,
        address="Kara",
        location=restaurant.location,
        phone="+22890000009",
    )
    kara = personnel("kara@elcorazon.test", ailleurs, "notifications.send")
    commande = build_order(restaurant, customer)
    commande.placed_at = envoyee.sent_at + dt.timedelta(days=1)
    commande.save(update_fields=["placed_at"])

    resultat = bilan(kara, envoyee)

    assert resultat["customers_who_ordered"] == 0
    assert resultat["revenue"] == []


def test_un_brouillon_rend_un_bilan_vide_sans_taux(restaurant: Restaurant) -> None:
    lecteur = personnel("marketing@elcorazon.test", restaurant, "notifications.send")
    brouillon = Campaign.objects.create(
        title="Brouillon", body="…", audience=Audience.ALL_CUSTOMERS
    )

    resultat = bilan(lecteur, brouillon)

    assert resultat["recipients"] == 0
    assert resultat["open_rate"] is None
    assert resultat["conversion_rate"] is None
