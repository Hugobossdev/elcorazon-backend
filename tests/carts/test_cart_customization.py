"""Personnalisation d'une ligne de panier — composition, modification, bornes.

Le fichier voisin (`test_cart_api.py`) couvre l'ajout : bornes de groupe, prix
relu au catalogue, fusion de deux lignes identiques. Celui-ci couvre les deux
gestes qui manquaient au contrat — **retenir une option déjà indisponible**, que
le panier acceptait pour ne la refuser qu'à la commande, et **rouvrir une ligne
pour changer ses choix**, qui obligeait le client à la supprimer puis à la
recomposer, et la perdait si le second appel échouait.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.carts.models import CartLine
from apps.catalog.models import MenuItem, Option, OptionGroup
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


def lines_url(restaurant: Restaurant) -> str:
    return reverse("v1:carts:cart-add-line", args=[restaurant.slug])


def line_url(restaurant: Restaurant, line_id: str) -> str:
    return reverse("v1:carts:cart-update-line", args=[restaurant.slug, line_id])


@pytest.fixture
def cuissons(option_group: OptionGroup) -> tuple[Option, Option]:
    """« Cuisson », min 1 / max 1 — le groupe obligatoire des fixtures."""
    a_point = Option.objects.create(group=option_group, name="À point", price_delta=Money(0, XOF))
    bien_cuit = Option.objects.create(
        group=option_group, name="Bien cuit", price_delta=Money(300, XOF)
    )
    return a_point, bien_cuit


def compose(
    as_customer: APIClient, restaurant: Restaurant, menu_item: MenuItem, option: Option
) -> str:
    """Ajoute une ligne portant cette option et rend son identifiant.

    La ligne est retrouvée **par son option** et non prise en tête de liste :
    l'ajout rend le panier entier, ordonné par date de création, si bien que
    `lines[0]` désigne la plus ancienne — la précédente, pas celle qu'on vient
    d'ajouter.
    """
    response = as_customer.post(
        lines_url(restaurant),
        {"menu_item": str(menu_item.pk), "options": [str(option.pk)]},
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED

    ajoutees = [
        ligne
        for ligne in response.data["lines"]
        if [str(retenue["id"]) for retenue in ligne["options"]] == [str(option.pk)]
    ]
    assert len(ajoutees) == 1, "l'option devait désigner une ligne et une seule"
    return str(ajoutees[0]["id"])


class TestOptionIndisponible:
    """Une option éteinte au catalogue ne se retient pas.

    Le panier savait déjà *marquer* une ligne dont une option s'était éteinte
    après l'ajout — c'est le cas qu'on ne peut pas empêcher. Il acceptait en
    revanche d'en retenir une déjà indisponible, et le client ne l'apprenait
    qu'au moment de commander, devant un panier qu'il fallait défaire.
    """

    def test_retenir_une_option_indisponible_est_refuse(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
    ) -> None:
        epuisee = Option.objects.create(
            group=option_group,
            name="Saignant",
            price_delta=Money(0, XOF),
            is_available=False,
        )

        response = as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(menu_item.pk), "options": [str(epuisee.pk)]},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not CartLine.objects.exists()

    def test_une_option_disponible_du_meme_groupe_passe(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        option_group: OptionGroup,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Le refus porte sur l'option retenue, pas sur le groupe : une
        rupture sur « Saignant » ne doit pas rendre le plat incommandable."""
        Option.objects.create(
            group=option_group, name="Saignant", price_delta=Money(0, XOF), is_available=False
        )
        a_point, _ = cuissons

        response = as_customer.post(
            lines_url(restaurant),
            {"menu_item": str(menu_item.pk), "options": [str(a_point.pk)]},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED


class TestModificationDeLaPersonnalisation:
    def test_la_cuisson_se_change_et_le_prix_suit(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne), {"options": [str(bien_cuit.pk)]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        rendue = response.data["lines"][0]
        assert [option["name"] for option in rendue["options"]] == ["Bien cuit"]
        # 3 500 au catalogue + 300 de supplément, recomposés serveur (C1) :
        # aucun montant n'a traversé le réseau depuis le client.
        assert rendue["unit_price"] == {"amount": "3800", "currency": XOF}

    def test_la_quantite_seule_laisse_la_personnalisation(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Le « + » du panier ne connaît pas les options de la ligne : ne pas
        les envoyer doit les laisser en place, et non les effacer."""
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(line_url(restaurant, ligne), {"quantity": 3}, format="json")

        rendue = response.data["lines"][0]
        assert rendue["quantity"] == 3
        assert [option["name"] for option in rendue["options"]] == ["À point"]

    def test_la_quantite_se_change_en_meme_temps_que_les_options(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne),
            {"options": [str(bien_cuit.pk)], "quantity": 2},
            format="json",
        )

        rendue = response.data["lines"][0]
        assert rendue["quantity"] == 2
        assert rendue["total"] == {"amount": "7600", "currency": XOF}

    def test_le_plafond_est_revalide_a_la_modification(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """« Cuisson » n'accepte qu'un choix : la modification ne doit pas être
        une porte dérobée sur les bornes."""
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne),
            {"options": [str(a_point.pk), str(bien_cuit.pk)]},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_retirer_le_choix_d_un_groupe_obligatoire_est_refuse(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(line_url(restaurant, ligne), {"options": []}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_une_option_d_un_autre_article_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """L'attaque la plus directe : emprunter l'option bon marché d'un autre
        plat pour se composer un prix."""
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        autre = MenuItem.objects.create(
            restaurant=restaurant,
            category=menu_item.category,
            name="Poisson",
            slug="poisson",
            price=Money(2_500, XOF),
        )
        ailleurs = Option.objects.create(
            group=OptionGroup.objects.create(
                menu_item=autre, name="Sauce", min_select=0, max_select=1
            ),
            name="Piment",
            price_delta=Money(-3_000, XOF),
        )

        response = as_customer.patch(
            line_url(restaurant, ligne), {"options": [str(ailleurs.pk)]}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_une_option_devenue_indisponible_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)
        Option.objects.filter(pk=bien_cuit.pk).update(is_available=False)

        response = as_customer.patch(
            line_url(restaurant, ligne), {"options": [str(bien_cuit.pk)]}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_deux_lignes_devenues_identiques_fusionnent(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Modifier la seconde pour qu'elle rejoigne la première laisse une
        ligne de quantité 2 : deux lignes que rien ne distingue plus n'ont pas
        de raison de rester deux."""
        a_point, bien_cuit = cuissons
        compose(as_customer, restaurant, menu_item, a_point)
        seconde = compose(as_customer, restaurant, menu_item, bien_cuit)

        response = as_customer.patch(
            line_url(restaurant, seconde), {"options": [str(a_point.pk)]}, format="json"
        )

        assert len(response.data["lines"]) == 1
        assert response.data["lines"][0]["quantity"] == 2

    def test_la_ligne_reecrite_ne_fusionne_pas_avec_elle_meme(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Réenregistrer sans rien changer garde une ligne de quantité 1 : ses
        options viennent d'être réécrites, et elle se reconnaîtrait comme sa
        propre jumelle si la recherche ne l'écartait pas."""
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne), {"options": [str(a_point.pk)]}, format="json"
        )

        assert len(response.data["lines"]) == 1
        assert response.data["lines"][0]["quantity"] == 1

    def test_la_note_se_change_seule(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne), {"notes": "Sans oignons"}, format="json"
        )

        rendue = response.data["lines"][0]
        assert rendue["notes"] == "Sans oignons"
        assert [option["name"] for option in rendue["options"]] == ["À point"]

    def test_une_modification_vide_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Un corps vide ne dit pas ce qu'il faut changer ; l'accepter
        laisserait croire à une modification qui n'a pas eu lieu."""
        a_point, _ = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(line_url(restaurant, ligne), {}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_la_ligne_d_autrui_reste_introuvable(
        self,
        client: APIClient,
        courier_user: User,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """Un identifiant de ligne deviné ne doit pas donner prise sur la
        personnalisation d'un autre client."""
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)
        client.force_authenticate(courier_user)

        response = client.patch(
            line_url(restaurant, ligne), {"options": [str(bien_cuit.pk)]}, format="json"
        )

        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        assert CartLine.objects.get(pk=ligne).options.get().option_id == a_point.pk

    def test_le_prix_envoye_a_la_modification_est_ignore(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        menu_item: MenuItem,
        cuissons: tuple[Option, Option],
    ) -> None:
        """C1 sur le chemin de la modification aussi : le champ n'existe ni au
        contrat ni en base, il n'y a donc rien à valider et rien à oublier."""
        a_point, bien_cuit = cuissons
        ligne = compose(as_customer, restaurant, menu_item, a_point)

        response = as_customer.patch(
            line_url(restaurant, ligne),
            {
                "options": [str(bien_cuit.pk)],
                "unit_price": {"amount": "1", "currency": XOF},
                "price": {"amount": "1", "currency": XOF},
            },
            format="json",
        )

        assert response.data["lines"][0]["unit_price"] == {"amount": "3800", "currency": XOF}
