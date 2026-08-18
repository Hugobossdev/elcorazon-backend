"""Administration des comptes, des rôles et du personnel — ADR-005.

Le test qui porte ce module est
`test_on_n_accorde_pas_une_permission_qu_on_ne_detient_pas` : sans lui,
`roles.write` — la permission qui compose les rôles — vaudrait « Super Admin »
en deux requêtes. Créer un rôle portant tout le registre, se l'attribuer. Le
registre fermé de l'ADR-005 protège des permissions inventées, pas de celles
qu'on s'accorde.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

from apps.accounts.models import Role, User, UserType
from apps.accounts.permissions import PERMISSIONS
from apps.accounts.services import AuthService
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def siege() -> User:
    return User.objects.create_superuser("siege@elcorazon.test", "motdepasse")


class TestComptesClients:
    def test_un_client_se_consulte_mais_ne_s_edite_pas(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Le nom et l'adresse électronique sont les données du client. Les
        rendre éditables ici ouvrirait un chemin de reprise de compte."""
        client = connecte(personnel("sav@elcorazon.test", restaurant, "customers.read"))
        url = reverse("v1:administration:customer-detail", args=[customer.pk])

        assert client.get(url).status_code == status.HTTP_200_OK
        assert client.patch(url, {"email": "voleur@ailleurs.test"}, format="json").status_code == (
            status.HTTP_405_METHOD_NOT_ALLOWED
        )

    def test_le_personnel_n_apparait_pas_dans_les_clients(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Un compte du personnel n'est pas « interdit » ici : il n'appartient
        pas à cette ressource."""
        agent = personnel("sav@elcorazon.test", restaurant, "customers.read")

        emails = {
            fiche["email"]
            for fiche in connecte(agent)
            .get(reverse("v1:administration:customer-list"))
            .data["results"]
        }

        assert emails == {customer.email}

    def test_lire_ne_donne_pas_le_droit_de_bloquer(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        client = connecte(personnel("sav@elcorazon.test", restaurant, "customers.read"))

        response = client.post(
            reverse("v1:administration:customer-block", args=[customer.pk]),
            {"reason": "fraude"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_bloquer_revoque_les_jetons(self, customer: User, restaurant: Restaurant) -> None:
        """Désactiver sans révoquer ne ferme rien pendant la durée de vie des
        jetons en circulation."""
        AuthService.issue_tokens(customer)
        client = connecte(
            personnel("manager@elcorazon.test", restaurant, "customers.read", "customers.block")
        )

        response = client.post(
            reverse("v1:administration:customer-block", args=[customer.pk]),
            {"reason": "impayés répétés"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        customer.refresh_from_db()
        assert not customer.is_active
        assert BlacklistedToken.objects.filter(token__user=customer).exists()

    def test_un_blocage_sans_motif_est_refuse(self, customer: User, restaurant: Restaurant) -> None:
        """Un compte fermé sans motif est un litige qu'on ne saura pas
        instruire six mois plus tard."""
        client = connecte(
            personnel("manager@elcorazon.test", restaurant, "customers.read", "customers.block")
        )

        response = client.post(
            reverse("v1:administration:customer-block", args=[customer.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_compte_bloque_se_rouvre(self, customer: User, restaurant: Restaurant) -> None:
        User.objects.filter(pk=customer.pk).update(is_active=False)
        client = connecte(
            personnel("manager@elcorazon.test", restaurant, "customers.read", "customers.block")
        )

        client.post(
            reverse("v1:administration:customer-unblock", args=[customer.pk]), {}, format="json"
        )

        customer.refresh_from_db()
        assert customer.is_active


class TestRoles:
    def test_le_registre_des_permissions_est_servi_par_le_serveur(
        self, restaurant: Restaurant
    ) -> None:
        """Sans cette route, l'écran qui compose un rôle recopierait la liste
        côté client, et les deux divergeraient à la première permission
        ajoutée."""
        client = connecte(personnel("admin@elcorazon.test", restaurant, "roles.read"))

        response = client.get(reverse("v1:administration:role-permissions"))

        assert response.status_code == status.HTTP_200_OK
        assert {entree["code"] for entree in response.data} == set(PERMISSIONS)

    def test_une_permission_inconnue_est_refusee_en_400(self, siege: User) -> None:
        """`Role.save()` lève une `ValidationError` Django, que DRF ne traduit
        pas : sans validation ici, une faute de frappe sortirait en 500."""
        response = connecte(siege).post(
            reverse("v1:administration:role-list"),
            {"name": "Bancal", "permissions": ["orders.refunds"]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not Role.objects.filter(name="Bancal").exists()

    def test_un_role_systeme_ne_se_modifie_pas(self, siege: User) -> None:
        """Retirer « Super Admin » d'une instance en production enfermerait
        tout le monde dehors."""
        systeme = Role.objects.create(
            name="Super Admin", permissions=sorted(PERMISSIONS), is_system=True
        )

        response = connecte(siege).patch(
            reverse("v1:administration:role-detail", args=[systeme.pk]),
            {"permissions": []},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        systeme.refresh_from_db()
        assert systeme.permissions

    def test_un_role_ne_se_supprime_pas(self, siege: User) -> None:
        """Retiré à chaud, il priverait sans préavis les comptes qui le
        portent, et l'effet ne se verrait qu'au prochain refus."""
        role = Role.objects.create(name="Éphémère", permissions=["orders.read"])

        response = connecte(siege).delete(reverse("v1:administration:role-detail", args=[role.pk]))

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestPersonnel:
    def test_un_compte_du_personnel_se_cree_avec_ses_roles_et_son_perimetre(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        role = Role.objects.create(name="Caisse", permissions=["orders.read"])

        response = connecte(siege).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "nouveau@elcorazon.test",
                "full_name": "Ama Nouvelle",
                "password": "MotDePasseSolide!42",
                "roles": [str(role.pk)],
                "restaurants": [restaurant.slug],
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["permissions"] == ["orders.read"]
        assert response.data["restaurants"] == [restaurant.slug]

        membre = User.objects.get(email="nouveau@elcorazon.test")
        assert membre.user_type == UserType.STAFF
        assert membre.check_password("MotDePasseSolide!42")

    def test_un_compte_se_cree_avec_un_mot_de_passe(self, siege: User) -> None:
        response = connecte(siege).post(
            reverse("v1:restaurants:staff-list"),
            {"email": "sans@elcorazon.test", "full_name": "Sans mot de passe"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_on_n_accorde_pas_une_permission_qu_on_ne_detient_pas(
        self, restaurant: Restaurant
    ) -> None:
        """**Le test central de ce module.** Sans lui, `roles.write` vaudrait
        « Super Admin » en deux requêtes."""
        gerant = personnel("gerant@elcorazon.test", restaurant, "roles.read", "roles.write")
        tout = Role.objects.create(name="Tout puissant", permissions=sorted(PERMISSIONS))

        response = connecte(gerant).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "complice@elcorazon.test",
                "full_name": "Complice",
                "password": "MotDePasseSolide!42",
                "roles": [str(tout.pk)],
                "restaurants": [restaurant.slug],
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not User.objects.filter(email="complice@elcorazon.test").exists()

    def test_un_gerant_n_embauche_pas_pour_un_autre_etablissement(
        self, restaurant: Restaurant, zone: object
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000002",
        )
        gerant = personnel("gerant@elcorazon.test", restaurant, "roles.read", "roles.write")

        response = connecte(gerant).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "ailleurs@elcorazon.test",
                "full_name": "Ailleurs",
                "password": "MotDePasseSolide!42",
                "restaurants": [ailleurs.slug],
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_l_adresse_electronique_ne_se_modifie_pas(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        """Elle est l'identifiant de connexion : la changer depuis un écran
        d'administration serait un chemin de reprise de compte."""
        membre = personnel("membre@elcorazon.test", restaurant, "orders.read")

        connecte(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[membre.pk]),
            {"email": "voleur@ailleurs.test"},
            format="json",
        )

        membre.refresh_from_db()
        assert membre.email == "membre@elcorazon.test"

    def test_desactiver_revoque_les_jetons(self, siege: User, restaurant: Restaurant) -> None:
        membre = personnel("membre@elcorazon.test", restaurant, "orders.refund")
        AuthService.issue_tokens(membre)

        connecte(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[membre.pk]),
            {"is_active": False},
            format="json",
        )

        membre.refresh_from_db()
        assert not membre.is_active
        assert BlacklistedToken.objects.filter(token__user=membre).exists()

    def test_le_rattachement_conserve_sa_date(self, siege: User, restaurant: Restaurant) -> None:
        """Un rattachement dit depuis quand quelqu'un travaille là : le
        remplacer en bloc à chaque enregistrement effacerait l'information."""
        membre = personnel("membre@elcorazon.test", restaurant, "orders.read")
        origine = StaffMembership.objects.get(user=membre).created_at

        connecte(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[membre.pk]),
            {"restaurants": [restaurant.slug], "full_name": "Nom corrigé"},
            format="json",
        )

        assert StaffMembership.objects.get(user=membre).created_at == origine

    def test_un_gerant_ne_voit_que_le_personnel_de_ses_etablissements(
        self, restaurant: Restaurant
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000003",
        )
        gerant = personnel("gerant@elcorazon.test", restaurant, "roles.read")
        personnel("kara@elcorazon.test", ailleurs, "orders.read")

        emails = {
            fiche["email"]
            for fiche in connecte(gerant).get(reverse("v1:restaurants:staff-list")).data["results"]
        }

        assert emails == {"gerant@elcorazon.test"}
