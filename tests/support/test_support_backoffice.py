"""Le support vu du back-office — lire, répondre, statuer.

Les routes du support n'étaient ouvertes qu'aux clients. Un client écrivait,
réclamait, demandait un retour ; le back-office n'avait aucun moyen de le lire,
et une réponse saisie dans l'administration Django ne partait vers personne.

Le test décisif est `test_repondre_previent_le_client` : c'est la moitié de la
conversation qui n'existait pas.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.notifications.models import Notification, NotificationKind
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Refund, Transaction
from apps.payments.services import RefundService
from apps.restaurants.models import Restaurant, StaffMembership
from apps.support.models import (
    Complaint,
    ComplaintKind,
    ComplaintStatus,
    ReturnRequest,
    ReturnStatus,
    SupportTicket,
    TicketCategory,
    TicketStatus,
)
from apps.support.services import SupportService
from common.money import Money
from tests.fixtures import XOF

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


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


def livrer(order: Order) -> Order:
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


def rembourser(order: Order, montant: Money, acteur: User) -> Refund:
    """Un remboursement **réellement constaté** sur la commande.

    Le support ne dit « remboursé » que sur ce que `payments` a soldé : sans
    cette étape, le client lirait l'annonce d'un virement que personne n'a
    fait.
    """
    transaction = Transaction.objects.create(
        order=order,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference="PD-SUPPORT-001",
        amount=order.total,
        status=PaymentStatus.COMPLETED,
    )
    demande = RefundService.refund(
        order=order,
        transaction_id=str(transaction.pk),
        amount=montant,
        reason="Retour accepté",
        actor=acteur,
    )
    return RefundService.settle(refund=demande, actor=acteur)


@pytest.fixture
def agent(restaurant: Restaurant) -> User:
    return personnel("support@elcorazon.test", restaurant, "support.read", "support.write")


@pytest.fixture
def ticket(customer: User) -> SupportTicket:
    return SupportService.open_ticket(
        user=customer,
        category=TicketCategory.PAYMENT,
        subject="Paiement débité deux fois",
        description="Deux débits pour une seule commande.",
    )


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


class TestTickets:
    def test_le_back_office_lit_ce_que_le_client_a_ecrit(
        self, agent: User, ticket: SupportTicket
    ) -> None:
        response = connecte(agent).get(reverse("v1:support:managed-ticket-list"))

        assert response.status_code == status.HTTP_200_OK
        (ligne,) = response.data["results"]
        assert ligne["subject"] == "Paiement débité deux fois"
        assert ligne["customer_name"] == ticket.user.full_name

    def test_a_traiter_se_demande_en_une_requete(
        self, agent: User, ticket: SupportTicket, customer: User
    ) -> None:
        """Ouvert **et** en cours, en une page : deux requêtes recollées
        feraient mentir la pagination."""
        en_cours = SupportService.open_ticket(
            user=customer, subject="Livreur perdu", description="Il tourne en rond."
        )
        SupportTicket.objects.filter(pk=en_cours.pk).update(status=TicketStatus.IN_PROGRESS)
        clos = SupportService.open_ticket(user=customer, subject="Réglé", description="Merci.")
        SupportTicket.objects.filter(pk=clos.pk).update(status=TicketStatus.CLOSED)

        response = connecte(agent).get(
            reverse("v1:support:managed-ticket-list"), {"status__in": "open,in_progress"}
        )

        sujets = {ligne["subject"] for ligne in response.data["results"]}
        assert sujets == {"Paiement débité deux fois", "Livreur perdu"}

    def test_sans_support_read_rien_ne_se_lit(
        self, restaurant: Restaurant, ticket: SupportTicket
    ) -> None:
        cuisinier = personnel("cuisine@elcorazon.test", restaurant, "orders.read")

        response = connecte(cuisinier).get(reverse("v1:support:managed-ticket-list"))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_client_n_ouvre_pas_la_file_du_support(
        self, customer: User, ticket: SupportTicket
    ) -> None:
        response = connecte(customer).get(reverse("v1:support:managed-ticket-list"))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_repondre_previent_le_client(self, agent: User, ticket: SupportTicket) -> None:
        response = connecte(agent).post(
            reverse("v1:support:managed-ticket-reply", args=[ticket.pk]),
            {"content": "Le second débit vous sera rendu sous 48 h."},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["author"]["user_type"] == UserType.STAFF
        ticket.refresh_from_db()
        # Quelqu'un s'en occupe, et le client le voit.
        assert ticket.status == TicketStatus.IN_PROGRESS
        avis = Notification.objects.get(user=ticket.user, kind=NotificationKind.SUPPORT)
        assert "48 h" in avis.body

    def test_la_fiche_porte_le_fil(self, agent: User, ticket: SupportTicket) -> None:
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-ticket-reply", args=[ticket.pk]),
            {"content": "Nous regardons."},
            format="json",
        )

        fiche = client.get(reverse("v1:support:managed-ticket-detail", args=[ticket.pk])).data

        assert [m["content"] for m in fiche["messages"]] == ["Nous regardons."]
        assert fiche["messages_count"] == 1

    def test_lire_ne_donne_pas_le_droit_de_repondre(
        self, restaurant: Restaurant, ticket: SupportTicket
    ) -> None:
        lecteur = personnel("lecteur@elcorazon.test", restaurant, "support.read")

        response = connecte(lecteur).post(
            reverse("v1:support:managed-ticket-reply", args=[ticket.pk]),
            {"content": "Bonjour"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_resoudre_exige_de_dire_comment(self, agent: User, ticket: SupportTicket) -> None:
        response = connecte(agent).post(
            reverse("v1:support:managed-ticket-status", args=[ticket.pk]),
            {"status": TicketStatus.RESOLVED},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.OPEN

    def test_resoudre_date_la_resolution_et_rouvrir_l_efface(
        self, agent: User, ticket: SupportTicket
    ) -> None:
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-ticket-status", args=[ticket.pk]),
            {"status": TicketStatus.RESOLVED, "resolution": "Débit en double annulé."},
            format="json",
        )
        ticket.refresh_from_db()
        assert ticket.resolved_at is not None
        assert Notification.objects.filter(
            user=ticket.user, title__startswith="Demande résolue"
        ).exists()

        client.post(
            reverse("v1:support:managed-ticket-status", args=[ticket.pk]),
            {"status": TicketStatus.OPEN},
            format="json",
        )
        ticket.refresh_from_db()
        # Un ticket rouvert n'est plus résolu : garder la date ferait mentir
        # les délais de traitement.
        assert ticket.resolved_at is None


class TestReclamations:
    @pytest.fixture
    def reclamation(self, order: Order, customer: User) -> Complaint:
        return SupportService.file_complaint(
            user=customer,
            order=order,
            kind=ComplaintKind.QUALITY,
            subject="Plat froid",
            description="Arrivé tiède.",
        )

    def test_l_equipe_de_la_cuisine_est_prevenue(
        self, agent: User, order: Order, customer: User
    ) -> None:
        SupportService.file_complaint(
            user=customer,
            order=order,
            kind=ComplaintKind.QUALITY,
            subject="Plat froid",
            description="Arrivé tiède.",
        )

        assert Notification.objects.filter(user=agent, title="Nouvelle réclamation").exists()

    def test_une_autre_cuisine_ne_la_voit_pas(
        self, ailleurs: Restaurant, reclamation: Complaint
    ) -> None:
        kara = personnel("kara@elcorazon.test", ailleurs, "support.read", "support.write")

        liste = connecte(kara).get(reverse("v1:support:managed-complaint-list")).data
        decision = connecte(kara).post(
            reverse("v1:support:managed-complaint-decide", args=[reclamation.pk]),
            {"status": ComplaintStatus.RESOLVED, "resolution": "x"},
            format="json",
        )

        assert liste["results"] == []
        assert decision.status_code == status.HTTP_404_NOT_FOUND

    def test_statuer_exige_une_reponse_et_la_transmet(
        self, agent: User, reclamation: Complaint
    ) -> None:
        client = connecte(agent)
        sans = client.post(
            reverse("v1:support:managed-complaint-decide", args=[reclamation.pk]),
            {"status": ComplaintStatus.REJECTED},
            format="json",
        )
        avec = client.post(
            reverse("v1:support:managed-complaint-decide", args=[reclamation.pk]),
            {
                "status": ComplaintStatus.RESOLVED,
                "resolution": "Un avoir de 1 000 F vous est offert.",
            },
            format="json",
        )

        assert sans.status_code == status.HTTP_409_CONFLICT
        assert avec.status_code == status.HTTP_200_OK
        avis = Notification.objects.get(user=reclamation.user, kind=NotificationKind.SUPPORT)
        assert "avoir" in avis.body

    def test_une_reclamation_close_ne_se_rejuge_pas(
        self, agent: User, reclamation: Complaint
    ) -> None:
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-complaint-decide", args=[reclamation.pk]),
            {"status": ComplaintStatus.RESOLVED, "resolution": "Réglé."},
            format="json",
        )

        response = client.post(
            reverse("v1:support:managed-complaint-decide", args=[reclamation.pk]),
            {"status": ComplaintStatus.REJECTED, "resolution": "Finalement non."},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestRetours:
    @pytest.fixture
    def retour(self, order: Order, customer: User) -> ReturnRequest:
        return SupportService.request_return(
            user=customer,
            order=livrer(order),
            reason="Boisson manquante",
            items=["Bissap"],
            refund_amount=Money(500, XOF),
        )

    def test_refuser_exige_un_motif_que_le_client_lit(
        self, agent: User, retour: ReturnRequest, as_customer_of: APIClient
    ) -> None:
        client = connecte(agent)
        sans = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REJECTED},
            format="json",
        )
        avec = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REJECTED, "resolution": "La boisson figure au bon de sortie."},
            format="json",
        )

        assert sans.status_code == status.HTTP_409_CONFLICT
        assert avec.status_code == status.HTTP_200_OK
        # Le client lit le motif depuis sa propre route.
        vue_client = as_customer_of.get(reverse("v1:support:return-detail", args=[retour.pk])).data
        assert vue_client["resolution"] == "La boisson figure au bon de sortie."

    def test_un_retour_refuse_ne_devient_pas_rembourse(
        self, agent: User, retour: ReturnRequest
    ) -> None:
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REJECTED, "resolution": "Non."},
            format="json",
        )

        response = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REFUNDED},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        retour.refresh_from_db()
        assert retour.status == ReturnStatus.REJECTED

    def test_rembourse_ne_se_pose_que_sur_un_retour_approuve(
        self, agent: User, retour: ReturnRequest
    ) -> None:
        client = connecte(agent)
        directement = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REFUNDED},
            format="json",
        )
        client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.APPROVED},
            format="json",
        )
        rembourser(retour.order, Money(500, XOF), agent)
        ensuite = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REFUNDED},
            format="json",
        )

        assert directement.status_code == status.HTTP_409_CONFLICT
        assert ensuite.status_code == status.HTTP_200_OK
        retour.refresh_from_db()
        assert retour.resolved_at is not None

    def test_on_ne_dit_pas_rembourse_tant_que_rien_n_est_parti(
        self, agent: User, retour: ReturnRequest
    ) -> None:
        """Le statut se posait d'un clic, sans qu'aucun remboursement n'existe :
        le client lisait « Retour remboursé » et attendait un virement que
        personne n'avait fait."""
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.APPROVED},
            format="json",
        )

        response = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REFUNDED},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        retour.refresh_from_db()
        assert retour.status == ReturnStatus.APPROVED
        assert not Notification.objects.filter(title__startswith="Retour remboursé").exists()

    def test_un_remboursement_partiel_suffit_a_le_constater(
        self, agent: User, retour: ReturnRequest
    ) -> None:
        """Un geste commercial partiel reste un remboursement : exiger le
        montant demandé laisserait la demande « approuvée » à vie."""
        client = connecte(agent)
        client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.APPROVED},
            format="json",
        )
        rembourser(retour.order, Money(200, XOF), agent)

        response = client.post(
            reverse("v1:support:managed-return-decide", args=[retour.pk]),
            {"status": ReturnStatus.REFUNDED},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

    def test_la_demande_previent_la_cuisine(
        self, agent: User, order: Order, customer: User
    ) -> None:
        SupportService.request_return(
            user=customer,
            order=livrer(order),
            reason="Boisson manquante",
            items=["Bissap"],
            refund_amount=Money(500, XOF),
        )

        assert Notification.objects.filter(user=agent, title="Demande de retour").exists()


@pytest.fixture
def as_customer_of(customer: User) -> APIClient:
    return connecte(customer)


class TestLeFilDuTicket:
    """Ce qui arrive après la première réponse — et que personne ne voyait."""

    def test_la_relance_du_client_rouvre_un_ticket_resolu(
        self, agent: User, ticket: SupportTicket, customer: User
    ) -> None:
        """**Le défaut.** Le back-office filtre « à traiter » sur *ouvert* et
        *en cours* : la relance tombait dans un dossier « résolu », et
        n'apparaissait donc sur aucun écran."""
        connecte(agent).post(
            reverse("v1:support:managed-ticket-status", args=[ticket.pk]),
            {"status": TicketStatus.RESOLVED, "resolution": "Remboursement envoyé."},
            format="json",
        )

        connecte(customer).post(
            reverse("v1:support:ticket-messages", args=[ticket.pk]),
            {"content": "Je n'ai toujours rien reçu."},
            format="json",
        )

        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.OPEN
        assert ticket.resolved_at is None
        a_traiter = connecte(agent).get(
            reverse("v1:support:managed-ticket-list"),
            {"status__in": f"{TicketStatus.OPEN},{TicketStatus.IN_PROGRESS}"},
        )
        assert [ligne["id"] for ligne in a_traiter.data["results"]] == [str(ticket.pk)]

    def test_une_relance_sur_un_ticket_en_cours_ne_change_rien(
        self, agent: User, ticket: SupportTicket, customer: User
    ) -> None:
        """C'est le même échange qui continue."""
        connecte(agent).post(
            reverse("v1:support:managed-ticket-reply", args=[ticket.pk]),
            {"content": "Nous regardons."},
            format="json",
        )

        connecte(customer).post(
            reverse("v1:support:ticket-messages", args=[ticket.pk]),
            {"content": "Merci."},
            format="json",
        )

        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.IN_PROGRESS

    def test_resoudre_deux_fois_ne_previent_qu_une_fois(
        self, agent: User, ticket: SupportTicket, customer: User
    ) -> None:
        """Deux agents sur le même dossier : le client recevait deux fois
        « Demande résolue »."""
        url = reverse("v1:support:managed-ticket-status", args=[ticket.pk])
        charge = {"status": TicketStatus.RESOLVED, "resolution": "Remboursement envoyé."}

        premier = connecte(agent).post(url, charge, format="json")
        second = connecte(agent).post(url, charge, format="json")

        assert premier.status_code == second.status_code == status.HTTP_200_OK
        assert (
            Notification.objects.filter(
                user=customer, kind=NotificationKind.SUPPORT, title__startswith="Demande résolue"
            ).count()
            == 1
        )
