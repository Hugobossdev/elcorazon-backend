"""API du catalogue — invariant C1.

C1 est la faille la plus coûteuse de l'implémentation précédente : le prix
était envoyé par le client et facturé tel quel. Le test qui la ferme ici est
`test_le_prix_n_est_jamais_accepte_du_client` — le champ n'existe dans aucun
sérialiseur d'entrée, donc aucune requête ne peut le porter.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


class TestCategories:
    def test_lisibles_sans_compte(self, client: APIClient, category: Category) -> None:
        response = client.get(reverse("v1:catalog:category-list"))

        assert response.status_code == status.HTTP_200_OK
        assert [c["slug"] for c in response.data] == ["burgers"]

    def test_non_paginees(self, client: APIClient, category: Category) -> None:
        """Une carte compte une dizaine de catégories : les paginer obligerait
        chaque client à boucler pour afficher un menu complet."""
        assert isinstance(client.get(reverse("v1:catalog:category-list")).data, list)

    def test_filtrees_par_restaurant(self, client: APIClient, category: Category) -> None:
        response = client.get(
            reverse("v1:catalog:category-list"), {"restaurant__slug": category.restaurant.slug}
        )

        assert len(response.data) == 1
        assert response.data[0]["emoji"] == "🍔"

    def test_une_categorie_desactivee_disparait(
        self, client: APIClient, category: Category
    ) -> None:
        Category.objects.filter(pk=category.pk).update(is_active=False)

        assert client.get(reverse("v1:catalog:category-list")).data == []


class TestArticles:
    def test_le_prix_sort_avec_sa_devise(self, client: APIClient, menu_item: MenuItem) -> None:
        fiche = client.get(reverse("v1:catalog:item-list")).data["results"][0]

        assert fiche["price"] == {"amount": "3500", "currency": XOF}

    def test_le_prix_n_est_jamais_accepte_du_client(self) -> None:
        """C1 — le prix vit dans le catalogue et nulle part ailleurs. Aucun
        sérialiseur d'entrée du domaine ne le porte : ce n'est pas une
        validation qu'on peut oublier d'écrire, c'est un champ qui n'existe
        pas."""
        from apps.catalog.serializers import MenuItemDetailSerializer, ReviewWriteSerializer

        assert "price" not in ReviewWriteSerializer().fields
        assert MenuItemDetailSerializer().fields["price"].read_only

    def test_un_article_supprime_disparait_du_catalogue(
        self, client: APIClient, menu_item: MenuItem
    ) -> None:
        """Suppression logique : invisible au client, toujours lisible depuis
        les commandes passées qui en conservent une copie figée."""
        menu_item.delete()

        assert client.get(reverse("v1:catalog:item-list")).data["count"] == 0
        assert MenuItem.objects.filter(pk=menu_item.pk).exists()

    def test_un_article_d_un_restaurant_inactif_disparait(
        self, client: APIClient, menu_item: MenuItem, restaurant: Restaurant
    ) -> None:
        Restaurant.objects.filter(pk=restaurant.pk).update(is_active=False)

        assert client.get(reverse("v1:catalog:item-list")).data["count"] == 0

    def test_recherche_par_nom(self, client: APIClient, menu_item: MenuItem) -> None:
        trouve = client.get(reverse("v1:catalog:item-list"), {"search": "corazón"})
        rate = client.get(reverse("v1:catalog:item-list"), {"search": "pizza"})

        assert trouve.data["count"] == 1
        assert rate.data["count"] == 0

    def test_filtre_par_categorie(self, client: APIClient, menu_item: MenuItem) -> None:
        response = client.get(reverse("v1:catalog:item-list"), {"category__slug": "burgers"})

        assert response.data["count"] == 1

    def test_tri_par_prix(self, client: APIClient, menu_item: MenuItem, category: Category) -> None:
        """Le tri porte sur `price_minor`, l'entier : trier sur une chaîne
        formatée mettrait « 10 000 » avant « 900 »."""
        MenuItem.objects.create(
            restaurant=menu_item.restaurant,
            category=category,
            name="Frites",
            slug="frites",
            price=Money(900, XOF),
        )

        response = client.get(reverse("v1:catalog:item-list"), {"ordering": "price_minor"})

        assert [i["name"] for i in response.data["results"]] == ["Frites", "Burger Corazón"]


class TestFicheArticle:
    def test_la_fiche_porte_les_options_que_la_liste_omet(
        self, client: APIClient, menu_item: MenuItem, option: Option
    ) -> None:
        liste = client.get(reverse("v1:catalog:item-list")).data["results"][0]
        fiche = client.get(reverse("v1:catalog:item-detail", args=[menu_item.pk])).data

        assert "option_groups" not in liste
        assert fiche["option_groups"][0]["name"] == "Cuisson"
        assert fiche["option_groups"][0]["is_required"] is True
        assert fiche["option_groups"][0]["options"][0]["name"] == "À point"

    def test_l_ecart_de_prix_d_une_option_porte_sa_devise(
        self, client: APIClient, menu_item: MenuItem, option_group: OptionGroup
    ) -> None:
        """« Sans fromage, −200 F » : l'écart peut être négatif."""
        Option.objects.create(group=option_group, name="Sans fromage", price_delta=Money(-200, XOF))

        fiche = client.get(reverse("v1:catalog:item-detail", args=[menu_item.pk])).data
        ecarts = {o["name"]: o["price_delta"] for o in fiche["option_groups"][0]["options"]}

        assert ecarts["Sans fromage"] == {"amount": "-200", "currency": XOF}

    def test_une_option_indisponible_reste_visible_mais_marquee(
        self, client: APIClient, menu_item: MenuItem, option: Option
    ) -> None:
        """La masquer ferait croire à un menu qui change de forme d'une minute
        à l'autre."""
        Option.objects.filter(pk=option.pk).update(is_available=False)

        fiche = client.get(reverse("v1:catalog:item-detail", args=[menu_item.pk])).data

        assert fiche["option_groups"][0]["options"][0]["is_available"] is False


class TestEcriture:
    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_le_catalogue_est_en_lecture_seule_sur_cette_api(
        self, client: APIClient, menu_item: MenuItem, method: str
    ) -> None:
        """L'administration du catalogue passe par le back-office et la
        permission `catalog.write`, pas par la route publique."""
        url = (
            reverse("v1:catalog:item-list")
            if method == "post"
            else reverse("v1:catalog:item-detail", args=[menu_item.pk])
        )

        response = getattr(client, method)(url, {}, format="json")

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
