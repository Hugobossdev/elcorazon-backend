"""API du panier — invariant C1.

Le test décisif est `test_le_prix_envoye_par_le_client_est_ignore` : dans
l'implémentation précédente, la ligne de panier portait une colonne `price`
renseignée depuis la requête, et un client pouvait fixer son propre prix. Ici
la colonne n'existe pas — il n'y a rien à valider, donc rien à oublier.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.carts.models import CartLine
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    """Client HTTP **distinct** de la fixture `client`.

    Les réutiliser ferait qu'un `force_authenticate` plus loin dans le test
    changerait aussi l'identité de celui-ci — deux acteurs qui n'en font qu'un,
    et un test qui ne vérifie plus ce qu'il annonce.
    """
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


def cart_url(restaurant: Restaurant) -> str:
    return reverse("v1:carts:cart-detail", args=[restaurant.slug])


def lines_url(restaurant: Restaurant) -> str:
    return reverse("v1:carts:cart-add-line", args=[restaurant.slug])


def line_url(restaurant: Restaurant, line_id: str) -> str:
    return reverse("v1:carts:cart-set-quantity", args=[restaurant.slug, line_id])


class TestOuverture:
    def test_un_panier_absent_est_rendu_vide(
        self, as_customer: APIClient, restaurant: Restaurant
    ) -> None:
        """« Pas encore de panier » et « panier vide » sont le même écran :
        les distinguer obligerait chaque client à gérer un cas de plus."""
        response = as_customer.get(cart_url(restaurant))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["lines"] == []
        assert response.data["subtotal"] == {"amount": "0", "currency": XOF}
        assert response.data["is_orderable"] is False

    def test_un_livreur_n_a_pas_de_panier(
        self, client: APIClient, courier_user: User, restaurant: Restaurant
    ) -> None:
        client.force_authenticate(courier_user)

        assert client.get(cart_url(restaurant)).status_code == status.HTTP_403_FORBIDDEN


class TestAjout:
    def test_le_prix_vient_du_catalogue(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        response = as_customer.post(
            lines_url(restaurant), {"menu_item": str(menu_item.pk), "quantity": 2}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        ligne = response.data["lines"][0]
        assert ligne["unit_price"] == {"amount": "3500", "currency": XOF}
        assert ligne["total"] == {"amount": "7000", "currency": XOF}
        assert response.data["subtotal"] == {"amount": "7000", "currency": XOF}

    def test_le_prix_envoye_par_le_client_est_ignore(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """C1 — le champ n'existe ni au contrat ni en base."""
        response = as_customer.post(
            lines_url(restaurant),
            {
                "menu_item": str(menu_item.pk),
                "quantity": 1,
                "price": {"amount": "1", "currency": XOF},
            },
            format="json",
        )

        assert response.data["subtotal"] == {"amount": "3500", "currency": XOF}

    def test_les_options_s_ajoutent_au_prix(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
        option: Option,
    ) -> None:
        supplement = Option.objects.create(
            group=OptionGroup.objects.create(
                menu_item=menu_item, name="Suppléments", min_select=0, max_select=2
            ),
            name="Double fromage",
            price_delta=Money(500, XOF),
        )

        response = as_customer.post(
            lines_url(restaurant),
            {
                "menu_item": str(menu_item.pk),
                "quantity": 1,
                "options": [str(option.pk), str(supplement.pk)],
            },
            format="json",
        )

        assert response.data["lines"][0]["unit_price"] == {"amount": "4000", "currency": XOF}

    def test_un_ecart_negatif_reduit_le_prix(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
        option: Option,
    ) -> None:
        """« Sans fromage, −200 F » : l'écart peut être négatif."""
        sans = Option.objects.create(
            group=OptionGroup.objects.create(
                menu_item=menu_item, name="Retraits", min_select=0, max_select=1
            ),
            name="Sans fromage",
            price_delta=Money(-200, XOF),
        )

        response = as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(menu_item.pk), "options": [str(option.pk), str(sans.pk)]},
            format="json",
        )

        assert response.data["lines"][0]["unit_price"] == {"amount": "3300", "currency": XOF}

    def test_un_article_d_un_autre_restaurant_est_refuse(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem, zone
    ) -> None:
        """Une commande ne peut pas mélanger deux établissements : elle est
        préparée à un endroit et enlevée en un seul point."""
        autre = Restaurant.objects.create(
            name="Autre",
            slug="autre",
            zone=zone,
            address="X",
            location=restaurant.location,
            phone="+22890000002",
        )
        categorie = Category.objects.create(restaurant=autre, name="Plats", slug="plats")
        ailleurs = MenuItem.objects.create(
            restaurant=autre,
            category=categorie,
            name="Poulet",
            slug="poulet",
            price=Money(2_000, XOF),
        )

        response = as_customer.post(
            lines_url(restaurant), {"menu_item": str(ailleurs.pk)}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_article_indisponible_est_refuse(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        response = as_customer.post(
            lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestBornesDesGroupes:
    def test_un_groupe_obligatoire_doit_etre_servi(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
    ) -> None:
        """« Cuisson » exige un choix : l'omettre produirait un plat que la
        cuisine ne saurait pas préparer."""
        response = as_customer.post(
            lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "Cuisson" in response.data["detail"]

    def test_le_plafond_d_un_groupe_est_applique(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
        option: Option,
    ) -> None:
        saignant = Option.objects.create(
            group=option_group, name="Saignant", price_delta=Money(0, XOF)
        )

        response = as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(menu_item.pk), "options": [str(option.pk), str(saignant.pk)]},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "au plus 1" in response.data["detail"]

    def test_une_option_d_un_autre_article_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        category: Category,
        option: Option,
    ) -> None:
        autre_article = MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Frites",
            slug="frites",
            price=Money(900, XOF),
        )

        response = as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(autre_article.pk), "options": [str(option.pk)]},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestFusionDesLignes:
    def test_deux_ajouts_identiques_n_en_font_qu_un(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """Sans fusion, le panier se remplit de doublons à chaque tapotement."""
        for _ in range(3):
            as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")

        response = as_customer.get(cart_url(restaurant))

        assert len(response.data["lines"]) == 1
        assert response.data["lines"][0]["quantity"] == 3

    def test_deux_cuissons_differentes_restent_deux_lignes(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
        option: Option,
    ) -> None:
        saignant = Option.objects.create(
            group=option_group, name="Saignant", price_delta=Money(0, XOF)
        )

        for choix in (option, saignant):
            as_customer.post(
                lines_url(restaurant),
                {"menu_item": str(menu_item.pk), "options": [str(choix.pk)]},
                format="json",
            )

        assert len(as_customer.get(cart_url(restaurant)).data["lines"]) == 2

    def test_une_note_differente_separe_les_lignes(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")
        as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(menu_item.pk), "notes": "Sans oignon"},
            format="json",
        )

        assert len(as_customer.get(cart_url(restaurant)).data["lines"]) == 2


class TestModification:
    @pytest.fixture
    def ligne(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> CartLine:
        as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")
        return CartLine.objects.get()

    def test_la_quantite_se_change(
        self, as_customer: APIClient, restaurant: Restaurant, ligne: CartLine
    ) -> None:
        response = as_customer.patch(
            line_url(restaurant, str(ligne.pk)), {"quantity": 4}, format="json"
        )

        assert response.data["lines"][0]["quantity"] == 4
        assert response.data["subtotal"] == {"amount": "14000", "currency": XOF}

    def test_une_quantite_nulle_est_refusee(
        self, as_customer: APIClient, restaurant: Restaurant, ligne: CartLine
    ) -> None:
        """Retirer un article se fait par `DELETE`, pas par une quantité zéro
        qui laisserait une ligne fantôme en base."""
        response = as_customer.patch(
            line_url(restaurant, str(ligne.pk)), {"quantity": 0}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_la_ligne_se_supprime(
        self, as_customer: APIClient, restaurant: Restaurant, ligne: CartLine
    ) -> None:
        response = as_customer.delete(line_url(restaurant, str(ligne.pk)))

        assert response.data["lines"] == []

    def test_le_panier_se_vide(
        self, as_customer: APIClient, restaurant: Restaurant, ligne: CartLine
    ) -> None:
        response = as_customer.delete(lines_url(restaurant))

        assert response.data["lines"] == []

    def test_la_ligne_d_autrui_est_introuvable(
        self,
        client: APIClient,
        courier_user: User,
        restaurant: Restaurant,
        ligne: CartLine,
    ) -> None:
        """Un identifiant de ligne deviné ne doit pas donner prise sur le
        panier d'un autre client."""
        client.force_authenticate(courier_user)

        response = client.patch(
            line_url(restaurant, str(ligne.pk)), {"quantity": 99}, format="json"
        )

        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        ligne.refresh_from_db()
        assert ligne.quantity == 1


class TestArticleDevenuIndisponible:
    def test_la_ligne_est_marquee_et_le_panier_bloque(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """L'article était disponible à l'ajout ; il ne l'est plus. Le panier
        le dit au lieu de laisser la commande échouer plus loin."""
        as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        response = as_customer.get(cart_url(restaurant))

        assert response.data["is_orderable"] is False
        assert response.data["lines"][0]["is_orderable"] is False
        assert "indisponible" in response.data["lines"][0]["unavailable_reason"]

    def test_un_article_retire_du_menu_le_dit_autrement(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """« Plus au menu » et « momentanément indisponible » n'appellent pas
        le même geste du client."""
        as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")
        menu_item.delete()

        response = as_customer.get(cart_url(restaurant))

        assert "plus au menu" in response.data["lines"][0]["unavailable_reason"]


class TestPrixSuivantLeCatalogue:
    def test_un_panier_oublie_affiche_le_prix_du_jour(
        self, as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """C1 — l'implémentation précédente facturait le tarif figé à
        l'ajout, parfois vieux d'une semaine."""
        as_customer.post(lines_url(restaurant), {"menu_item": str(menu_item.pk)}, format="json")

        menu_item.price = Money(4_000, XOF)
        menu_item.save()

        response = as_customer.get(cart_url(restaurant))

        assert response.data["subtotal"] == {"amount": "4000", "currency": XOF}
