"""Bibliothèque d'options réutilisables — `/catalog/manage/option-templates/`.

Le test décisif est `test_corriger_un_modele_ne_reprice_pas_les_articles` :
l'application **copie** le modèle au lieu de le référencer. Une clé étrangère
aurait fait d'un prix de bibliothèque le prix facturé de tous les articles qui
s'en servent, y compris pendant qu'un client compose son panier.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import MenuItem, Option, OptionGroup, OptionTemplate
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
LISTE = "v1:catalog:managed-option-template-list"
DETAIL = "v1:catalog:managed-option-template-detail"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def gestionnaire(restaurant: Restaurant) -> User:
    """Personnel de l'établissement, muni des droits sur le catalogue."""
    membre = User.objects.create_user(
        "carte@elcorazon.test", "motdepasse", full_name="Ama Carte", user_type=UserType.STAFF
    )
    membre.roles.add(
        Role.objects.create(name="Carte", permissions=["catalog.read", "catalog.write"])
    )
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


@pytest.fixture
def as_staff(gestionnaire: User) -> APIClient:
    authenticated = APIClient()
    authenticated.force_authenticate(gestionnaire)
    return authenticated


@pytest.fixture
def modele(restaurant: Restaurant) -> OptionTemplate:
    return OptionTemplate.objects.create(
        restaurant=restaurant,
        name="Extra fromage",
        group_name="Suppléments",
        price_delta=Money(500, XOF),
    )


def apply_url(item: MenuItem) -> str:
    return reverse("v1:catalog:managed-item-apply-template", kwargs={"pk": item.pk})


class TestBibliotheque:
    def test_le_personnel_range_une_option_reutilisable(
        self, as_staff: APIClient, restaurant: Restaurant
    ) -> None:
        response = as_staff.post(
            reverse(LISTE),
            {
                "restaurant": restaurant.slug,
                "name": "Sans oignon",
                "group_name": "Préparation",
                "price_delta": {"amount": "0", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["name"] == "Sans oignon"

    def test_deux_modeles_homonymes_dans_un_groupe_sont_refuses(
        self, as_staff: APIClient, restaurant: Restaurant, modele: OptionTemplate
    ) -> None:
        """L'opérateur ne saurait pas lequel il applique."""
        response = as_staff.post(
            reverse(LISTE),
            {
                "restaurant": restaurant.slug,
                "name": modele.name,
                "group_name": modele.group_name,
                "price_delta": {"amount": "700", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_meme_nom_dans_un_autre_groupe_est_accepte(
        self, as_staff: APIClient, restaurant: Restaurant, modele: OptionTemplate
    ) -> None:
        response = as_staff.post(
            reverse(LISTE),
            {
                "restaurant": restaurant.slug,
                "name": modele.name,
                "group_name": "Garnitures",
                "price_delta": {"amount": "500", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED


class TestApplication:
    def test_appliquer_cree_le_groupe_et_l_option(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        response = as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["name"] == "Suppléments"
        assert [o["name"] for o in response.data["options"]] == ["Extra fromage"]

    def test_le_groupe_existant_est_reutilise(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        """Le nom désigne le groupe : c'est ainsi que l'exploitation le nomme."""
        OptionGroup.objects.create(menu_item=menu_item, name="Suppléments")

        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        assert menu_item.option_groups.count() == 1

    def test_le_groupe_cible_peut_etre_impose(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        response = as_staff.post(
            apply_url(menu_item),
            {"template": str(modele.pk), "group_name": "Extras"},
            format="json",
        )

        assert response.data["name"] == "Extras"

    def test_appliquer_deux_fois_est_refuse(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")
        response = as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Option.objects.filter(name=modele.name).count() == 1

    def test_la_preselection_suit_le_modele(
        self, as_staff: APIClient, menu_item: MenuItem, restaurant: Restaurant
    ) -> None:
        """« À point » présélectionné dans la bibliothèque l'est sur l'article."""
        modele = OptionTemplate.objects.create(
            restaurant=restaurant,
            name="À point",
            group_name="Cuisson",
            price_delta=Money(0, XOF),
            is_default=True,
        )

        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        assert Option.objects.get(group__menu_item=menu_item).is_default

    def test_ni_le_prix_ni_le_nom_ne_s_ecrivent_a_l_application(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        """Sinon « appliquer un modèle » deviendrait « créer une option
        quelconque », et la bibliothèque ne garantirait plus rien."""
        as_staff.post(
            apply_url(menu_item),
            {
                "template": str(modele.pk),
                "name": "Truffe",
                "price_delta": {"amount": "9000", "currency": XOF},
            },
            format="json",
        )

        option = Option.objects.get(group__menu_item=menu_item)
        assert option.name == "Extra fromage"
        assert option.price_delta == Money(500, XOF)

    def test_l_article_du_back_office_porte_ses_groupes(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        """La forme publique les omet ; celle du back-office les porte.

        L'écran des personnalisations montre quelles options portent quels
        articles : sans ce champ, il faudrait une requête par article.
        """
        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        response = as_staff.get(
            reverse("v1:catalog:managed-item-detail", kwargs={"pk": menu_item.pk})
        )

        groupes = response.data["option_groups"]
        assert [o["name"] for g in groupes for o in g["options"]] == ["Extra fromage"]


class TestCopieEtNonReference:
    def test_corriger_un_modele_ne_reprice_pas_les_articles(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        """Le cœur du choix de conception : la bibliothèque n'est pas une source
        de prix partagée."""
        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        as_staff.patch(
            reverse(DETAIL, kwargs={"pk": modele.pk}),
            {"price_delta": {"amount": "1500", "currency": XOF}},
            format="json",
        )

        option = Option.objects.get(group__menu_item=menu_item)
        assert option.price_delta == Money(500, XOF)

    def test_supprimer_un_modele_ne_retire_rien_de_la_carte(
        self, as_staff: APIClient, menu_item: MenuItem, modele: OptionTemplate
    ) -> None:
        as_staff.post(apply_url(menu_item), {"template": str(modele.pk)}, format="json")

        as_staff.delete(reverse(DETAIL, kwargs={"pk": modele.pk}))

        assert Option.objects.filter(group__menu_item=menu_item).count() == 1


class TestCloisonnement:
    def test_on_n_applique_pas_la_bibliotheque_d_une_autre_enseigne(
        self, as_staff: APIClient, menu_item: MenuItem
    ) -> None:
        autre = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=menu_item.restaurant.zone,
            address="Kara",
            location=Point(1.19, 9.55, srid=4326),
            phone="+22890000001",
        )
        etranger = OptionTemplate.objects.create(
            restaurant=autre,
            name="Piment fort",
            group_name="Assaisonnement",
            price_delta=Money(0, XOF),
        )

        response = as_staff.post(
            apply_url(menu_item), {"template": str(etranger.pk)}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not Option.objects.filter(name="Piment fort").exists()

    def test_sans_droit_sur_le_catalogue_rien_ne_s_ecrit(
        self, client: APIClient, customer: User, restaurant: Restaurant
    ) -> None:
        client.force_authenticate(customer)

        response = client.post(
            reverse(LISTE),
            {
                "restaurant": restaurant.slug,
                "name": "Gratuit",
                "price_delta": {"amount": "0", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
