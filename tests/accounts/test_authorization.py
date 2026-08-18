"""Tests du modèle d'autorisation — ADR-005.

L'implémentation précédente avait deux mécanismes concurrents : une colonne
`role` appliquée par le serveur, et des rôles admin à permissions JSONB
appliqués uniquement par l'interface. Un « Opérateur » privé du module
marketing dans l'écran pouvait appeler l'API marketing sans obstacle.

Ces tests verrouillent le fait qu'il n'y a plus qu'une vérité, côté serveur.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, User, UserType
from apps.accounts.permissions import PERMISSIONS, SYSTEM_ROLES

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff() -> User:
    return User.objects.create_user(
        "staff@elcorazon.test",
        "motdepasse",
        full_name="Membre du personnel",
        user_type=UserType.STAFF,
    )


@pytest.fixture
def customer() -> User:
    return User.objects.create_user("client@elcorazon.test", "motdepasse", full_name="Cliente")


class TestRegistreDePermissions:
    def test_une_permission_hors_registre_est_refusee(self) -> None:
        """Une faute de frappe produirait sinon un rôle qui *semble* accorder
        un droit sans en accorder aucun — ni fonctionnel, ni détecté."""
        role = Role(name="Bancal", permissions=["orders.refunds"])
        with pytest.raises(ValidationError, match="inconnues"):
            role.save()

    def test_les_permissions_valides_sont_acceptees(self) -> None:
        role = Role.objects.create(name="Caisse", permissions=["orders.read", "orders.refund"])
        assert role.pk is not None

    def test_les_roles_systeme_ne_citent_que_des_permissions_connues(self) -> None:
        """Garde-fou sur les données livrées : un rôle d'installation qui
        référencerait une permission supprimée passerait inaperçu."""
        for name, codes in SYSTEM_ROLES.items():
            unknown = set(codes) - set(PERMISSIONS)
            assert not unknown, f"Rôle {name} : permissions inconnues {unknown}"


class TestPermissionsDuPersonnel:
    def test_sans_role_le_personnel_n_a_aucun_droit(self, staff: User) -> None:
        """Refus par défaut (T3) : un compte du personnel fraîchement créé ne
        peut rien faire tant qu'un rôle ne lui a pas été accordé."""
        assert staff.permission_codes() == set()
        assert not staff.has_permission("orders.read")

    def test_les_roles_se_cumulent(self, staff: User) -> None:
        staff.roles.add(
            Role.objects.create(name="Lecture", permissions=["orders.read"]),
            Role.objects.create(name="Remboursement", permissions=["orders.refund"]),
        )
        assert staff.permission_codes() == {"orders.read", "orders.refund"}

    def test_un_superutilisateur_a_tout(self, staff: User) -> None:
        staff.is_superuser = True
        assert staff.has_permission("roles.write")

    def test_un_compte_desactive_ne_detient_plus_rien(self, staff: User) -> None:
        """La désactivation doit être immédiatement effective, y compris pour
        un superutilisateur : sinon révoquer un accès demanderait aussi de
        défaire ses rôles un par un."""
        staff.roles.add(Role.objects.create(name="Lecture", permissions=["orders.read"]))
        staff.is_superuser = True
        staff.is_active = False

        assert not staff.has_permission("orders.read")
        assert not staff.has_permission("roles.write")


class TestSeparationDesTypes:
    def test_un_client_ne_peut_pas_detenir_de_permission(self, customer: User) -> None:
        """Même en lui attachant un rôle : le type de compte prime.

        C'est la garde contre une erreur d'administration — accorder par
        mégarde un rôle du personnel à un compte client ne doit pas ouvrir le
        back-office.
        """
        customer.roles.add(Role.objects.create(name="Manager", permissions=["orders.refund"]))

        assert customer.permission_codes() == set()
        assert not customer.has_permission("orders.refund")

    def test_les_predicats_de_type_sont_exclusifs(self, customer: User, staff: User) -> None:
        assert customer.is_customer and not customer.is_staff_member
        assert staff.is_staff_member and not staff.is_customer


class TestInterfaceAdmin:
    def test_is_staff_exige_type_et_activation(self, staff: User, customer: User) -> None:
        assert staff.is_staff
        assert not customer.is_staff

        staff.is_active = False
        assert not staff.is_staff

    def test_has_perm_delegue_au_modele_metier(self, staff: User) -> None:
        """`has_perm` existe pour django.contrib.admin, mais ne doit pas
        constituer un second système de permissions."""
        staff.roles.add(Role.objects.create(name="Catalogue", permissions=["catalog.write"]))

        assert staff.has_perm("catalog.write")
        assert not staff.has_perm("orders.refund")

    def test_has_module_perms(self, staff: User) -> None:
        staff.roles.add(Role.objects.create(name="Catalogue", permissions=["catalog.write"]))

        assert staff.has_module_perms("catalog")
        assert not staff.has_module_perms("payments")


class TestCreationDeCompte:
    def test_un_superutilisateur_est_du_personnel(self) -> None:
        user = User.objects.create_superuser("root@elcorazon.test", "motdepasse")
        assert user.user_type == UserType.STAFF
        assert user.is_superuser

    def test_un_superutilisateur_client_est_refuse(self) -> None:
        with pytest.raises(ValueError, match="personnel"):
            User.objects.create_superuser(
                "faux@elcorazon.test", "motdepasse", user_type=UserType.CUSTOMER
            )

    def test_l_email_est_obligatoire(self) -> None:
        with pytest.raises(ValueError, match="e-mail"):
            User.objects.create_user("", "motdepasse", full_name="Sans email")

    def test_le_mot_de_passe_est_hache(self) -> None:
        user = User.objects.create_user("a@elcorazon.test", "motdepasse", full_name="A")
        assert user.password != "motdepasse"
        assert user.check_password("motdepasse")
