"""Ouverture de tickets, réclamations et demandes de retour.

La règle commune aux trois : un client n'agit que sur **sa** commande. Ni
`Complaint` ni `ReturnRequest` ne déclarent de relation entre `user` et
`order` qu'une contrainte de base saurait vérifier — la propriété se vérifie
donc ici, à la création, sur le modèle de S3 (partage de commande) : la
réclamation de quelqu'un d'autre n'est pas une ressource qu'on refuse, c'est
une ressource dont on tait jusqu'à l'existence.
"""

from __future__ import annotations

from apps.accounts.models import User
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.support.models import (
    Complaint,
    ComplaintKind,
    ReturnRequest,
    SupportMessage,
    SupportTicket,
    TicketCategory,
)
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["SupportService"]


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
    def reply(*, ticket: SupportTicket, author: User, content: str) -> SupportMessage:
        return SupportMessage.objects.create(ticket=ticket, author=author, content=content)

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

        return Complaint.objects.create(
            user=user,
            order=order,
            kind=kind,
            subject=subject,
            description=description,
            photos=photos or [],
        )

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

        return ReturnRequest.objects.create(  # type: ignore[misc]
            user=user, order=order, reason=reason, items=items, refund_amount=refund_amount
        )
