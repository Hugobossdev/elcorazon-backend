"""Le journal des décisions — ce qu'il consigne des droits, et qui le relit.

Il était écrit à chaque changement de barème, de zone ou d'emplacement, et
lisible **nulle part**. Et il ne disait rien des droits : un rôle qui gagnait
`orders.refund`, un compte rattaché à une cuisine de plus, un client bloqué —
dont le motif, pourtant exigé, n'était conservé nulle part.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.restaurants.models import Restaurant, StaffMembership
from common.audit import AuditAction, AuditEntry, record_change

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

JOURNAL = "v1:restaurants:audit-list"


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def siege() -> User:
    return User.objects.create_superuser("siege@elcorazon.test", "motdepasse")


class TestCeQuiEstConsigne:
    def test_le_motif_d_un_blocage_client_est_conserve(self, siege: User, customer: User) -> None:
        connecte(siege).post(
            reverse("v1:administration:customer-block", args=[customer.pk]),
            {"reason": "Trois commandes refusées à la livraison"},
            format="json",
        )

        entree = AuditEntry.objects.get(action=AuditAction.CUSTOMER_BLOCK)
        assert entree.after == {
            "is_active": False,
            "reason": "Trois commandes refusées à la livraison",
        }
        assert entree.actor == siege

    def test_les_permissions_d_un_role_avant_et_apres(self, siege: User) -> None:
        role = Role.objects.create(name="Caisse", permissions=["orders.read"])

        connecte(siege).patch(
            reverse("v1:administration:role-detail", args=[role.pk]),
            {"permissions": ["orders.read", "orders.refund"]},
            format="json",
        )

        entree = AuditEntry.objects.get(action=AuditAction.ROLE_PERMISSIONS)
        assert entree.before == {"permissions": ["orders.read"]}
        assert entree.after == {"permissions": ["orders.read", "orders.refund"]}

    def test_renommer_un_role_n_ecrit_rien(self, siege: User) -> None:
        role = Role.objects.create(name="Caisse", permissions=["orders.read"])

        connecte(siege).patch(
            reverse("v1:administration:role-detail", args=[role.pk]),
            {"name": "Caisse soir"},
            format="json",
        )

        assert not AuditEntry.objects.filter(action=AuditAction.ROLE_PERMISSIONS).exists()

    def test_rattacher_et_nommer_font_deux_entrees(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        membre = personnel("awa@elcorazon.test", None)
        role = Role.objects.create(name="Opérateur de nuit", permissions=["orders.read"])

        connecte(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[membre.pk]),
            {"roles": [str(role.pk)], "restaurants": [restaurant.slug]},
            format="json",
        )

        actions = set(
            AuditEntry.objects.filter(target_id=str(membre.pk)).values_list("action", flat=True)
        )
        assert actions == {AuditAction.STAFF_ROLES, AuditAction.STAFF_SCOPE}
        perimetre = AuditEntry.objects.get(action=AuditAction.STAFF_SCOPE)
        assert perimetre.after["restaurants"] == [restaurant.slug]

    def test_un_mot_de_passe_remplace_se_consigne_sans_etre_ecrit(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        membre = personnel("awa@elcorazon.test", restaurant, "orders.read")

        connecte(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[membre.pk]),
            {"password": "NouveauMotDePasse!42"},
            format="json",
        )

        entree = AuditEntry.objects.get(action=AuditAction.STAFF_PASSWORD)
        assert "NouveauMotDePasse!42" not in str(entree.before) + str(entree.after)


class TestQuiLeRelit:
    def test_sans_audit_read_le_journal_est_ferme(self, restaurant: Restaurant) -> None:
        operateur = personnel("op@elcorazon.test", restaurant, "orders.read")

        response = connecte(operateur).get(reverse(JOURNAL))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_le_siege_lit_tout(self, siege: User, restaurant: Restaurant) -> None:
        record_change(
            actor=siege,
            action=AuditAction.ROLE_PERMISSIONS,
            target_type="role",
            target_id="role-1",
            target_label="Caisse",
            before={"permissions": []},
            after={"permissions": ["orders.refund"]},
        )

        response = connecte(siege).get(reverse(JOURNAL))

        assert response.status_code == status.HTTP_200_OK
        assert [e["action"] for e in response.data["results"]] == [AuditAction.ROLE_PERMISSIONS]

    def test_un_gerant_lit_son_etablissement_pas_les_roles(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        gerant = personnel("gerant@elcorazon.test", restaurant, "audit.read")
        record_change(
            actor=siege,
            action=AuditAction.RESTAURANT_LOCATION,
            target_type="restaurant",
            target_id=restaurant.pk,
            target_label=restaurant.name,
            before={"location": [1.0, 6.0]},
            after={"location": [1.1, 6.1]},
        )
        record_change(
            actor=siege,
            action=AuditAction.ZONE_TARIFF,
            target_type="zone",
            target_id=restaurant.zone_id,
            target_label="Zone du restaurant",
            before={"fee": "500"},
            after={"fee": "700"},
        )
        record_change(
            actor=siege,
            action=AuditAction.ROLE_PERMISSIONS,
            target_type="role",
            target_id="role-1",
            target_label="Caisse",
            before={"permissions": []},
            after={"permissions": ["orders.refund"]},
        )

        actions = {e["action"] for e in connecte(gerant).get(reverse(JOURNAL)).data["results"]}

        assert actions == {AuditAction.RESTAURANT_LOCATION, AuditAction.ZONE_TARIFF}

    def test_le_filtre_par_famille_d_action(self, siege: User) -> None:
        for action in (AuditAction.STAFF_ROLES, AuditAction.STAFF_SCOPE, AuditAction.ZONE_TARIFF):
            record_change(
                actor=siege,
                action=action,
                target_type="x",
                target_id="1",
                target_label="x",
                before={"a": 1},
                after={"a": 2},
            )

        response = connecte(siege).get(reverse(JOURNAL), {"action__startswith": "staff."})

        assert {e["action"] for e in response.data["results"]} == {
            AuditAction.STAFF_ROLES,
            AuditAction.STAFF_SCOPE,
        }


class TestLArgentQuiSort:
    """Les décisions d'argent au journal, et leur cloisonnement.

    Un retrait, un remboursement ou une réclamation tranchée ne se rattachent à
    aucune des trois cibles historiques — établissement, zone, compte — et leur
    établissement se lit sur `payments` et `support`, que `restaurants` n'a pas
    le droit de connaître. Le périmètre est donc **écrit sur l'entrée** au
    moment de la décision ; ces tests vérifient qu'il l'est, et qu'il suffit à
    rendre l'entrée lisible par celui qui l'a prise — et illisible ailleurs.
    """

    @pytest.fixture
    def ailleurs(self, restaurant: Restaurant) -> Restaurant:
        return Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000031",
        )

    def entree(
        self, restaurant: Restaurant | None, action: str = AuditAction.PAYOUT_SETTLE
    ) -> None:
        record_change(
            actor=None,
            action=action,
            target_type="withdrawal",
            target_id="01a0c0d4-a48e-7563-a977-5b86ec4f8888",
            target_label="2 000 XOF — Komi Livreur",
            before={"status": "pending"},
            after={"status": "completed"},
            scope_restaurant_id=restaurant.pk if restaurant else None,
        )

    def test_un_gerant_relit_les_versements_de_sa_cuisine(self, restaurant: Restaurant) -> None:
        self.entree(restaurant)
        gerant = personnel("gerant@elcorazon.test", restaurant, "audit.read")

        actions = [
            ligne["action"] for ligne in connecte(gerant).get(reverse(JOURNAL)).data["results"]
        ]

        assert actions == [AuditAction.PAYOUT_SETTLE]

    def test_il_ne_relit_pas_ceux_d_ailleurs(
        self, restaurant: Restaurant, ailleurs: Restaurant
    ) -> None:
        self.entree(ailleurs)
        gerant = personnel("gerant@elcorazon.test", restaurant, "audit.read")

        assert connecte(gerant).get(reverse(JOURNAL)).data["results"] == []

    def test_une_entree_sans_perimetre_ne_se_lit_qu_au_siege(
        self, restaurant: Restaurant, siege: User
    ) -> None:
        """Le défaut sûr : ce qui ne relève d'aucun établissement — un rôle, un
        client — reste au siège."""
        self.entree(None)
        gerant = personnel("gerant@elcorazon.test", restaurant, "audit.read")

        assert connecte(gerant).get(reverse(JOURNAL)).data["results"] == []
        assert len(connecte(siege).get(reverse(JOURNAL)).data["results"]) == 1
