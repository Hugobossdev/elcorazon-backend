"""Administration du catalogue — ADR-005, troisième étage.

Deux clés, pas une : la permission dit *ce qu'on a le droit de faire*, le
rattachement dit **sur quoi**. Les tests qui portent ce module sont ceux du
cloisonnement — `test_un_article_hors_perimetre_est_introuvable` et
`test_une_creation_hors_perimetre_est_refusee` —, parce que c'est exactement la
distinction que l'implémentation précédente n'avait pas : ses rôles admin
n'étaient appliqués que côté interface.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.geography.models import City, DeliveryZone
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def gerant(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    """Membre du personnel muni de permissions et, éventuellement, d'un périmètre."""
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def redacteur(restaurant: Restaurant) -> APIClient:
    """Personnel qui tient la carte de son établissement."""
    client = APIClient()
    client.force_authenticate(
        gerant("carte@elcorazon.test", restaurant, "catalog.read", "catalog.write")
    )
    return client


@pytest.fixture
def lecteur(restaurant: Restaurant) -> APIClient:
    """Opérateur : consulte la carte, ne la modifie pas."""
    client = APIClient()
    client.force_authenticate(gerant("operateur@elcorazon.test", restaurant, "catalog.read"))
    return client


@pytest.fixture
def autre_restaurant(city: City, zone: DeliveryZone) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=zone,
        address="Kara",
        location=zone.city.centroid,
        phone="+22890000001",
    )


def article(
    client: APIClient, restaurant: Restaurant, category: Category, **extra: object
) -> object:
    corps: dict[str, object] = {
        "restaurant": restaurant.slug,
        "category": str(category.pk),
        "name": "Poulet braisé",
        "slug": "poulet-braise",
        "price": {"amount": "2500", "currency": XOF},
    }
    corps.update(extra)
    return client.post(reverse("v1:catalog:managed-item-list"), corps, format="json")


class TestDeuxPermissionsDistinctes:
    def test_sans_permission_rien_n_est_lisible(self, customer: User) -> None:
        """Le refus est le défaut : un client authentifié n'est pas du personnel."""
        client = APIClient()
        client.force_authenticate(customer)

        response = client.get(reverse("v1:catalog:managed-item-list"))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_lire_ne_donne_pas_le_droit_d_ecrire(
        self, lecteur: APIClient, restaurant: Restaurant, category: Category, menu_item: MenuItem
    ) -> None:
        """`catalog.read` et `catalog.write` sont deux permissions, et la
        distinction porte : un opérateur consulte la carte sans pouvoir changer
        un prix."""
        assert (
            lecteur.get(reverse("v1:catalog:managed-item-list")).status_code == status.HTTP_200_OK
        )

        refuse = article(lecteur, restaurant, category)

        assert refuse.status_code == status.HTTP_403_FORBIDDEN

    def test_ecrire_cree_l_article_au_prix_annonce(
        self, redacteur: APIClient, restaurant: Restaurant, category: Category
    ) -> None:
        response = article(redacteur, restaurant, category)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["price"] == {"amount": "2500", "currency": XOF}
        assert MenuItem.objects.get(slug="poulet-braise").price == Money(2_500, XOF)


class TestCloisonnement:
    def test_un_article_hors_perimetre_est_introuvable(
        self, redacteur: APIClient, autre_restaurant: Restaurant, category: Category
    ) -> None:
        """Introuvable et non interdit : un 403 confirmerait l'existence de
        l'article à qui a deviné son identifiant."""
        ailleurs = MenuItem.objects.create(
            restaurant=autre_restaurant,
            category=Category.objects.create(
                restaurant=autre_restaurant, name="Grillades", slug="grillades"
            ),
            name="Brochettes",
            slug="brochettes",
            price=Money(1_500, XOF),
        )

        liste = redacteur.get(reverse("v1:catalog:managed-item-list"))
        fiche = redacteur.get(reverse("v1:catalog:managed-item-detail", args=[ailleurs.pk]))

        assert [i["id"] for i in liste.data["results"]] == []
        assert fiche.status_code == status.HTTP_404_NOT_FOUND

    def test_une_creation_hors_perimetre_est_refusee(
        self, redacteur: APIClient, autre_restaurant: Restaurant
    ) -> None:
        """Une création désigne son établissement dans le corps : aucun
        `get_object` ne peut la filtrer, il faut une garde explicite."""
        categorie = Category.objects.create(
            restaurant=autre_restaurant, name="Grillades", slug="grillades"
        )

        response = article(redacteur, autre_restaurant, categorie)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not MenuItem.objects.filter(slug="poulet-braise").exists()

    def test_un_membre_sans_rattachement_ne_voit_rien(
        self, menu_item: MenuItem, restaurant: Restaurant
    ) -> None:
        """Un oubli de configuration produit une panne visible, jamais un accès
        trop large et silencieux."""
        client = APIClient()
        client.force_authenticate(gerant("orphelin@elcorazon.test", None, "catalog.read"))

        assert client.get(reverse("v1:catalog:managed-item-list")).data["count"] == 0


class TestCoherenceDesEcritures:
    def test_une_categorie_d_un_autre_etablissement_est_refusee(
        self, redacteur: APIClient, restaurant: Restaurant, autre_restaurant: Restaurant
    ) -> None:
        """Sans cette garde, un article se range dans une catégorie d'ailleurs
        et disparaît de sa propre carte."""
        categorie = Category.objects.create(
            restaurant=autre_restaurant, name="Grillades", slug="grillades"
        )

        response = article(redacteur, restaurant, categorie)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "category" in response.data["errors"] or "category" in str(response.data)

    def test_un_prix_dans_une_autre_devise_est_refuse(
        self, redacteur: APIClient, restaurant: Restaurant, category: Category
    ) -> None:
        """La devise est héritée du pays (ADR-006). Acceptée ici, l'incohérence
        n'apparaîtrait qu'à l'addition au panier, c'est-à-dire chez le client."""
        response = article(
            redacteur, restaurant, category, price={"amount": "2500", "currency": "EUR"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_la_note_moyenne_ne_s_ecrit_pas(
        self, redacteur: APIClient, menu_item: MenuItem
    ) -> None:
        """Un agrégat calculé depuis les avis : le rendre inscriptible
        permettrait de fabriquer une note."""
        redacteur.patch(
            reverse("v1:catalog:managed-item-detail", args=[menu_item.pk]),
            {"rating_average": "5.00", "rating_count": 999},
            format="json",
        )

        menu_item.refresh_from_db()
        assert menu_item.rating_count == 0


class TestArchivage:
    def test_supprimer_archive_au_lieu_d_effacer(
        self, redacteur: APIClient, menu_item: MenuItem
    ) -> None:
        """Des commandes passées y renvoient : un effacement réel rendrait un
        historique illisible."""
        response = redacteur.delete(reverse("v1:catalog:managed-item-detail", args=[menu_item.pk]))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        menu_item.refresh_from_db()
        assert menu_item.is_deleted

    def test_un_article_archive_se_retrouve_et_se_restaure(
        self, redacteur: APIClient, menu_item: MenuItem
    ) -> None:
        menu_item.delete()

        vivants = redacteur.get(reverse("v1:catalog:managed-item-list"))
        archives = redacteur.get(reverse("v1:catalog:managed-item-list"), {"archived": "true"})
        restaure = redacteur.post(
            reverse("v1:catalog:managed-item-restore", args=[menu_item.pk]), {}, format="json"
        )

        assert vivants.data["count"] == 0
        assert archives.data["count"] == 1
        assert restaure.status_code == status.HTTP_200_OK
        menu_item.refresh_from_db()
        assert not menu_item.is_deleted


class TestGroupesEtOptions:
    def test_un_groupe_porte_ses_bornes(self, redacteur: APIClient, menu_item: MenuItem) -> None:
        """« 2 accompagnements parmi 5 » se crée en donnée, sans développement."""
        response = redacteur.post(
            reverse("v1:catalog:managed-option-group-list"),
            {
                "menu_item": str(menu_item.pk),
                "name": "Accompagnements",
                "min_select": 0,
                "max_select": 2,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert OptionGroup.objects.get(name="Accompagnements").max_select == 2

    def test_un_ecart_de_prix_negatif_est_accepte(
        self, redacteur: APIClient, option_group: OptionGroup
    ) -> None:
        """« Sans fromage, −200 F »."""
        response = redacteur.post(
            reverse("v1:catalog:managed-option-list"),
            {
                "group": str(option_group.pk),
                "name": "Sans fromage",
                "price_delta": {"amount": "-200", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Option.objects.get(name="Sans fromage").price_delta == Money(-200, XOF)

    def test_une_option_hors_perimetre_est_refusee(
        self, redacteur: APIClient, autre_restaurant: Restaurant
    ) -> None:
        ailleurs = MenuItem.objects.create(
            restaurant=autre_restaurant,
            category=Category.objects.create(
                restaurant=autre_restaurant, name="Grillades", slug="grillades"
            ),
            name="Brochettes",
            slug="brochettes",
            price=Money(1_500, XOF),
        )
        groupe = OptionGroup.objects.create(menu_item=ailleurs, name="Sauce")

        response = redacteur.post(
            reverse("v1:catalog:managed-option-list"),
            {
                "group": str(groupe.pk),
                "name": "Piment",
                "price_delta": {"amount": "0", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestInventaire:
    def test_le_stock_se_fixe_en_valeur_absolue(
        self, redacteur: APIClient, menu_item: MenuItem
    ) -> None:
        """Un delta rejoué par un réseau capricieux ajouterait deux fois ; une
        valeur absolue rejouée écrit deux fois la même chose."""
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=3)
        url = reverse("v1:catalog:managed-item-stock", args=[menu_item.pk])

        redacteur.post(url, {"stock_quantity": 12}, format="json")
        redacteur.post(url, {"stock_quantity": 12}, format="json")

        menu_item.refresh_from_db()
        assert menu_item.stock_quantity == 12

    def test_un_stock_negatif_est_refuse(self, redacteur: APIClient, menu_item: MenuItem) -> None:
        response = redacteur.post(
            reverse("v1:catalog:managed-item-stock", args=[menu_item.pk]),
            {"stock_quantity": -1},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
