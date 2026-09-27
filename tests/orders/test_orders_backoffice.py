"""Supervision des commandes — ADR-005, troisième étage.

Cette suite porte deux régressions que la rédaction précédente laissait
passer, et c'est pour elles que le module `backoffice.py` existe :

* `orders.read` n'était appliquée nulle part. Le registre la déclare et les
  rôles la distribuent, mais aucune route ne l'exigeait : un compte du
  personnel privé de ce droit lisait quand même les commandes de son
  établissement. `test_sans_orders_read_rien_n_est_lisible` est le test qui le
  constate ;
* le verbe d'annulation du client était joignable par le livreur, dont le
  `get_queryset` d'alors rendait les commandes qu'il transportait —
  `test_le_livreur_n_annule_pas_la_commande_qu_il_transporte`.

Le reste vérifie ce que la séparation permet d'exiger : une permission par
verbe, un cloisonnement par établissement, et un motif d'annulation qui rend le
journal exploitable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import Assignment, CourierProfile
from apps.geography.models import City, DeliveryZone
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def membre(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    """Membre du personnel muni de permissions et, éventuellement, d'un périmètre."""
    user = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=user, restaurant=restaurant)
    return user


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def superviseur(restaurant: Restaurant) -> APIClient:
    """Le poste courant : il voit le service, l'avance et l'annule."""
    return connecte(
        membre(
            "superviseur@elcorazon.test",
            restaurant,
            "orders.read",
            "orders.update_status",
            "orders.cancel",
        )
    )


@pytest.fixture
def lecteur(restaurant: Restaurant) -> APIClient:
    """Consulte le service sans y toucher."""
    return connecte(membre("lecteur@elcorazon.test", restaurant, "orders.read"))


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


def avancer(client: APIClient, order: Order, cible: str) -> object:
    return client.post(
        reverse("v1:orders:managed-order-status", args=[order.pk]),
        {"status": cible},
        format="json",
    )


def annuler(client: APIClient, order: Order, **corps: object) -> object:
    return client.post(
        reverse("v1:orders:managed-order-cancel", args=[order.pk]), corps, format="json"
    )


class TestUnePermissionParVerbe:
    def test_sans_orders_read_rien_n_est_lisible(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        """La régression que ce module corrige.

        Avant la séparation, la supervision partageait les routes du client et
        n'exigeait que l'authentification : ce compte-là — du personnel, muni
        du droit d'avancer un statut mais pas de celui de lire — voyait
        pourtant toutes les commandes de son établissement.
        """
        sans_lecture = connecte(
            membre("sans-lecture@elcorazon.test", restaurant, "orders.update_status")
        )

        assert (
            sans_lecture.get(reverse("v1:orders:managed-order-list")).status_code
            == status.HTTP_403_FORBIDDEN
        )

    def test_un_client_n_atteint_pas_la_supervision(self, customer: User, order: Order) -> None:
        """Le refus est le défaut : être authentifié n'est pas être du personnel."""
        response = connecte(customer).get(reverse("v1:orders:managed-order-list"))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_lire_ne_donne_pas_le_droit_d_avancer(self, lecteur: APIClient, order: Order) -> None:
        liste = lecteur.get(reverse("v1:orders:managed-order-list"))

        assert liste.status_code == status.HTTP_200_OK
        assert avancer(lecteur, order, OrderStatus.CONFIRMED).status_code == (
            status.HTTP_403_FORBIDDEN
        )

    def test_avancer_ne_donne_pas_le_droit_d_annuler(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        """Le registre distingue `orders.update_status` de `orders.cancel`
        parce que l'exploitation les distingue : faire avancer le service est
        le geste de tous les jours, annuler la commande d'un tiers ne l'est
        pas."""
        operateur = connecte(
            membre("operateur@elcorazon.test", restaurant, "orders.read", "orders.update_status")
        )

        assert annuler(operateur, order, reason="Rupture").status_code == status.HTTP_403_FORBIDDEN
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_l_annulation_ne_passe_pas_par_la_route_des_statuts(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        """`cancelled` est une cible légitime de la machine à états, ce qui
        ferait de `orders.update_status` un droit d'annuler si la route la
        laissait passer — et viderait `orders.cancel` de son sens."""
        operateur = connecte(
            membre("contournement@elcorazon.test", restaurant, "orders.update_status")
        )

        response = avancer(operateur, order, OrderStatus.CANCELLED)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "orders.cancel" in response.data["detail"]
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING


class TestSeparationDesPublics:
    def test_le_livreur_n_annule_pas_la_commande_qu_il_transporte(
        self, courier: CourierProfile, courier_user: User, order: Order
    ) -> None:
        """La seconde régression.

        L'annulation client n'avait pas de `permission_classes` propre — donc
        « authentifié » — et le `get_queryset` rendait au livreur les commandes
        qui lui étaient confiées. Il pouvait annuler celle qu'il était en train
        de livrer, et le service ne l'aurait pas arrêté : `cancel_by_customer`
        ne regarde que le statut.
        """
        Assignment.objects.create(order=order, courier=courier)

        response = connecte(courier_user).post(
            reverse("v1:orders:order-cancel", args=[order.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_le_personnel_n_a_plus_de_vue_elargie_sur_la_route_client(
        self, restaurant: Restaurant, order: Order
    ) -> None:
        """Un chemin, un public : `/orders/` répond « vos commandes » à qui
        l'appelle, sans exception. La supervision a `manage/`, où elle présente
        `orders.read`."""
        superviseur = connecte(
            membre("elargie@elcorazon.test", restaurant, "orders.read", "orders.update_status")
        )

        response = superviseur.get(reverse("v1:orders:order-list"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 0


class TestCloisonnement:
    def test_une_commande_hors_perimetre_est_introuvable(
        self, superviseur: APIClient, autre_restaurant: Restaurant, customer: User
    ) -> None:
        """Introuvable et non interdite. Sur des identifiants UUID la nuance
        est mince ; elle compte sur `reference`, qui est séquentielle."""
        ailleurs = build_order(autre_restaurant, customer, reference="EC000042")

        liste = superviseur.get(reverse("v1:orders:managed-order-list"))
        fiche = superviseur.get(reverse("v1:orders:managed-order-detail", args=[ailleurs.pk]))

        assert [o["id"] for o in liste.data["results"]] == []
        assert fiche.status_code == status.HTTP_404_NOT_FOUND

    def test_un_membre_sans_rattachement_ne_voit_rien(self, order: Order) -> None:
        """Un oubli de configuration produit une panne visible, jamais un accès
        trop large et silencieux."""
        orphelin = connecte(membre("orphelin@elcorazon.test", None, "orders.read"))

        assert orphelin.get(reverse("v1:orders:managed-order-list")).data["count"] == 0

    def test_le_superutilisateur_voit_l_enseigne_entiere(
        self, autre_restaurant: Restaurant, customer: User, order: Order
    ) -> None:
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
        build_order(autre_restaurant, customer, reference="EC000042")

        assert connecte(siege).get(reverse("v1:orders:managed-order-list")).data["count"] == 2

    def test_un_statut_ne_s_avance_pas_hors_perimetre(
        self, superviseur: APIClient, autre_restaurant: Restaurant, customer: User
    ) -> None:
        """Le filtre de requête sert aussi les écritures : `get_object` puise
        dans le même `get_queryset`, sans quoi la lecture serait cloisonnée et
        l'écriture pas."""
        ailleurs = build_order(autre_restaurant, customer, reference="EC000042")

        assert avancer(superviseur, ailleurs, OrderStatus.CONFIRMED).status_code == (
            status.HTTP_404_NOT_FOUND
        )


class TestAnnulationParLExploitation:
    def test_le_motif_est_obligatoire(self, superviseur: APIClient, order: Order) -> None:
        """Asymétrie voulue avec l'annulation client : l'opérateur annule la
        commande d'un tiers, qui sera remboursé et rappellera pour comprendre.
        Un journal sans motif ne répond pas à cette question."""
        vide = annuler(superviseur, order)
        blanc = annuler(superviseur, order, reason="")

        assert vide.status_code == status.HTTP_400_BAD_REQUEST
        assert blanc.status_code == status.HTTP_400_BAD_REQUEST
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_l_exploitation_annule_ce_que_le_client_ne_peut_plus(
        self, superviseur: APIClient, order: Order
    ) -> None:
        """Le cas qui motive le verbe : la rupture découverte en cuisine. Le
        client n'a plus la main passé la confirmation — sans cette route, la
        commande restait en préparation jusqu'à une livraison qui n'aurait pas
        lieu."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PREPARING)

        response = annuler(superviseur, order, reason="Rupture de poulet")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == OrderStatus.CANCELLED
        assert response.data["cancellation_reason"] == "Rupture de poulet"
        assert response.data["cancelled_at"] is not None

    def test_le_motif_est_journalise(self, superviseur: APIClient, order: Order) -> None:
        annuler(superviseur, order, reason="Client injoignable")

        evenements = order.status_events.all()
        assert [(e.from_status, e.to_status, e.reason) for e in evenements] == [
            (OrderStatus.PENDING, OrderStatus.CANCELLED, "Client injoignable")
        ]

    def test_apres_l_enlevement_la_machine_refuse(
        self, superviseur: APIClient, order: Order
    ) -> None:
        """Le repas est parti : un incident après ce point relève du
        remboursement, pas de l'annulation. La règle vit dans la table de
        transitions, et la route ne la redouble pas."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PICKED_UP)

        response = annuler(superviseur, order, reason="Trop tard")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "illegal_transition"


class TestEcranDeService:
    def test_la_liste_se_borne_au_service_en_cours(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, order: Order
    ) -> None:
        """L'écran demande toujours la même chose — « ce qui se passe
        maintenant » — et la seule façon de l'exprimer sans borne serait de
        tout charger pour n'en afficher que la fin."""
        veille = build_order(restaurant, customer, reference="EC000042")
        Order.objects.filter(pk=veille.pk).update(placed_at=timezone.now() - timedelta(days=1))

        response = superviseur.get(
            reverse("v1:orders:managed-order-list"),
            {"placed_at__gte": (timezone.now() - timedelta(hours=6)).isoformat()},
        )

        assert [o["id"] for o in response.data["results"]] == [str(order.pk)]

    def test_la_liste_se_filtre_par_statut(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, order: Order
    ) -> None:
        en_cuisine = build_order(restaurant, customer, reference="EC000042")
        Order.objects.filter(pk=en_cuisine.pk).update(status=OrderStatus.PREPARING)

        response = superviseur.get(
            reverse("v1:orders:managed-order-list"), {"status": OrderStatus.PREPARING}
        )

        assert [o["id"] for o in response.data["results"]] == [str(en_cuisine.pk)]

    def test_la_fiche_client_liste_son_historique(
        self, superviseur: APIClient, restaurant: Restaurant, customer: User, order: Order
    ) -> None:
        """« L'historique de cette personne », que l'opérateur consulte au
        téléphone pendant qu'elle réclame."""
        autre = User.objects.create_user("bea@elcorazon.test", "motdepasse", full_name="Béa")
        build_order(restaurant, autre, reference="EC000042")

        response = superviseur.get(
            reverse("v1:orders:managed-order-list"), {"customer": str(customer.pk)}
        )

        assert [o["id"] for o in response.data["results"]] == [str(order.pk)]

    def test_la_fiche_porte_le_journal_et_les_lignes(
        self, superviseur: APIClient, order: Order
    ) -> None:
        avancer(superviseur, order, OrderStatus.CONFIRMED)

        fiche = superviseur.get(reverse("v1:orders:managed-order-detail", args=[order.pk]))

        assert fiche.status_code == status.HTTP_200_OK
        assert fiche.data["allowed_transitions"] == ["cancelled", "preparing"]
        assert [e["to_status"] for e in fiche.data["status_events"]] == [OrderStatus.CONFIRMED]
