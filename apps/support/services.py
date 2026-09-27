"""Ouverture de tickets, réclamations et demandes de retour.

La règle commune aux trois : un client n'agit que sur **sa** commande. Ni
`Complaint` ni `ReturnRequest` ne déclarent de relation entre `user` et
`order` qu'une contrainte de base saurait vérifier — la propriété se vérifie
donc ici, à la création, sur le modèle de S3 (partage de commande) : la
réclamation de quelqu'un d'autre n'est pas une ressource qu'on refuse, c'est
une ressource dont on tait jusqu'à l'existence.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentStatus, Refund
from apps.support.models import (
    Complaint,
    ComplaintKind,
    ComplaintStatus,
    ReturnRequest,
    ReturnStatus,
    SupportMessage,
    SupportTicket,
    TicketCategory,
    TicketStatus,
)
from apps.support.signals import (
    complaint_decided,
    complaint_filed,
    return_decided,
    return_requested,
    ticket_answered,
    ticket_status_changed,
)
from common.audit import AuditAction, record_change
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["SupportDeskService", "SupportService"]

_TICKET_TERMINAL = frozenset({TicketStatus.RESOLVED, TicketStatus.CLOSED})
_COMPLAINT_TERMINAL = frozenset({ComplaintStatus.RESOLVED, ComplaintStatus.REJECTED})

#: Ce qu'une demande de retour peut devenir, depuis chaque état.
#:
#: Pas une machine à états complète — le module s'en passe à dessein (voir
#: `models`) — mais deux règles qu'il faut tenir, parce qu'elles touchent à
#: l'argent : un retour **refusé** ne devient pas remboursé par un clic
#: suivant, et « remboursé » ne se pose que sur un retour approuvé.
RETURN_TRANSITIONS: dict[str, frozenset[str]] = {
    ReturnStatus.PENDING: frozenset({ReturnStatus.APPROVED, ReturnStatus.REJECTED}),
    ReturnStatus.APPROVED: frozenset({ReturnStatus.REFUNDED, ReturnStatus.REJECTED}),
    ReturnStatus.REJECTED: frozenset(),
    ReturnStatus.REFUNDED: frozenset(),
}


class SupportService:
    @staticmethod
    def open_ticket(
        *,
        user: User,
        category: str = TicketCategory.OTHER,
        subject: str,
        description: str,
        attachments: list[str] | None = None,
    ) -> SupportTicket:
        return SupportTicket.objects.create(
            user=user,
            category=category,
            subject=subject,
            description=description,
            attachments=attachments or [],
        )

    @staticmethod
    @transaction.atomic
    def reply(*, ticket: SupportTicket, author: User, content: str) -> SupportMessage:
        """Le client écrit sur son fil — et **rouvre** le ticket s'il était clos.

        Sans cette réouverture, le message tombait dans un ticket « résolu » :
        le back-office filtre « à traiter » sur *ouvert* et *en cours*, si bien
        que la relance d'un client mécontent n'apparaissait sur aucun écran.
        Elle ne se découvrait qu'en rouvrant un dossier que rien ne signalait.

        Un ticket déjà ouvert ou en cours ne bouge pas : c'est le même échange
        qui continue.
        """
        message = SupportMessage.objects.create(ticket=ticket, author=author, content=content)
        verrouille = SupportTicket.objects.select_for_update().get(pk=ticket.pk)
        if verrouille.status in _TICKET_TERMINAL:
            verrouille.status = TicketStatus.OPEN
            verrouille.resolved_at = None
            verrouille.save(update_fields=["status", "resolved_at", "updated_at"])
            ticket.status = verrouille.status
            ticket.resolved_at = None
        return message

    @staticmethod
    def file_complaint(
        *,
        user: User,
        order: Order,
        kind: str = ComplaintKind.OTHER,
        subject: str,
        description: str,
        photos: list[str] | None = None,
    ) -> Complaint:
        if order.customer_id != user.pk:
            raise BusinessRuleViolation("Vous ne pouvez réclamer que sur vos propres commandes.")

        complaint = Complaint.objects.create(
            user=user,
            order=order,
            kind=kind,
            subject=subject,
            description=description,
            photos=photos or [],
        )
        complaint_filed.send(sender=Complaint, complaint=complaint)
        return complaint

    @staticmethod
    def request_return(
        *, user: User, order: Order, reason: str, items: list[str], refund_amount: Money
    ) -> ReturnRequest:
        """Enregistre une demande — ne rembourse rien.

        Deux gardes avant l'écriture : la commande doit être **livrée** (on ne
        retourne pas un repas qu'on n'a pas reçu), et le montant demandé ne
        peut pas dépasser ce que la commande a coûté — le même plafond que P3
        applique au remboursement réel, posé ici avant même que la demande
        n'atteigne quiconque.
        """
        if order.customer_id != user.pk:
            raise BusinessRuleViolation("Vous ne pouvez retourner que vos propres commandes.")

        if order.status != OrderStatus.DELIVERED:
            raise BusinessRuleViolation("Seule une commande livrée peut faire l'objet d'un retour.")

        if refund_amount.currency != order.total.currency or refund_amount > order.total:
            raise BusinessRuleViolation(
                f"Le montant demandé dépasse le total de la commande ({order.total}).",
                order_total=str(order.total.amount_minor),
                currency=order.total.currency,
            )

        demande = ReturnRequest.objects.create(  # type: ignore[misc]
            user=user, order=order, reason=reason, items=items, refund_amount=refund_amount
        )
        return_requested.send(sender=ReturnRequest, return_request=demande)
        return demande


class SupportDeskService:
    """Le côté **personnel** du support : répondre, statuer.

    Rien de tout cela n'existait hors de l'administration Django. Un client
    écrivait, et n'apprenait jamais qu'on l'avait lu : aucune route ne
    permettait au back-office de lui répondre, et une réponse saisie dans
    l'administration ne partait vers personne.

    Chaque geste émet un signal que `notifications` relaie au client. Le
    motif d'une décision défavorable est **exigé** : c'est la phrase que le
    client lira, et « rejetée » sans raison se lit comme un mépris.
    """

    @staticmethod
    @transaction.atomic
    def answer(*, ticket: SupportTicket, author: User, content: str) -> SupportMessage:
        """Répond sur le fil d'un ticket.

        Un ticket **ouvert** passe « en cours » : quelqu'un s'en occupe, et le
        client le voit. Un ticket résolu ou fermé qui reçoit une réponse est
        rouvert — répondre sur un dossier clos sans le rouvrir laisserait la
        réponse sous un statut qui dit « c'est réglé ».
        """
        message = SupportMessage.objects.create(ticket=ticket, author=author, content=content)
        if ticket.status != TicketStatus.IN_PROGRESS:
            ticket.status = TicketStatus.IN_PROGRESS
            ticket.resolved_at = None
            ticket.save(update_fields=["status", "resolved_at", "updated_at"])
        ticket_answered.send(sender=SupportTicket, ticket=ticket, message=message)
        return message

    @staticmethod
    @transaction.atomic
    def set_ticket_status(
        *, ticket: SupportTicket, status: str, resolution: str = "", actor: User | None = None
    ) -> SupportTicket:
        """Change le statut d'un ticket. Résoudre exige de dire comment.

        La ligne est **relue sous verrou** : deux agents qui résolvent le même
        ticket à la même seconde lisaient tous deux « en cours » et émettaient
        chacun leur signal, si bien que le client recevait deux fois
        « Demande résolue ». Le verrou fait que le second lit l'état écrit par
        le premier, et se tait.
        """
        ticket = SupportTicket.objects.select_for_update().get(pk=ticket.pk)
        if status == TicketStatus.RESOLVED and not (resolution or ticket.resolution).strip():
            raise BusinessRuleViolation("Dites au client comment son problème a été résolu.")

        avant = ticket.status
        ticket.status = status
        if resolution.strip():
            ticket.resolution = resolution.strip()
        # L'horodatage suit le statut dans les deux sens : un ticket rouvert
        # n'est plus résolu, et garder sa date ferait mentir les délais.
        if status in _TICKET_TERMINAL:
            ticket.resolved_at = ticket.resolved_at or timezone.now()
        else:
            ticket.resolved_at = None
        ticket.save(update_fields=["status", "resolution", "resolved_at", "updated_at"])

        if avant != status:
            if status in _TICKET_TERMINAL:
                record_change(
                    actor=actor,
                    action=AuditAction.TICKET_RESOLUTION,
                    target_type="ticket",
                    target_id=ticket.pk,
                    target_label=ticket.subject,
                    before={"status": avant},
                    after={"status": status, "resolution": ticket.resolution},
                )
            ticket_status_changed.send(sender=SupportTicket, ticket=ticket)
        return ticket

    @staticmethod
    @transaction.atomic
    def decide_complaint(
        *, complaint: Complaint, status: str, resolution: str = "", actor: User | None = None
    ) -> Complaint:
        """Statue sur une réclamation. Une décision finale exige un motif."""
        complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)
        if complaint.status in _COMPLAINT_TERMINAL:
            raise BusinessRuleViolation(
                "Cette réclamation est déjà close : ouvrez un ticket pour la reprendre.",
                current_status=complaint.status,
            )
        if status in _COMPLAINT_TERMINAL and not resolution.strip():
            raise BusinessRuleViolation("Le client lira la réponse : elle est obligatoire.")

        avant = complaint.status
        complaint.status = status
        if resolution.strip():
            complaint.resolution = resolution.strip()
        complaint.save(update_fields=["status", "resolution", "updated_at"])

        if status in _COMPLAINT_TERMINAL:
            record_change(
                actor=actor,
                action=AuditAction.COMPLAINT_DECISION,
                target_type="complaint",
                target_id=complaint.pk,
                target_label=f"{complaint.subject} — commande {complaint.order.reference}",
                before={"status": avant},
                after={"status": status, "resolution": complaint.resolution},
                scope_restaurant_id=complaint.order.restaurant_id,
            )
            complaint_decided.send(sender=Complaint, complaint=complaint)
        return complaint

    @staticmethod
    @transaction.atomic
    def decide_return(
        *,
        return_request: ReturnRequest,
        status: str,
        resolution: str = "",
        actor: User | None = None,
    ) -> ReturnRequest:
        """Statue sur une demande de retour — **sans rembourser**.

        « Remboursée » **constate** un remboursement fait par `payments` (fiche
        de la commande, puis « Remboursements ») ; elle ne le déclenche pas —
        un statut qui verserait de l'argent serait un second chemin, sans le
        plafond P3.

        Encore faut-il qu'il ait eu lieu. Rien ne le vérifiait : le statut se
        posait d'un clic, et le client recevait « Retour remboursé » en
        attendant un virement que personne n'avait fait. Le constat est donc
        opposé à ce que `payments` a réellement soldé sur cette commande — une
        lecture, la seule que `support` fasse des paiements.
        """
        demande = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        permis = RETURN_TRANSITIONS.get(demande.status, frozenset())
        if status not in permis:
            raise BusinessRuleViolation(
                f"Une demande « {demande.get_status_display()} » ne peut pas devenir "
                f"« {ReturnStatus(status).label} ».",
                current_status=demande.status,
                allowed=sorted(permis),
            )
        if status == ReturnStatus.REJECTED and not resolution.strip():
            raise BusinessRuleViolation("Dites au client pourquoi son retour est refusé.")
        if status == ReturnStatus.REFUNDED:
            SupportDeskService._assert_rembourse(demande)

        avant = demande.status
        demande.status = status
        if resolution.strip():
            demande.resolution = resolution.strip()
        if status in (ReturnStatus.REJECTED, ReturnStatus.REFUNDED):
            demande.resolved_at = timezone.now()
        demande.save(update_fields=["status", "resolution", "resolved_at", "updated_at"])

        record_change(
            actor=actor,
            action=AuditAction.RETURN_DECISION,
            target_type="return",
            target_id=demande.pk,
            target_label=f"{demande.refund_amount} — commande {demande.order.reference}",
            before={"status": avant},
            after={
                "status": status,
                "resolution": demande.resolution,
                "amount": str(demande.refund_amount.amount_minor),
                "currency": demande.refund_amount.currency,
            },
            scope_restaurant_id=demande.order.restaurant_id,
        )
        return_decided.send(sender=ReturnRequest, return_request=demande)
        return demande

    @staticmethod
    def _assert_rembourse(demande: ReturnRequest) -> None:
        """Refuse de dire « remboursé » quand rien n'a été versé.

        La comparaison porte sur le **cumul soldé** de la commande et non sur le
        montant demandé : un geste commercial partiel — la moitié rendue, après
        discussion — reste un remboursement, et le refuser obligerait à mentir
        dans l'autre sens, en laissant la demande « approuvée » à vie.

        Ce qui est refusé est le cas où **rien** n'est parti.
        """
        rendu = Refund.objects.filter(
            order_id=demande.order_id, status=PaymentStatus.COMPLETED
        ).exists()
        if not rendu:
            raise BusinessRuleViolation(
                "Aucun remboursement n'a encore été constaté sur cette commande : "
                "enregistrez-le dans Caisse › Remboursements avant de le dire au client.",
                order=demande.order.reference,
            )
