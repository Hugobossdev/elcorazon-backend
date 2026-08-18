"""Support — propriété de la commande, montant de retour plafonné.

Le fil conducteur est le même que S3 sur le partage social : une réclamation
ou une demande de retour désigne une commande, et rien ne relie `order` à
`user` qu'une contrainte de base saurait vérifier — la propriété se contrôle
donc dans le service, à la création.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from apps.support.models import ComplaintKind, ReturnStatus, SupportTicket, TicketCategory
from apps.support.services import SupportService
from common.exceptions import BusinessRuleViolation
from common.money import Money
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db

XOF = "XOF"


def deliver(order: Order) -> Order:
    for cible in (
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.PICKED_UP,
        OrderStatus.ON_THE_WAY,
        OrderStatus.DELIVERED,
    ):
        OrderService.transition_to(order=order, target=cible)
    order.refresh_from_db()
    return order


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


class TestTickets:
    def test_ouvrir_un_ticket(self, customer: User) -> None:
        ticket = SupportService.open_ticket(
            user=customer,
            category=TicketCategory.PAYMENT,
            subject="Paiement refusé",
            description="Ma carte a été débitée sans confirmation.",
        )

        assert ticket.status == "open"
        assert ticket.resolved_at is None

    def test_repondre_alimente_le_fil(self, customer: User) -> None:
        ticket = SupportService.open_ticket(user=customer, subject="Question", description="...")

        SupportService.reply(ticket=ticket, author=customer, content="Une précision")

        assert ticket.messages.count() == 1


class TestReclamations:
    def test_reclamer_sur_sa_propre_commande(self, customer: User, order: Order) -> None:
        reclamation = SupportService.file_complaint(
            user=customer,
            order=order,
            kind=ComplaintKind.QUALITY,
            subject="Plat froid",
            description="Le repas est arrivé froid.",
        )

        assert reclamation.order_id == order.pk

    def test_reclamer_sur_la_commande_d_autrui_est_refuse(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        commande_d_autrui = build_order(restaurant, courier_user, reference="EC000002")

        with pytest.raises(BusinessRuleViolation):
            SupportService.file_complaint(
                user=customer,
                order=commande_d_autrui,
                kind=ComplaintKind.QUALITY,
                subject="?",
                description="?",
            )


class TestDemandesDeRetour:
    def test_retourner_une_commande_livree(self, customer: User, order: Order) -> None:
        deliver(order)

        demande = SupportService.request_return(
            user=customer,
            order=order,
            reason="Article manquant",
            items=["Burger Corazón"],
            refund_amount=Money(1_000, XOF),
        )

        assert demande.status == ReturnStatus.PENDING

    def test_impossible_avant_livraison(self, customer: User, order: Order) -> None:
        with pytest.raises(BusinessRuleViolation):
            SupportService.request_return(
                user=customer,
                order=order,
                reason="Trop tôt",
                items=["Burger Corazón"],
                refund_amount=Money(1_000, XOF),
            )

    def test_le_montant_ne_peut_pas_depasser_le_total(self, customer: User, order: Order) -> None:
        """Même plafond que P3 sur le remboursement réel, posé ici avant
        même que la demande n'atteigne quiconque."""
        deliver(order)

        with pytest.raises(BusinessRuleViolation):
            SupportService.request_return(
                user=customer,
                order=order,
                reason="Abusif",
                items=["Tout"],
                refund_amount=order.total + Money(1, XOF),
            )

    def test_retourner_la_commande_d_autrui_est_refuse(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        commande_d_autrui = build_order(restaurant, courier_user, reference="EC000003")
        deliver(commande_d_autrui)

        with pytest.raises(BusinessRuleViolation):
            SupportService.request_return(
                user=customer,
                order=commande_d_autrui,
                reason="?",
                items=["?"],
                refund_amount=Money(100, XOF),
            )


class TestRoutes:
    def test_ouvrir_un_ticket_par_l_api(self, as_customer: APIClient) -> None:
        response = as_customer.post(
            reverse("v1:support:ticket-list"),
            {"category": "account", "subject": "Mot de passe", "description": "Je suis bloqué."},
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "open"

    def test_les_tickets_d_autrui_sont_invisibles(
        self, as_customer: APIClient, customer: User, courier_user: User
    ) -> None:
        SupportService.open_ticket(user=courier_user, subject="Autre", description="...")
        SupportService.open_ticket(user=customer, subject="Le mien", description="...")

        response = as_customer.get(reverse("v1:support:ticket-list"))

        assert [t["subject"] for t in response.data["results"]] == ["Le mien"]

    def test_reclamer_par_l_api(self, as_customer: APIClient, order: Order) -> None:
        response = as_customer.post(
            reverse("v1:support:complaint-list"),
            {
                "order": str(order.pk),
                "kind": "quality",
                "subject": "Plat froid",
                "description": "Froid à réception.",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_reclamer_sur_la_commande_d_autrui_par_l_api_est_refuse(
        self, as_customer: APIClient, courier_user: User, restaurant: Restaurant
    ) -> None:
        commande_d_autrui = build_order(restaurant, courier_user, reference="EC000004")

        response = as_customer.post(
            reverse("v1:support:complaint-list"),
            {
                "order": str(commande_d_autrui.pk),
                "kind": "quality",
                "subject": "?",
                "description": "?",
            },
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_demander_un_retour_par_l_api(self, as_customer: APIClient, order: Order) -> None:
        deliver(order)

        response = as_customer.post(
            reverse("v1:support:return-list"),
            {
                "order": str(order.pk),
                "reason": "Erreur de commande",
                "items": ["Burger Corazón"],
                "refund_amount": {"amount": "1000", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["refund_amount"] == {"amount": "1000", "currency": XOF}

    def test_un_montant_excessif_sort_en_409(self, as_customer: APIClient, order: Order) -> None:
        deliver(order)

        response = as_customer.post(
            reverse("v1:support:return-list"),
            {
                "order": str(order.pk),
                "reason": "Abusif",
                "items": ["Tout"],
                "refund_amount": {"amount": "999999", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_repondre_a_son_ticket_par_l_api(self, as_customer: APIClient, customer: User) -> None:
        ticket = SupportService.open_ticket(user=customer, subject="Sujet", description="...")

        response = as_customer.post(
            reverse("v1:support:ticket-messages", args=[ticket.pk]), {"content": "Une précision"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert SupportTicket.objects.get(pk=ticket.pk).messages.count() == 1

    def test_un_anonyme_n_a_rien(self) -> None:
        response = APIClient().get(reverse("v1:support:ticket-list"))

        assert response.status_code in (401, 403)

    def test_lister_le_fil_d_un_ticket(self, as_customer: APIClient, customer: User) -> None:
        ticket = SupportService.open_ticket(user=customer, subject="Sujet", description="...")
        SupportService.reply(ticket=ticket, author=customer, content="Une précision")

        response = as_customer.get(reverse("v1:support:ticket-messages", args=[ticket.pk]))

        assert response.status_code == status.HTTP_200_OK
        assert [m["content"] for m in response.data["results"]] == ["Une précision"]

    def test_lister_ses_reclamations(self, as_customer: APIClient, order: Order) -> None:
        SupportService.file_complaint(
            user=order.customer,
            order=order,
            kind=ComplaintKind.QUALITY,
            subject="Plat froid",
            description="Froid à réception.",
        )

        response = as_customer.get(reverse("v1:support:complaint-list"))

        assert response.status_code == status.HTTP_200_OK
        assert [c["subject"] for c in response.data["results"]] == ["Plat froid"]

    def test_lister_ses_demandes_de_retour(self, as_customer: APIClient, order: Order) -> None:
        deliver(order)
        SupportService.request_return(
            user=order.customer,
            order=order,
            reason="Article manquant",
            items=["Burger Corazón"],
            refund_amount=Money(1_000, XOF),
        )

        response = as_customer.get(reverse("v1:support:return-list"))

        assert response.status_code == status.HTTP_200_OK
        assert [r["reason"] for r in response.data["results"]] == ["Article manquant"]
