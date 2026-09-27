"""Élévation de privilège par la gestion des droits — ADR-005.

`test_administration.py` ferme le chemin le plus visible : on n'**attribue**
pas un rôle plus large que ses propres droits. Il en restait trois, et chacun
rendait cette garde décorative :

* **modifier un rôle qu'on porte déjà.** `roles.write` compose les rôles ; le
  rôle sur mesure qu'on vous a attribué est un rôle comme un autre. Lui ajouter
  `orders.refund` vous l'accorde à l'instant, sans aucune attribution — la
  garde n'était jamais consultée ;
* **modifier un rôle que porte quelqu'un d'autre**, ailleurs : un directeur de
  Lomé qui élargit un rôle commun élargit du même geste les comptes d'Abidjan ;
* **reprendre un compte plus puissant que soi.** Un compte cloisonné muni de
  `roles.write` voyait, et pouvait modifier, tout collègue rattaché à l'un de
  ses établissements — y compris le siège, s'il y était rattaché. Remplacer son
  mot de passe, c'est se connecter à sa place.

La règle qui les ferme tous : **on ne touche pas à ce qui dépasse ses propres
droits** — ni un rôle plus large que les siens, ni un compte plus puissant ou
plus étendu que le sien. Le siège n'est pas concerné : il détient tout.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.accounts.permissions import PERMISSIONS
from apps.geography.models import Country
from apps.restaurants.models import AreaMembership, Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

ADMIN_DROITS = ("roles.read", "roles.write")


def compte(email: str, restaurant: Restaurant | None, role: Role) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(role)
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def ailleurs(restaurant: Restaurant) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=restaurant.zone,
        address="Kara",
        location=restaurant.location,
        phone="+22890000009",
    )


@pytest.fixture
def role_rh() -> Role:
    """Le rôle sur mesure d'un responsable du personnel : il compose et attribue."""
    return Role.objects.create(name="Responsable RH", permissions=list(ADMIN_DROITS))


@pytest.fixture
def rh(restaurant: Restaurant, role_rh: Role) -> User:
    return compte("rh@elcorazon.test", restaurant, role_rh)


class TestLesRoles:
    def test_on_ne_s_accorde_pas_une_permission_en_modifiant_son_propre_role(
        self, rh: User, role_rh: Role
    ) -> None:
        """**La faille.** Aucune attribution, donc aucune garde : le rôle porté
        s'élargit, et son porteur avec lui."""
        response = connecte(rh).patch(
            reverse("v1:administration:role-detail", args=[role_rh.pk]),
            {"permissions": sorted(PERMISSIONS)},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        role_rh.refresh_from_db()
        assert sorted(role_rh.permissions) == sorted(ADMIN_DROITS)
        assert "orders.refund" not in User.objects.get(pk=rh.pk).permission_codes()

    def test_on_ne_compose_pas_un_role_plus_large_que_ses_droits(self, rh: User) -> None:
        """Créé, il attendrait qu'un autre l'attribue ; modifié ensuite, il
        élargirait ses porteurs. La garde est la même aux deux portes."""
        response = connecte(rh).post(
            reverse("v1:administration:role-list"),
            {"name": "Caisse élargie", "permissions": ["orders.read", "orders.refund"]},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Role.objects.filter(name="Caisse élargie").exists()

    def test_un_role_dans_ses_droits_se_compose_et_se_modifie(self, rh: User) -> None:
        """Le geste légitime reste ouvert : c'est ce que `roles.write` sert à
        faire."""
        creation = connecte(rh).post(
            reverse("v1:administration:role-list"),
            {"name": "Lecture des rôles", "permissions": ["roles.read"]},
            format="json",
        )
        assert creation.status_code == status.HTTP_201_CREATED

        modification = connecte(rh).patch(
            reverse("v1:administration:role-detail", args=[creation.data["id"]]),
            {"permissions": ["roles.read", "roles.write"]},
            format="json",
        )
        assert modification.status_code == status.HTTP_200_OK

    def test_on_ne_modifie_pas_un_role_porte_hors_de_son_perimetre(
        self, rh: User, ailleurs: Restaurant
    ) -> None:
        """Élargir un rôle commun, c'est élargir d'un geste tous ceux qui le
        portent — y compris ceux d'un établissement qu'on ne gère pas."""
        commun = Role.objects.create(name="Lecture", permissions=["roles.read"])
        compte("kara@elcorazon.test", ailleurs, commun)

        response = connecte(rh).patch(
            reverse("v1:administration:role-detail", args=[commun.pk]),
            {"permissions": ["roles.read", "roles.write"]},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        commun.refresh_from_db()
        assert commun.permissions == ["roles.read"]

    def test_le_siege_compose_librement(self) -> None:
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")

        response = connecte(siege).post(
            reverse("v1:administration:role-list"),
            {"name": "Tout", "permissions": sorted(PERMISSIONS)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED


class TestLesComptes:
    def test_on_ne_remplace_pas_le_mot_de_passe_du_siege(
        self, rh: User, restaurant: Restaurant
    ) -> None:
        """**La faille.** Un superutilisateur rattaché à un établissement
        apparaît dans la liste du gérant ; remplacer son mot de passe, c'est se
        connecter à sa place."""
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
        StaffMembership.objects.create(user=siege, restaurant=restaurant)

        response = connecte(rh).patch(
            reverse("v1:restaurants:staff-detail", args=[siege.pk]),
            {"password": "PriseDeControle!42"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        siege.refresh_from_db()
        assert siege.check_password("motdepasse")

    def test_on_ne_reprend_pas_un_collegue_plus_puissant(
        self, rh: User, restaurant: Restaurant
    ) -> None:
        """Il détient `orders.refund`, que le responsable n'a pas : prendre son
        compte, c'est gagner ce droit."""
        caissier = compte(
            "caisse@elcorazon.test",
            restaurant,
            Role.objects.create(name="Caisse", permissions=["orders.read", "orders.refund"]),
        )

        for charge in ({"password": "PriseDeControle!42"}, {"is_active": False}, {"roles": []}):
            response = connecte(rh).patch(
                reverse("v1:restaurants:staff-detail", args=[caissier.pk]), charge, format="json"
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN, charge

        caissier.refresh_from_db()
        assert caissier.check_password("motdepasse")
        assert caissier.is_active

    def test_on_ne_retire_pas_un_rattachement_qu_on_ne_couvre_pas(
        self, rh: User, restaurant: Restaurant, ailleurs: Restaurant
    ) -> None:
        """Rattaché à Lomé **et** à Kara, le collègue apparaît chez le gérant de
        Lomé. Lui renvoyer `[lomé]` effaçait Kara — une décision sur un
        établissement que le gérant ne gère pas."""
        partage = compte("partage@elcorazon.test", restaurant, Role.objects.create(name="Vide"))
        StaffMembership.objects.create(user=partage, restaurant=ailleurs)

        response = connecte(rh).patch(
            reverse("v1:restaurants:staff-detail", args=[partage.pk]),
            {"restaurants": [restaurant.slug]},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert StaffMembership.objects.filter(user=partage, restaurant=ailleurs).exists()

    def test_on_ne_touche_pas_a_un_directeur_de_marche(
        self, rh: User, restaurant: Restaurant, country: Country
    ) -> None:
        """Un directeur pays rattaché aussi à l'établissement voit plus large
        que le gérant : son compte n'est pas du ressort de ce dernier."""
        directeur = compte("directeur@elcorazon.test", restaurant, Role.objects.create(name="Dir"))
        AreaMembership.objects.create(user=directeur, country=country)

        response = connecte(rh).patch(
            reverse("v1:restaurants:staff-detail", args=[directeur.pk]),
            {"full_name": "Renommé"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_collegue_de_son_ressort_se_gere(self, rh: User, restaurant: Restaurant) -> None:
        """Le geste ordinaire — renommer, réinitialiser, désactiver un collègue
        qui ne dépasse ni vos droits ni votre périmètre — reste ouvert."""
        equipier = compte(
            "equipier@elcorazon.test", restaurant, Role.objects.create(name="Rien", permissions=[])
        )

        response = connecte(rh).patch(
            reverse("v1:restaurants:staff-detail", args=[equipier.pk]),
            {"full_name": "Équipier renommé", "password": "NouveauMotDePasse!42"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        equipier.refresh_from_db()
        assert equipier.check_password("NouveauMotDePasse!42")
