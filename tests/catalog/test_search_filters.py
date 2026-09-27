"""Recherche avancée du catalogue — `MenuItemFilter`.

Ces filtres remplacent une recherche que l'app composait elle-même contre
Supabase, puis finissait de trier en mémoire sur la page reçue. Le test qui
compte ici est `test_le_filtre_porte_sur_le_catalogue_pas_sur_une_page` : il
échoue dès qu'un critère redevient un filtre client.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, MenuItem
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
LISTE = "v1:catalog:item-list"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def carte(restaurant: Restaurant, category: Category) -> list[MenuItem]:
    """Trois articles qui se distinguent sur chaque critère filtrable."""
    return [
        MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Salade Corazón",
            slug="salade-corazon",
            description="Salade fraîche de saison.",
            price=Money(2_000, XOF),
            preparation_minutes=5,
            calories=300,
            ingredients=["tomate", "basilic"],
            allergens=[],
            dietary_tags=["vegetarian", "vegan"],
            rating_average="4.80",
        ),
        MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Burger Corazón",
            slug="burger-corazon",
            description="Le classique de la maison.",
            price=Money(3_500, XOF),
            preparation_minutes=15,
            calories=850,
            ingredients=["tomate", "boeuf"],
            allergens=["gluten"],
            dietary_tags=[],
            rating_average="4.20",
        ),
        MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Pad thaï aux arachides",
            slug="pad-thai",
            description="Nouilles sautées.",
            price=Money(5_000, XOF),
            preparation_minutes=25,
            calories=700,
            ingredients=["arachide", "nouilles"],
            allergens=["arachide"],
            dietary_tags=["vegetarian"],
            rating_average="3.90",
        ),
    ]


def noms(response: object) -> list[str]:
    return [item["name"] for item in response.data["results"]]  # type: ignore[attr-defined]


class TestFiltresDePrix:
    def test_prix_minimum_en_unite_mineure(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"price_min": 3_000})

        assert response.status_code == status.HTTP_200_OK
        assert sorted(noms(response)) == ["Burger Corazón", "Pad thaï aux arachides"]

    def test_fourchette_de_prix(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"price_min": 2_500, "price_max": 4_000})

        assert noms(response) == ["Burger Corazón"]


class TestFiltresNutritionnels:
    def test_calories_maximales(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"calories_max": 750})

        assert sorted(noms(response)) == ["Pad thaï aux arachides", "Salade Corazón"]

    def test_temps_de_preparation(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"preparation_max": 15})

        assert sorted(noms(response)) == ["Burger Corazón", "Salade Corazón"]

    def test_note_minimale(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"rating_min": "4.50"})

        assert noms(response) == ["Salade Corazón"]


class TestRegimesEtAllergenes:
    def test_cumuler_deux_regimes_restreint(self, client: APIClient, carte: list[MenuItem]) -> None:
        """`vegetarian` en rend deux, `vegetarian,vegan` un seul."""
        assert len(noms(client.get(reverse(LISTE), {"dietary_tags": "vegetarian"}))) == 2
        assert noms(client.get(reverse(LISTE), {"dietary_tags": "vegetarian,vegan"})) == [
            "Salade Corazón"
        ]

    def test_un_allergene_ecarte_suffit(self, client: APIClient, carte: list[MenuItem]) -> None:
        """Le piège du filtre d'allergènes : écarter les articles qui les
        contiennent **tous** laisserait passer un plat aux arachides dès qu'il
        est sans gluten. Ici, chacun exclut à lui seul."""
        response = client.get(reverse(LISTE), {"exclude_allergens": "arachide,gluten"})

        assert noms(response) == ["Salade Corazón"]

    def test_ingredients_cumules(self, client: APIClient, carte: list[MenuItem]) -> None:
        assert sorted(noms(client.get(reverse(LISTE), {"ingredients": "tomate"}))) == [
            "Burger Corazón",
            "Salade Corazón",
        ]
        assert noms(client.get(reverse(LISTE), {"ingredients": "tomate,basilic"})) == [
            "Salade Corazón"
        ]

    def test_valeurs_vides_ignorees(self, client: APIClient, carte: list[MenuItem]) -> None:
        """`dietary_tags=` (vide) ne doit pas rendre une liste vide : un critère
        non renseigné n'est pas un critère impossible."""
        response = client.get(reverse(LISTE), {"dietary_tags": "", "exclude_allergens": " , "})

        assert len(noms(response)) == 3


class TestCombinaisons:
    def test_le_filtre_porte_sur_le_catalogue_pas_sur_une_page(
        self, client: APIClient, carte: list[MenuItem]
    ) -> None:
        """Avec une page d'un seul article, le résultat reste juste.

        C'est ce que l'ancienne recherche ne pouvait pas garantir : elle
        recevait une page, puis filtrait dedans — l'article recherché n'y était
        pas forcément.
        """
        response = client.get(
            reverse(LISTE), {"page_size": 1, "dietary_tags": "vegan", "ordering": "name"}
        )

        assert response.data["count"] == 1
        assert noms(response) == ["Salade Corazón"]

    def test_recherche_texte_et_filtres_se_cumulent(
        self, client: APIClient, carte: list[MenuItem]
    ) -> None:
        response = client.get(reverse(LISTE), {"search": "corazón", "price_max": 2_500})

        assert noms(response) == ["Salade Corazón"]

    def test_tri_par_temps_de_preparation(self, client: APIClient, carte: list[MenuItem]) -> None:
        response = client.get(reverse(LISTE), {"ordering": "preparation_minutes"})

        assert noms(response) == ["Salade Corazón", "Burger Corazón", "Pad thaï aux arachides"]
