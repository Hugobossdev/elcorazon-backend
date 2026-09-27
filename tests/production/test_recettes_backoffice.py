"""Back-office des recettes — la porte d'entrée de la matière.

Sans ces routes, une recette ne se saisissait que par un `shell` : tout le
mécanisme de réservation et de rupture existait, et aucun plat n'en avait.

Ce que la suite verrouille : la permission propre aux recettes, le cloisonnement
par la cuisine de la cible, les règles de saisie qu'aucune contrainte de table
ne porte, et la couverture — ce qu'il reste à saisir avant de croire un coût
matière.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.geography.models import DeliveryZone
from apps.inventory.models import Ingredient
from apps.production.models import Recipe
from apps.restaurants.models import Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from common.quantities import Dimension

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def membre(email: str, restaurant: Restaurant, *permissions: str) -> APIClient:
    user = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=user, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(user)
    return client


def url(nom: str, *args: Any) -> str:
    return reverse(f"v1:production:{nom}", args=args)


@pytest.fixture
def chef(restaurant: Restaurant) -> APIClient:
    return membre("chef@elcorazon.test", restaurant, "recipes.read", "recipes.write")


@pytest.fixture
def lecteur(restaurant: Restaurant) -> APIClient:
    return membre("lecteur@elcorazon.test", restaurant, "recipes.read")


@pytest.fixture
def ailleurs(zone: DeliveryZone) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=zone,
        address="Kara",
        location=zone.city.centroid,
        phone="+22890000001",
        status=RestaurantStatus.ACTIVE,
    )


@pytest.fixture
def oignon() -> Ingredient:
    return Ingredient.objects.create(name="Oignon", slug="oignon", dimension=Dimension.MASS)


@pytest.fixture
def sans_oignon(menu_item: MenuItem) -> Option:
    groupe = OptionGroup.objects.create(menu_item=menu_item, name="Retirer", max_select=3)
    return Option.objects.create(group=groupe, name="Sans oignon", price_delta=Money(0, XOF))


def creer(client: APIClient, **cible: str) -> Any:
    return client.post(url("managed-recipe-list"), {**cible, "notes": ""}, format="json")


def poser(
    client: APIClient, recette_id: str, ingredient: Ingredient, montant: str, unite: str
) -> Any:
    return client.post(
        url("managed-recipe-lines", recette_id),
        {"ingredient": str(ingredient.pk), "quantity": {"amount": montant, "unit": unite}},
        format="json",
    )


class TestSaisie:
    def test_une_recette_se_cree_et_se_compose(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        creation = creer(chef, menu_item=str(menu_item.pk))
        assert creation.status_code == status.HTTP_201_CREATED
        assert creation.data["target_name"] == menu_item.name

        ligne = poser(chef, creation.data["id"], oignon, "20", "g")

        assert ligne.status_code == status.HTTP_200_OK
        assert ligne.data["lines"] == [
            {
                "id": ligne.data["lines"][0]["id"],
                "ingredient": oignon.pk,
                "ingredient_name": "Oignon",
                "ingredient_slug": "oignon",
                "quantity": {"amount": "20", "unit": "g", "dimension": "mass"},
            }
        ]

    def test_poser_deux_fois_le_meme_ingredient_remplace(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        """Deux lignes pour la même référence se liraient comme un remplacement
        alors qu'elles s'additionnent."""
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]
        poser(chef, recette_id, oignon, "20", "g")

        reponse = poser(chef, recette_id, oignon, "0.03", "kg")

        assert len(reponse.data["lines"]) == 1
        assert reponse.data["lines"][0]["quantity"]["amount"] == "30"

    def test_une_quantite_dans_la_mauvaise_dimension_est_refusee(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]

        reponse = poser(chef, recette_id, oignon, "20", "ml")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert reponse.data["code"] == "dimension_mismatch"
        assert not Recipe.objects.get(pk=recette_id).lines.exists()

    def test_une_quantite_negative_sur_un_plat_est_refusee(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        """« −20 g » sur un plat serait ramené à zéro en silence par le plancher,
        et la faute de saisie ne se verrait jamais."""
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]

        reponse = poser(chef, recette_id, oignon, "-20", "g")

        assert reponse.status_code == status.HTTP_409_CONFLICT

    def test_une_option_de_retrait_porte_une_quantite_negative(
        self, chef: APIClient, sans_oignon: Option, oignon: Ingredient
    ) -> None:
        recette_id = creer(chef, option=str(sans_oignon.pk)).data["id"]

        reponse = poser(chef, recette_id, oignon, "-20", "g")

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["target_name"] == "Retirer › Sans oignon"
        assert reponse.data["lines"][0]["quantity"]["amount"] == "-20"

    def test_un_ingredient_retire_n_entre_plus_dans_une_recette(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        Ingredient.objects.filter(pk=oignon.pk).update(is_active=False)
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]

        reponse = poser(chef, recette_id, oignon, "20", "g")

        assert reponse.status_code == status.HTTP_409_CONFLICT

    def test_une_recette_vise_exactement_une_cible(
        self, chef: APIClient, menu_item: MenuItem, sans_oignon: Option
    ) -> None:
        deux = creer(chef, menu_item=str(menu_item.pk), option=str(sans_oignon.pk))
        aucune = chef.post(url("managed-recipe-list"), {"notes": "rien"}, format="json")

        assert deux.status_code == status.HTTP_400_BAD_REQUEST
        assert aucune.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_plat_n_a_qu_une_recette(self, chef: APIClient, menu_item: MenuItem) -> None:
        creer(chef, menu_item=str(menu_item.pk))

        seconde = creer(chef, menu_item=str(menu_item.pk))

        assert seconde.status_code == status.HTTP_400_BAD_REQUEST

    def test_la_cible_ne_change_pas(
        self, chef: APIClient, menu_item: MenuItem, category: Category, restaurant: Restaurant
    ) -> None:
        """Réattribuer une recette ferait porter à un autre plat l'histoire de ce
        que le premier a consommé."""
        salade = MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Salade",
            slug="salade",
            price=Money(2_000, XOF),
        )
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]

        reponse = chef.patch(
            url("managed-recipe-detail", recette_id), {"menu_item": str(salade.pk)}, format="json"
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_retirer_une_ligne_et_retirer_ce_qui_n_y_est_pas(
        self, chef: APIClient, menu_item: MenuItem, oignon: Ingredient
    ) -> None:
        recette_id = creer(chef, menu_item=str(menu_item.pk)).data["id"]
        poser(chef, recette_id, oignon, "20", "g")
        adresse = reverse(
            "v1:production:managed-recipe-remove-line", args=[recette_id, str(oignon.pk)]
        )

        premiere = chef.delete(adresse)
        seconde = chef.delete(adresse)

        assert premiere.status_code == seconde.status_code == status.HTTP_200_OK
        assert premiere.data["lines"] == []


class TestRefus:
    def test_sans_recipes_write_on_ne_compose_pas(
        self, lecteur: APIClient, menu_item: MenuItem
    ) -> None:
        reponse = creer(lecteur, menu_item=str(menu_item.pk))

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_le_catalogue_ne_donne_pas_acces_aux_recettes(
        self, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """Une recette change le coût matière, pas ce que voit le client : la
        permission du catalogue n'y ouvre rien."""
        redacteur = membre("carte@elcorazon.test", restaurant, "catalog.read", "catalog.write")

        assert redacteur.get(url("managed-recipe-list")).status_code == status.HTTP_403_FORBIDDEN

    def test_on_ne_cree_pas_la_recette_d_un_plat_d_une_autre_cuisine(
        self, chef: APIClient, ailleurs: Restaurant
    ) -> None:
        categorie = Category.objects.create(restaurant=ailleurs, name="Plats", slug="plats")
        plat = MenuItem.objects.create(
            restaurant=ailleurs,
            category=categorie,
            name="Riz",
            slug="riz",
            price=Money(1_500, XOF),
        )

        reponse = creer(chef, menu_item=str(plat.pk))

        assert reponse.status_code == status.HTTP_403_FORBIDDEN
        assert not Recipe.objects.filter(menu_item=plat).exists()

    def test_la_recette_d_une_autre_cuisine_est_introuvable(
        self, chef: APIClient, ailleurs: Restaurant, oignon: Ingredient
    ) -> None:
        categorie = Category.objects.create(restaurant=ailleurs, name="Plats", slug="plats")
        plat = MenuItem.objects.create(
            restaurant=ailleurs,
            category=categorie,
            name="Riz",
            slug="riz",
            price=Money(1_500, XOF),
        )
        recette = Recipe.objects.create(menu_item=plat)

        detail = chef.get(url("managed-recipe-detail", recette.pk))
        ligne = poser(chef, str(recette.pk), oignon, "20", "g")
        liste = chef.get(url("managed-recipe-list"))

        assert detail.status_code == status.HTTP_404_NOT_FOUND
        assert ligne.status_code == status.HTTP_404_NOT_FOUND
        assert liste.data["results"] == []


class TestCouverture:
    def test_la_couverture_dit_ce_qu_il_reste_a_saisir(
        self, chef: APIClient, restaurant: Restaurant, category: Category, menu_item: MenuItem
    ) -> None:
        """Tant que la liste n'est pas vide, un plat ne consomme rien au stock
        et son coût matière est inconnu."""
        frites = MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Frites",
            slug="frites",
            price=Money(1_000, XOF),
        )
        retire = MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Ancien",
            slug="ancien",
            price=Money(1_000, XOF),
        )
        retire.delete()
        creer(chef, menu_item=str(menu_item.pk))

        reponse = chef.get(url("managed-recipe-coverage"), {"restaurant": restaurant.slug})

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["items_total"] == 2
        assert reponse.data["items_with_recipe"] == 1
        assert [plat["id"] for plat in reponse.data["missing"]] == [str(frites.pk)]

    def test_la_couverture_d_une_autre_cuisine_est_introuvable(
        self, chef: APIClient, ailleurs: Restaurant
    ) -> None:
        reponse = chef.get(url("managed-recipe-coverage"), {"restaurant": ailleurs.slug})

        assert reponse.status_code == status.HTTP_404_NOT_FOUND
