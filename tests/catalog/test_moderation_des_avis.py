"""Modération des avis — masquer, réafficher, et ce que la vitrine en montre.

Le module du catalogue l'annonçait sans l'offrir : « ce sont des gestes de
modération, qui appellent une trace d'audit et une permission dédiée ». Le test
décisif est `test_un_avis_masque_sort_de_la_note_moyenne` : un avis retiré pour
insulte qui pèserait encore sur la note resterait publié par un autre moyen.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import MenuItem, Review
from apps.catalog.services import ReviewService
from apps.restaurants.models import Restaurant, StaffMembership
from common.audit import AuditAction, AuditEntry

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def plat(menu_item: MenuItem) -> MenuItem:
    return menu_item


@pytest.fixture
def deux_avis(plat: MenuItem, customer: User) -> tuple[Review, Review]:
    autre = User.objects.create_user("autre@elcorazon.test", "motdepasse", full_name="Autre")
    insultant = ReviewService.submit(user=customer, menu_item=plat, rating=1, comment="Insulte.")
    honnete = ReviewService.submit(user=autre, menu_item=plat, rating=5, comment="Très bon.")
    return insultant, honnete


@pytest.fixture
def moderateur(restaurant: Restaurant) -> User:
    return personnel("mod@elcorazon.test", restaurant, "catalog.read", "catalog.write")


def masquer(user: User, avis: Review, motif: str = "Propos injurieux") -> Any:
    return connecte(user).post(
        reverse("v1:catalog:managed-review-hide", args=[avis.pk]), {"reason": motif}, format="json"
    )


def test_un_avis_masque_sort_de_la_note_moyenne(
    moderateur: User, plat: MenuItem, deux_avis: tuple[Review, Review]
) -> None:
    insultant, _ = deux_avis
    plat.refresh_from_db()
    assert plat.rating_average == Decimal("3.00")

    response = masquer(moderateur, insultant)

    assert response.status_code == status.HTTP_200_OK
    plat.refresh_from_db()
    assert plat.rating_average == Decimal("5.00")
    assert plat.rating_count == 1


def test_un_avis_masque_sort_de_la_liste_publique(
    moderateur: User, deux_avis: tuple[Review, Review]
) -> None:
    insultant, honnete = deux_avis
    masquer(moderateur, insultant)

    publics = APIClient().get(reverse("v1:catalog:review-list")).data["results"]

    assert {a["id"] for a in publics} == {str(honnete.pk)}


def test_masquer_exige_un_motif_et_catalog_write(
    restaurant: Restaurant, moderateur: User, deux_avis: tuple[Review, Review]
) -> None:
    insultant, _ = deux_avis
    lecteur = personnel("lecteur@elcorazon.test", restaurant, "catalog.read")

    sans_motif = masquer(moderateur, insultant, motif="  ")
    sans_droit = masquer(lecteur, insultant)

    assert sans_motif.status_code == status.HTTP_400_BAD_REQUEST
    assert sans_droit.status_code == status.HTTP_403_FORBIDDEN


def test_le_geste_est_au_journal_et_se_defait(
    moderateur: User, plat: MenuItem, deux_avis: tuple[Review, Review]
) -> None:
    insultant, _ = deux_avis
    masquer(moderateur, insultant)

    reaffiche = connecte(moderateur).post(
        reverse("v1:catalog:managed-review-show", args=[insultant.pk]), {}, format="json"
    )

    assert reaffiche.status_code == status.HTTP_200_OK
    assert reaffiche.data["hidden_at"] is None
    traces = AuditEntry.objects.filter(action=AuditAction.REVIEW_VISIBILITY).order_by("created_at")
    assert [t.after["visible"] for t in traces] == [False, True]
    plat.refresh_from_db()
    assert plat.rating_count == 2


def test_la_liste_de_moderation_filtre_les_mecontents(
    moderateur: User, deux_avis: tuple[Review, Review]
) -> None:
    insultant, _ = deux_avis

    response = connecte(moderateur).get(
        reverse("v1:catalog:managed-review-list"), {"rating__lte": 2}
    )

    assert [a["id"] for a in response.data["results"]] == [str(insultant.pk)]
