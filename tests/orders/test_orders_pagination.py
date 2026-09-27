"""Pagination et recherche de la supervision — `GET /orders/manage/`.

Ces deux mécanismes existaient sur le papier et **aucun des deux ne
fonctionnait**, chacun pour une raison différente :

* la pagination fonctionnait côté serveur, mais aucun appelant ne s'en servait :
  le dépôt Dart suivait `next` jusqu'au bout et rendait la liste entière. À dix
  mille commandes, l'écran de supervision téléchargeait cinq cents pages avant
  d'afficher sa première ligne ;
* la recherche, elle, était **silencieusement ignorée**. `SearchFilter` n'était
  pas monté dans `DEFAULT_FILTER_BACKENDS` : DRF acceptait `?search=`, ne
  l'appliquait pas, et rendait 200 avec la liste complète. C'est la panne la
  plus discrète de cette famille — le seul symptôme est un résultat trop large,
  qu'on prend pour un jeu de données de test.

D'où des cas qui vérifient qu'un terme **introuvable rend zéro**. Un test qui se
contenterait de chercher un terme présent passerait tout aussi bien sans filtre.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

URL = "v1:orders:managed-order-list"


@pytest.fixture
def superviseur(restaurant: Restaurant) -> APIClient:
    membre = User.objects.create_user(
        "supervision@elcorazon.test", "motdepasse", full_name="Kossi", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name="Supervision", permissions=["orders.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


@pytest.fixture
def vingt_cinq_commandes(restaurant: Restaurant, customer: User) -> list[Order]:
    """Assez pour déborder la page par défaut (20) — le seuil est le sujet."""
    return [build_order(restaurant, customer, reference=f"EC{index:06d}") for index in range(1, 26)]


class TestPagination:
    def test_la_premiere_page_est_bornee_et_annonce_la_suite(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        reponse = superviseur.get(reverse(URL))

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["count"] == 25
        assert len(reponse.data["results"]) == 20
        assert reponse.data["next"] is not None
        assert reponse.data["previous"] is None

    def test_la_page_suivante_rend_le_reste_et_sait_revenir(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        reponse = superviseur.get(reverse(URL), {"page": 2})

        assert len(reponse.data["results"]) == 5
        assert reponse.data["next"] is None
        assert reponse.data["previous"] is not None

    def test_les_pages_ne_se_recouvrent_pas(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        """Un doublon entre deux pages est le défaut classique d'une pagination
        sur un tri non déterministe. L'ordre est `-placed_at`, et les
        commandes de ce test sont créées dans la même seconde."""
        page1 = superviseur.get(reverse(URL)).data["results"]
        page2 = superviseur.get(reverse(URL), {"page": 2}).data["results"]

        identifiants = [str(ligne["id"]) for ligne in page1 + page2]
        assert len(identifiants) == len(set(identifiants)) == 25

    def test_la_taille_de_page_se_choisit(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        reponse = superviseur.get(reverse(URL), {"page_size": 5})

        assert len(reponse.data["results"]) == 5

    def test_la_taille_de_page_est_plafonnee(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        """`?page_size=100000` est un déni de service à un paramètre."""
        reponse = superviseur.get(reverse(URL), {"page_size": 100_000})

        assert len(reponse.data["results"]) <= 100

    def test_une_page_hors_bornes_repond_404(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        reponse = superviseur.get(reverse(URL), {"page": 99})

        assert reponse.status_code == status.HTTP_404_NOT_FOUND

    def test_sans_commande_la_reponse_est_vide_et_non_une_erreur(
        self, superviseur: APIClient
    ) -> None:
        reponse = superviseur.get(reverse(URL))

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["count"] == 0
        assert reponse.data["results"] == []


class TestRecherche:
    def test_un_terme_introuvable_rend_zero(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        """**Le test décisif.** Sans `SearchFilter` monté, DRF ignorait
        `?search=` et rendait les vingt-cinq commandes, en 200."""
        reponse = superviseur.get(reverse(URL), {"search": "zzz-introuvable"})

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["count"] == 0

    def test_la_recherche_porte_sur_la_reference(
        self, superviseur: APIClient, vingt_cinq_commandes: list[Order]
    ) -> None:
        """C'est le numéro que le client donne au téléphone."""
        reponse = superviseur.get(reverse(URL), {"search": "EC000007"})

        assert reponse.data["count"] == 1
        assert reponse.data["results"][0]["reference"] == "EC000007"

    def test_la_recherche_porte_sur_le_destinataire(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        build_order(restaurant, customer, reference="EC000100", recipient_name="Ama Konaté")
        build_order(restaurant, customer, reference="EC000101", recipient_name="Yao Mensah")

        reponse = superviseur.get(reverse(URL), {"search": "Konaté"})

        assert reponse.data["count"] == 1
        assert reponse.data["results"][0]["recipient_name"] == "Ama Konaté"

    def test_la_recherche_porte_sur_l_adresse(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        build_order(
            restaurant,
            customer,
            reference="EC000200",
            delivery_address_line="Avenue de la Libération",
        )

        reponse = superviseur.get(reverse(URL), {"search": "Libération"})

        assert reponse.data["count"] == 1

    def test_la_recherche_porte_sur_le_telephone(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        build_order(restaurant, customer, reference="EC000300", recipient_phone="+22899001122")

        reponse = superviseur.get(reverse(URL), {"search": "99001122"})

        assert reponse.data["count"] == 1


class TestCombinaisons:
    """La pagination doit composer avec les filtres, sinon « page 2 » d'une
    recherche affiche la page 2 de tout."""

    def test_recherche_et_pagination_se_composent(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        for index in range(1, 8):
            build_order(
                restaurant, customer, reference=f"EC{index:06d}", recipient_name="Ama Konaté"
            )
        for index in range(20, 25):
            build_order(
                restaurant, customer, reference=f"EC{index:06d}", recipient_name="Yao Mensah"
            )

        reponse = superviseur.get(reverse(URL), {"search": "Konaté", "page_size": 3})

        assert reponse.data["count"] == 7
        assert len(reponse.data["results"]) == 3
        assert all("Konaté" in ligne["recipient_name"] for ligne in reponse.data["results"])

    def test_statut_et_pagination_se_composent(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        for index in range(1, 5):
            build_order(
                restaurant, customer, reference=f"EC{index:06d}", status=OrderStatus.PENDING
            )
        for index in range(10, 13):
            build_order(
                restaurant, customer, reference=f"EC{index:06d}", status=OrderStatus.DELIVERED
            )

        reponse = superviseur.get(reverse(URL), {"status": OrderStatus.PENDING, "page_size": 2})

        assert reponse.data["count"] == 4
        assert all(ligne["status"] == OrderStatus.PENDING for ligne in reponse.data["results"])

    def test_le_cloisonnement_survit_a_la_recherche(
        self, restaurant: Restaurant, customer: User, superviseur: APIClient
    ) -> None:
        """Une recherche ne doit pas être une porte de sortie du périmètre."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            phone="+22890000007",
            location=restaurant.location,
        )
        build_order(ailleurs, customer, reference="EC999999", recipient_name="Ama Konaté")
        build_order(restaurant, customer, reference="EC000001", recipient_name="Ama Konaté")

        reponse = superviseur.get(reverse(URL), {"search": "Konaté"})

        assert reponse.data["count"] == 1
        assert reponse.data["results"][0]["reference"] == "EC000001"
