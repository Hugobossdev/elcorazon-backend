"""Un remboursement qui ne sera pas versé — et ce que son absence coûtait.

`RefundService.refund` enregistre une **intention** ; `settle` la solde. Entre
les deux, rien : une demande saisie par erreur — mauvais montant, mauvaise
commande, geste refusé par le responsable — restait « en attente » pour
toujours. Et comme le plafond du remboursable (P3) compte les lignes en
attente, elle rendait la commande **irremboursable** : le second essai, celui
avec le bon montant, était refusé par une ligne qu'on ne pouvait pas retirer.

Le seul recours était l'administration Django. `pending → cancelled` existait
pourtant dans la machine à états, et n'était atteint par personne.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.orders.models import Order
from apps.payments.models import PaymentProvider, PaymentStatus, Refund, Transaction
from apps.payments.services import RefundService
from apps.restaurants.models import Restaurant, StaffMembership
from common.audit import AuditAction, AuditEntry
from common.money import Money
from tests.fixtures import XOF

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def caissier(restaurant: Restaurant) -> User:
    return personnel("caisse@elcorazon.test", restaurant, "orders.read", "orders.refund")


@pytest.fixture
def encaissement(order: Order) -> Transaction:
    return Transaction.objects.create(
        order=order,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference="PD-042",
        amount=order.total,
        status=PaymentStatus.COMPLETED,
    )


@pytest.fixture
def demande(order: Order, encaissement: Transaction, caissier: User) -> Refund:
    return RefundService.refund(
        order=order,
        transaction_id=str(encaissement.pk),
        amount=order.total,
        reason="Saisi sur la mauvaise commande",
        actor=caissier,
    )


def abandonner(caissier: User, refund: Refund, motif: str = "Saisi par erreur.") -> object:
    return connecte(caissier).post(
        reverse("v1:payments:managed-refund-cancel", args=[refund.pk]),
        {"reason": motif},
        format="json",
    )


class TestAbandon:
    def test_une_demande_erronee_se_referme_avec_son_motif(
        self, caissier: User, demande: Refund
    ) -> None:
        response = abandonner(caissier, demande)

        assert response.status_code == status.HTTP_200_OK
        demande.refresh_from_db()
        assert demande.status == PaymentStatus.CANCELLED
        assert "Saisi par erreur" in demande.reason

    def test_le_motif_est_exige(self, caissier: User, demande: Refund) -> None:
        """« Annulé » sans raison est exactement ce qu'on cherche à comprendre
        six mois plus tard."""
        response = connecte(caissier).post(
            reverse("v1:payments:managed-refund-cancel", args=[demande.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        demande.refresh_from_db()
        assert demande.status == PaymentStatus.PENDING

    def test_le_plafond_est_rendu_a_la_commande(
        self, caissier: User, order: Order, encaissement: Transaction, demande: Refund
    ) -> None:
        """**Le test qui porte ce module.** La demande erronée consommait tout
        le remboursable : la commande ne pouvait plus être remboursée du bon
        montant."""
        charge = {
            "transaction": str(encaissement.pk),
            "amount": {"amount": "1000", "currency": XOF},
            "reason": "Le bon montant",
        }
        bloque = connecte(caissier).post(
            reverse("v1:payments:refund", args=[order.pk]), charge, format="json"
        )
        assert bloque.status_code == status.HTTP_409_CONFLICT

        abandonner(caissier, demande)

        apres = connecte(caissier).post(
            reverse("v1:payments:refund", args=[order.pk]), charge, format="json"
        )
        assert apres.status_code == status.HTTP_201_CREATED

    def test_un_remboursement_verse_ne_s_annule_pas(self, caissier: User, demande: Refund) -> None:
        """L'argent est parti : ce qui se corrige est un encaissement, pas une
        écriture."""
        RefundService.settle(refund=demande, provider_reference="PD-R-1", actor=caissier)

        response = abandonner(caissier, demande)

        assert response.status_code == status.HTTP_409_CONFLICT
        demande.refresh_from_db()
        assert demande.status == PaymentStatus.COMPLETED

    def test_abandonner_exige_orders_refund(self, restaurant: Restaurant, demande: Refund) -> None:
        lecteur = personnel("op@elcorazon.test", restaurant, "orders.read")

        response = abandonner(lecteur, demande)

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestJournal:
    """Les trois temps d'un remboursement laissent une trace lisible.

    La ligne dit où en est *ce* remboursement ; elle ne dit pas ce qu'un
    opérateur a décidé cette semaine, et c'est la question qu'on pose quand un
    client affirme n'avoir rien reçu.
    """

    def test_la_demande_le_constat_et_l_abandon_sont_consignes(
        self, caissier: User, order: Order, encaissement: Transaction
    ) -> None:
        verse = RefundService.refund(
            order=order,
            transaction_id=str(encaissement.pk),
            amount=Money(1_000, XOF),
            reason="Plat manquant",
            actor=caissier,
        )
        RefundService.settle(refund=verse, provider_reference="PD-R-9", actor=caissier)
        abandonnee = RefundService.refund(
            order=order,
            transaction_id=str(encaissement.pk),
            amount=Money(500, XOF),
            reason="Doublon",
            actor=caissier,
        )
        RefundService.cancel(refund=abandonnee, reason="Doublon de la précédente", actor=caissier)

        actions = list(
            AuditEntry.objects.filter(target_type="refund").values_list("action", flat=True)
        )

        assert sorted(actions) == sorted(
            [
                AuditAction.REFUND_REQUEST,
                AuditAction.REFUND_SETTLE,
                AuditAction.REFUND_REQUEST,
                AuditAction.REFUND_CANCEL,
            ]
        )
        assert all(
            entree.scope_restaurant_id == order.restaurant_id
            for entree in AuditEntry.objects.filter(target_type="refund")
        )
        assert AuditEntry.objects.filter(action=AuditAction.REFUND_SETTLE).first().actor == caissier
