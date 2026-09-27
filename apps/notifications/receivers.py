"""Abonnements aux événements de domaine — ADR-002, ADR-008.

C'est ici que `notifications` réagit à ce que font les autres apps. La flèche va
dans ce sens et pas dans l'autre : `orders` et `delivery` annoncent sans savoir
qui écoute, ce module écoute sans qu'ils le sachent. Ajouter une notification
sur un événement existant ne touche aucune autre app.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import CourierService
from apps.delivery.signals import (
    assignment_accepted,
    assignment_cancelled,
    assignment_offered,
    document_expiring,
    verification_decided,
)
from apps.delivery.states import VerificationStatus
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, staff_to_alert
from apps.orders.models import Order
from apps.orders.signals import order_created, order_status_changed
from apps.orders.states import OrderStatus
from apps.payments.models import Refund, Transaction, Withdrawal
from apps.payments.signals import (
    payment_transaction_failed,
    payment_transaction_settled,
    refund_settled,
    withdrawal_failed,
    withdrawal_requested,
    withdrawal_settled,
)
from apps.restaurants.models import Restaurant
from apps.restaurants.signals import restaurant_status_changed
from apps.restaurants.states import RestaurantStatus
from apps.support.models import (
    Complaint,
    ComplaintStatus,
    ReturnRequest,
    ReturnStatus,
    SupportMessage,
    SupportTicket,
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

__all__ = [
    "on_assignment_accepted",
    "on_assignment_offered",
    "on_complaint_decided",
    "on_complaint_filed",
    "on_document_expiring",
    "on_order_created_for_staff",
    "on_order_status_changed",
    "on_order_status_changed_for_staff",
    "on_payment_failed",
    "on_payment_to_refund",
    "on_refund_settled",
    "on_restaurant_status_changed",
    "on_return_decided",
    "on_return_requested",
    "on_ticket_answered",
    "on_ticket_status_changed",
    "on_verification_decided",
    "on_withdrawal_failed",
    "on_withdrawal_requested",
    "on_withdrawal_settled",
]

#: Permission qu'il faut détenir pour être prévenu d'une demande de retrait —
#: celle que `ManagedWithdrawalViewSet` oppose à la lecture.
PAYOUTS_READ = "payouts.read"

#: Permission qu'il faut détenir pour être prévenu d'un événement de commande.
#:
#: C'est celle que l'écran opposera ensuite (`HasPermission.of("orders.read")`).
#: Alerter au-delà produirait des notifications qui mènent à un 403.
ORDERS_READ = "orders.read"

#: Étapes annoncées au client, et ce qu'on lui dit.
#:
#: La liste est délibérément courte. `preparing` et `ready` sont des étapes de
#: cuisine : les annoncer ferait vibrer le téléphone sans rien apprendre
#: d'actionnable. Notifier chaque transition est le meilleur moyen de se faire
#: couper les notifications — et de perdre du même coup celles qui comptent.
CUSTOMER_ANNOUNCEMENTS: dict[str, tuple[str, str]] = {
    OrderStatus.CONFIRMED: ("Commande confirmée", "Votre commande {reference} est confirmée."),
    OrderStatus.ON_THE_WAY: ("En route", "Votre commande {reference} arrive."),
    OrderStatus.DELIVERED: ("Livrée", "Votre commande {reference} a été livrée. Bon appétit !"),
    OrderStatus.CANCELLED: ("Commande annulée", "Votre commande {reference} a été annulée."),
}


@receiver(order_status_changed, sender=Order, dispatch_uid="notifications.order_status")
def on_order_status_changed(
    sender: type[Order], *, order: Order, target: str, **kwargs: Any
) -> None:
    """Prévient le client des étapes qui le concernent.

    `dispatch_uid` protège du double abonnement : sans lui, un module importé
    deux fois — ce qui arrive au rechargement automatique en développement —
    enverrait deux notifications par transition, et le défaut ne se verrait
    qu'à l'usage.
    """
    message = CUSTOMER_ANNOUNCEMENTS.get(target)
    if message is None:
        return

    title, body = message
    notify(
        user=order.customer,
        kind=NotificationKind.ORDER_STATUS,
        title=title,
        body=body.format(reference=order.reference),
        data={"order": str(order.pk), "status": target},
    )


#: Étapes annoncées au **personnel**, et ce qu'on lui dit.
#:
#: Volontairement disjointe de `CUSTOMER_ANNOUNCEMENTS` : les deux publics
#: n'attendent pas les mêmes moments. Le client veut savoir où en est son repas ;
#: l'exploitation veut savoir ce qui **entre** et ce qui **casse**. Personne au
#: back-office n'a besoin d'apprendre qu'une commande a été livrée normalement —
#: c'est le cas nominal, et le notifier noierait les deux qui comptent.
STAFF_ANNOUNCEMENTS: dict[str, tuple[str, str]] = {
    # « Nouvelle commande » a changé de place : c'est l'**arrivée** qui porte ce
    # titre désormais (`on_order_created_for_staff`), et non la confirmation.
    # Les deux moments sont distincts et le personnel doit pouvoir les
    # distinguer — surtout quand ils sont séparés par son propre geste, ce qui
    # est le cas d'un règlement en espèces.
    OrderStatus.CONFIRMED: ("Commande confirmée", "La commande {reference} est à préparer."),
    OrderStatus.CANCELLED: ("Commande annulée", "La commande {reference} a été annulée."),
}


@receiver(order_created, sender=Order, dispatch_uid="notifications.order_created_staff")
def on_order_created_for_staff(sender: type[Order], *, order: Order, **kwargs: Any) -> None:
    """Prévient l'établissement qu'une commande vient d'arriver.

    C'est le premier maillon, et il manquait. `STAFF_ANNOUNCEMENTS` est indexé
    sur des **transitions** ; or une commande naît en `pending` et la seule voie
    automatique vers `confirmed` est l'encaissement par webhook. Le règlement en
    espèces à la livraison — aujourd'hui le seul moyen actif dans l'application
    cliente — n'en émet aucun. Une commande passée un vendredi soir n'était donc
    annoncée à personne : elle attendait qu'un membre du personnel rafraîchisse
    la liste et remarque la ligne.

    Le client, lui, n'est pas notifié ici : il vient de valider sa commande et a
    l'écran de confirmation sous les yeux. Lui envoyer une notification pour le
    geste qu'il achève à l'instant est le genre d'envoi qui fait couper les
    notifications — et perdre du même coup celles qui comptent.
    """
    for membre in staff_to_alert(restaurant_id=order.restaurant_id, permission=ORDERS_READ):
        notify(
            user=membre,
            kind=NotificationKind.ORDER_STATUS,
            title="Nouvelle commande",
            body=f"Commande {order.reference} reçue.",
            data={"order": str(order.pk), "status": order.status},
        )


@receiver(order_status_changed, sender=Order, dispatch_uid="notifications.order_status_staff")
def on_order_status_changed_for_staff(
    sender: type[Order], *, order: Order, target: str, **kwargs: Any
) -> None:
    """Prévient le personnel de l'établissement concerné.

    Second abonné au **même** signal, avec son propre `dispatch_uid` : le
    partager avec celui du client ferait que le dernier enregistré remplace
    l'autre en silence — Django indexe ses abonnements sur cet identifiant.

    Le destinataire n'est pas « les administrateurs » mais le personnel
    rattaché à *cet* établissement et habilité à lire les commandes — voir
    `staff_to_alert`. Un client ne reçoit jamais rien par ce chemin : la
    population est filtrée sur `user_type=staff`.
    """
    message = STAFF_ANNOUNCEMENTS.get(target)
    if message is None:
        return

    title, body = message
    for membre in staff_to_alert(restaurant_id=order.restaurant_id, permission=ORDERS_READ):
        notify(
            user=membre,
            kind=NotificationKind.ORDER_STATUS,
            title=title,
            body=body.format(reference=order.reference),
            data={"order": str(order.pk), "status": target},
        )


@receiver(
    payment_transaction_failed,
    sender=Transaction,
    dispatch_uid="notifications.payment_failed",
)
def on_payment_failed(
    sender: type[Transaction], *, transaction: Transaction, **kwargs: Any
) -> None:
    """Prévient le client qu'un paiement a échoué, et l'exploitation avec lui.

    Un paiement refusé était **entièrement muet** : la transaction passait en
    `failed`, la commande restait où elle était, et personne n'apprenait rien.
    Le client attendait devant une commande qui n'avancerait jamais, et le
    back-office la voyait vieillir sans savoir pourquoi. `NotificationKind.PAYMENT`
    existait dans l'énumération sans être émis une seule fois.

    C'est la seule notification qui parte aux deux publics pour un même
    événement, et c'est justifié : le client seul peut reprendre le paiement,
    l'exploitation seule peut le relancer ou libérer la commande.
    """
    order = transaction.order
    if order is None:
        # Un abonnement, un rechargement : rien à dire de plus que ce que
        # l'écran de paiement montre déjà, et aucune commande à désigner.
        return

    notify(
        user=order.customer,
        kind=NotificationKind.PAYMENT,
        title="Paiement refusé",
        body=f"Le paiement de la commande {order.reference} n'a pas abouti. Vous pouvez réessayer.",
        data={"order": str(order.pk), "transaction": str(transaction.pk)},
    )

    for membre in staff_to_alert(restaurant_id=order.restaurant_id, permission=ORDERS_READ):
        notify(
            user=membre,
            kind=NotificationKind.PAYMENT,
            title="Paiement en échec",
            body=f"Le paiement de la commande {order.reference} a échoué.",
            data={"order": str(order.pk), "transaction": str(transaction.pk)},
        )


@receiver(
    payment_transaction_settled,
    sender=Transaction,
    dispatch_uid="notifications.payment_to_refund",
)
def on_payment_to_refund(
    sender: type[Transaction], *, transaction: Transaction, **kwargs: Any
) -> None:
    """Prévient l'exploitation d'un encaissement que la commande n'appelait pas.

    Deux cas, que rien ne relevait : la commande est **déjà soldée** — deux
    demandes de paiement ouvertes, validées toutes les deux — ou elle est
    **annulée**, et la notification du prestataire arrive après coup. Le
    webhook ne peut pas refuser l'argent, déjà pris chez le prestataire ; il
    l'enregistre, et `_confirm_order` ne confirme rien. Sans cette alerte, la
    somme restait chez nous sans que personne le sache.

    Le client n'est pas prévenu ici : c'est le remboursement, une fois fait,
    qui le concerne.

    Lit `order.amount_paid`, que `report_settled_total` a mis à jour **avant**
    l'émission du signal.
    """
    order = transaction.order
    if order is None:
        return

    order.refresh_from_db(fields=["status", "amount_paid_minor", "amount_paid_currency"])
    encaisse = order.amount_paid
    if encaisse is None or not encaisse.is_positive:
        return
    annulee = order.status == OrderStatus.CANCELLED
    if not (annulee or encaisse > order.total):
        return

    motif = "commande annulée" if annulee else f"déjà réglée ({order.total})"
    for membre in staff_to_alert(restaurant_id=order.restaurant_id, permission=ORDERS_READ):
        notify(
            user=membre,
            kind=NotificationKind.PAYMENT,
            title="Encaissement à rembourser",
            body=(
                f"{transaction.amount} encaissés sur la commande {order.reference}, {motif}. "
                "À rembourser au client."
            ),
            data={"order": str(order.pk), "transaction": str(transaction.pk)},
        )


@receiver(refund_settled, sender=Refund, dispatch_uid="notifications.refund_settled")
def on_refund_settled(sender: type[Refund], *, refund: Refund, **kwargs: Any) -> None:
    """Dit au client que son argent lui est rendu, combien, et pour quelle commande.

    Au **versement** constaté, pas à la demande : `RefundService.refund` n'écrit
    qu'une intention, que l'exploitation peut encore abandonner. Annoncer
    « remboursé » à ce moment-là serait promettre ce qui n'est pas fait.
    """
    order = refund.order
    notify(
        user=order.customer,
        kind=NotificationKind.PAYMENT,
        title="Remboursement effectué",
        body=f"{refund.amount} vous ont été remboursés pour la commande {order.reference}.",
        data={"order": str(order.pk), "refund": str(refund.pk)},
    )


@receiver(assignment_accepted, sender=Assignment, dispatch_uid="notifications.delivery_accepted")
def on_assignment_accepted(
    sender: type[Assignment], *, assignment: Assignment, **kwargs: Any
) -> None:
    """Prévient le client qu'un livreur a pris sa commande.

    L'acceptation et non la proposition : une course proposée peut être
    refusée, et annoncer un livreur qui ne viendra pas est pire que de ne rien
    dire.

    C'est le seul moment du parcours où le client n'apprenait rien alors qu'il
    se passait quelque chose : `accepted` n'est volontairement pas projeté sur
    le statut de la commande — le repas n'est pas parti, elle reste `ready` —
    si bien qu'entre « confirmée » et « en route » il n'y avait aucun signe de
    vie. C'est précisément l'intervalle où l'on se demande si quelqu'un a vu la
    commande.
    """
    order = assignment.order
    notify(
        user=order.customer,
        kind=NotificationKind.ORDER_STATUS,
        title="Un livreur arrive",
        body=(
            f"{assignment.courier.user.full_name} prend en charge votre commande {order.reference}."
        ),
        data={"order": str(order.pk), "assignment": str(assignment.pk)},
    )


@receiver(assignment_offered, sender=Assignment, dispatch_uid="notifications.delivery_offer")
def on_assignment_offered(
    sender: type[Assignment], *, assignment: Assignment, **kwargs: Any
) -> None:
    """Prévient le livreur qu'une course l'attend.

    C'est le seul flux où rater un événement a un coût métier direct (ADR-008) :
    le livreur n'a pas son application au premier plan en roulant, et une
    course non vue est un repas qui refroidit. Le WebSocket ne suffit donc pas
    — la notification le double.
    """
    order = assignment.order
    notify(
        user=assignment.courier.user,
        kind=NotificationKind.DELIVERY_OFFER,
        title="Nouvelle course",
        body=f"{order.restaurant.name} — {order.delivery_address_line}",
        data={"assignment": str(assignment.pk), "order": str(order.pk)},
    )


@receiver(assignment_cancelled, sender=Assignment, dispatch_uid="notifications.delivery_cancelled")
def on_assignment_cancelled(
    sender: type[Assignment], *, assignment: Assignment, reason: str = "", **kwargs: Any
) -> None:
    """Prévient le livreur qu'on lui a retiré sa course.

    ## Le défaut que ce receveur ferme

    Rien ne le lui disait. L'annulation par le personnel diffusait bien un
    événement, mais sur le canal de la **commande** (`order_group`) — celui que
    le client écoute pour suivre sa livraison. Le livreur, lui, n'écoute que sa
    propre file (`ws/couriers/me/`).

    Concrètement : un livreur en route vers le restaurant continuait d'y aller,
    et l'apprenait de la cuisine en arrivant. C'est le pendant exact de
    `on_assignment_offered`, et il manquait — on savait lui confier une course,
    pas la lui reprendre.

    ## Pourquoi le motif est repris

    Une annulation sans raison se lit comme une sanction. Le personnel en
    saisit une (`decline_reason`), et c'est elle qui distingue « le client a
    annulé » d'« on vous retire cette course ». Absent, on ne prétend pas en
    avoir un.
    """
    order = assignment.order
    notify(
        user=assignment.courier.user,
        kind=NotificationKind.DELIVERY_OFFER,
        title="Course annulée",
        body=(
            f"{order.restaurant.name} — {reason}"
            if reason
            else f"La course pour {order.restaurant.name} vous a été retirée."
        ),
        data={"assignment": str(assignment.pk), "order": str(order.pk)},
    )


#: Permission qu'il faut détenir pour être prévenu d'un événement d'établissement.
#:
#: C'est celle que l'écran opposera ensuite (`restaurants.read`). Alerter
#: au-delà produirait des notifications qui mènent à un 403.
RESTAURANTS_READ = "restaurants.read"

#: Ce qu'on annonce, selon l'état d'arrivée.
#:
#: Indexé sur la cible et non sur la paire (précédent, cible) : seule
#: l'inauguration se distingue d'une réouverture, et elle se lit sur le
#: précédent au moment de composer le message. Une table à quinze entrées pour
#: cette seule nuance serait plus difficile à relire que la condition.
LIFECYCLE_ANNOUNCEMENTS: dict[str, tuple[str, str]] = {
    RestaurantStatus.ACTIVE: (
        "{name} est en service",
        "L'établissement est visible des clients et prend les commandes.",
    ),
    RestaurantStatus.INACTIVE: (
        "{name} est suspendu",
        "L'établissement n'apparaît plus dans l'application cliente et ne reçoit "
        "plus de commandes.",
    ),
    RestaurantStatus.CONFIGURING: (
        "{name} entre en configuration",
        "L'établissement est retiré de l'application cliente le temps des réglages.",
    ),
    RestaurantStatus.READY: (
        "{name} est prêt à ouvrir",
        "La configuration est complète : la mise en service peut être demandée.",
    ),
}


@receiver(
    restaurant_status_changed,
    sender=Restaurant,
    dispatch_uid="notifications.restaurant_status",
)
def on_restaurant_status_changed(
    sender: type[Restaurant],
    *,
    restaurant: Restaurant,
    previous: str,
    target: str,
    **kwargs: Any,
) -> None:
    """Prévient le personnel qu'un établissement change d'état.

    ## Pourquoi cela manquait

    Suspendre un établissement le fait disparaître de l'application cliente à
    la seconde. Personne n'était prévenu : l'équipe l'apprenait en constatant
    que les commandes ne rentraient plus, et cherchait la panne du côté du
    réseau. Une suspension est une décision, pas un incident — elle doit
    s'annoncer comme telle.

    ## Qui est prévenu

    Le personnel dont le périmètre couvre cet établissement, et qui est habilité
    à le lire (`staff_to_alert`) : ses gérants, mais aussi le directeur du
    marché depuis que le cloisonnement a ce palier. Pas les clients — ils voient
    la conséquence dans l'application, et une notification « le restaurant est
    suspendu » sur le téléphone de quelqu'un qui n'y commande pas serait du
    bruit.

    Pas non plus les livreurs : leur travail dépend des courses proposées, qui
    s'arrêtent d'elles-mêmes quand l'établissement ne prend plus de commandes.
    Les prévenir de l'état d'un établissement les rendrait dépendants d'une
    information qu'ils n'ont pas à suivre.

    ## `account` et non un genre dédié

    C'est une nouvelle d'exploitation qui concerne le compte de celui qui la
    reçoit, comme une suspension de dossier livreur. Créer un genre
    `restaurant_status` obligerait chaque client — trois applications — à savoir
    le ranger avant d'avoir un écran qui l'affiche, et un genre inconnu se
    range mal.
    """
    message = LIFECYCLE_ANNOUNCEMENTS.get(target)
    if message is None:
        return

    titre, corps = message

    # Le sens du geste est dans la **paire** (précédent, cible), pas dans la
    # seule cible. Deux nuances valent d'être dites, parce qu'elles changent ce
    # qu'on comprend en relisant ses notifications trois jours plus tard :
    #
    # * « suspendu → en service » est une réouverture, pas une inauguration ;
    # * « brouillon → en configuration » est un début, alors que « prêt → en
    #   configuration » ou « suspendu → en configuration » est un retour en
    #   arrière. Écrire « repasse » dans les trois cas laissait croire qu'un
    #   établissement qu'on vient de créer avait déjà été configuré une fois.
    #
    # Le reste des transitions ne demande pas cette distinction : leur cible
    # suffit à les décrire.
    if target == RestaurantStatus.ACTIVE and previous == RestaurantStatus.INACTIVE:
        titre = "{name} rouvre"
    elif target == RestaurantStatus.CONFIGURING and previous != RestaurantStatus.DRAFT:
        titre = "{name} repasse en configuration"

    for membre in staff_to_alert(restaurant_id=restaurant.pk, permission=RESTAURANTS_READ):
        notify(
            user=membre,
            kind=NotificationKind.ACCOUNT,
            title=titre.format(name=restaurant.name),
            body=corps,
            data={
                "restaurant": str(restaurant.pk),
                "slug": restaurant.slug,
                "previous": previous,
                "status": target,
            },
        )


@receiver(
    withdrawal_requested, sender=Withdrawal, dispatch_uid="notifications.withdrawal_requested"
)
def on_withdrawal_requested(
    sender: type[Withdrawal], *, withdrawal: Withdrawal, **kwargs: Any
) -> None:
    """Prévient l'exploitation qu'un livreur attend un versement.

    Sans elle, une demande n'apparaissait nulle part : il fallait ouvrir la
    liste des retraits pour découvrir qu'on en attendait un, et le livreur,
    gains déjà débités, attendait sans savoir si quelqu'un l'avait vu.
    """
    courier = withdrawal.courier
    for membre in staff_to_alert(restaurant_id=courier.restaurant_id, permission=PAYOUTS_READ):
        notify(
            user=membre,
            kind=NotificationKind.PAYMENT,
            title="Retrait à verser",
            body=f"{courier.user.full_name} demande le versement de {withdrawal.amount}.",
            data={"withdrawal": str(withdrawal.pk)},
        )


@receiver(withdrawal_settled, sender=Withdrawal, dispatch_uid="notifications.withdrawal_settled")
def on_withdrawal_settled(
    sender: type[Withdrawal], *, withdrawal: Withdrawal, **kwargs: Any
) -> None:
    """Dit au livreur que son versement est parti, et sous quelle référence.

    La référence est ce qu'il donnera à son opérateur de paiement mobile si
    l'argent tarde : sans elle, « c'est versé » ne se vérifie pas.
    """
    notify(
        user=withdrawal.courier.user,
        kind=NotificationKind.PAYMENT,
        title="Retrait versé",
        body=(
            f"Votre retrait de {withdrawal.amount} a été versé "
            f"(référence {withdrawal.provider_reference})."
        ),
        data={"withdrawal": str(withdrawal.pk), "status": withdrawal.status},
    )


@receiver(withdrawal_failed, sender=Withdrawal, dispatch_uid="notifications.withdrawal_failed")
def on_withdrawal_failed(
    sender: type[Withdrawal], *, withdrawal: Withdrawal, **kwargs: Any
) -> None:
    """Dit au livreur que son retrait est refusé, pourquoi, et que ses gains
    lui sont rendus — c'est la phrase qui évite l'appel inquiet."""
    motif = withdrawal.failure_reason.strip().rstrip(".")
    notify(
        user=withdrawal.courier.user,
        kind=NotificationKind.PAYMENT,
        title="Retrait non versé",
        body=(
            f"Votre retrait de {withdrawal.amount} n'a pas été versé : {motif}. "
            "Le montant est rendu à vos gains."
        ),
        data={"withdrawal": str(withdrawal.pk), "status": withdrawal.status},
    )


# --------------------------------------------------------------- support
#
# Le client écrivait au support et n'apprenait jamais qu'on l'avait lu. Ces
# abonnés portent la réponse jusqu'à lui ; ceux de l'exploitation l'alertent
# d'une réclamation ou d'un retour sur une commande de son périmètre.

#: Permission qu'il faut détenir pour être prévenu d'une demande client —
#: celle que les vues du support opposent à la lecture.
SUPPORT_READ = "support.read"


def _extrait(texte: str, limite: int = 140) -> str:
    """Le début d'un message, pour le corps d'une notification."""
    propre = " ".join(texte.split())
    return propre if len(propre) <= limite else propre[: limite - 1].rstrip() + "…"


@receiver(ticket_answered, sender=SupportTicket, dispatch_uid="notifications.ticket_answered")
def on_ticket_answered(
    sender: type[SupportTicket], *, ticket: SupportTicket, message: SupportMessage, **kwargs: Any
) -> None:
    notify(
        user=ticket.user,
        kind=NotificationKind.SUPPORT,
        title=f"Réponse du service client — {ticket.subject}",
        body=_extrait(message.content),
        data={"ticket": str(ticket.pk)},
    )


@receiver(ticket_status_changed, sender=SupportTicket, dispatch_uid="notifications.ticket_status")
def on_ticket_status_changed(
    sender: type[SupportTicket], *, ticket: SupportTicket, **kwargs: Any
) -> None:
    """Seule la résolution se dit : « fermé » ou « rouvert » n'apprennent rien
    que la prochaine réponse ne dira mieux."""
    if ticket.status != TicketStatus.RESOLVED:
        return
    notify(
        user=ticket.user,
        kind=NotificationKind.SUPPORT,
        title=f"Demande résolue — {ticket.subject}",
        body=_extrait(ticket.resolution),
        data={"ticket": str(ticket.pk), "status": ticket.status},
    )


@receiver(complaint_filed, sender=Complaint, dispatch_uid="notifications.complaint_filed")
def on_complaint_filed(sender: type[Complaint], *, complaint: Complaint, **kwargs: Any) -> None:
    """Prévient le personnel de la cuisine concernée, habilité à la lire."""
    for membre in staff_to_alert(
        restaurant_id=complaint.order.restaurant_id, permission=SUPPORT_READ
    ):
        notify(
            user=membre,
            kind=NotificationKind.SUPPORT,
            title="Nouvelle réclamation",
            body=f"Commande {complaint.order.reference} — {complaint.subject}",
            data={"complaint": str(complaint.pk), "order": str(complaint.order_id)},
        )


@receiver(complaint_decided, sender=Complaint, dispatch_uid="notifications.complaint_decided")
def on_complaint_decided(sender: type[Complaint], *, complaint: Complaint, **kwargs: Any) -> None:
    titre = (
        "Réclamation résolue"
        if complaint.status == ComplaintStatus.RESOLVED
        else "Réclamation non retenue"
    )
    notify(
        user=complaint.user,
        kind=NotificationKind.SUPPORT,
        title=f"{titre} — commande {complaint.order.reference}",
        body=_extrait(complaint.resolution),
        data={"complaint": str(complaint.pk), "status": complaint.status},
    )


@receiver(return_requested, sender=ReturnRequest, dispatch_uid="notifications.return_requested")
def on_return_requested(
    sender: type[ReturnRequest], *, return_request: ReturnRequest, **kwargs: Any
) -> None:
    for membre in staff_to_alert(
        restaurant_id=return_request.order.restaurant_id, permission=SUPPORT_READ
    ):
        notify(
            user=membre,
            kind=NotificationKind.SUPPORT,
            title="Demande de retour",
            body=(
                f"Commande {return_request.order.reference} — "
                f"{return_request.refund_amount} demandés."
            ),
            data={"return": str(return_request.pk), "order": str(return_request.order_id)},
        )


#: Ce qu'on dit au client selon la décision. « Approuvée » n'annonce pas
#: l'argent : il part ensuite, par un remboursement que `payments` constate.
_RETOUR_ANNONCE: dict[str, str] = {
    ReturnStatus.APPROVED: "Retour accepté — le remboursement va suivre",
    ReturnStatus.REJECTED: "Retour refusé",
    ReturnStatus.REFUNDED: "Retour remboursé",
}


@receiver(return_decided, sender=ReturnRequest, dispatch_uid="notifications.return_decided")
def on_return_decided(
    sender: type[ReturnRequest], *, return_request: ReturnRequest, **kwargs: Any
) -> None:
    titre = _RETOUR_ANNONCE.get(return_request.status)
    if titre is None:
        return
    corps = return_request.resolution or f"Commande {return_request.order.reference}."
    notify(
        user=return_request.user,
        kind=NotificationKind.SUPPORT,
        title=f"{titre} — commande {return_request.order.reference}",
        body=_extrait(corps),
        data={"return": str(return_request.pk), "status": return_request.status},
    )


# ------------------------------------------------- pièces du livreur


@receiver(document_expiring, sender=CourierProfile, dispatch_uid="notifications.document_expiring")
def on_document_expiring(
    sender: type[CourierProfile],
    *,
    courier: CourierProfile,
    piece: str,
    expires_on: Any,
    days_left: int,
    **kwargs: Any,
) -> None:
    """Prévient le livreur et l'équipe de sa cuisine qu'une pièce expire.

    Le livreur, parce que c'est lui qui renouvelle ; l'équipe habilitée à lire
    les livreurs, parce que c'est elle qui décidera s'il roule encore.
    """
    libelle = CourierService.LIBELLES_PIECES.get(piece, piece)
    donnees = {"courier": str(courier.pk), "piece": piece, "expires_on": str(expires_on)}

    # Trois temps, parce que le rappel peut arriver **après** l'échéance : la
    # tâche rattrape les journées où elle n'a pas tourné, et « expire le
    # 17/09 » au passé se lirait comme une erreur du système.
    if days_left > 0:
        titre, quand = "Pièce bientôt expirée", f"expire le {expires_on:%d/%m/%Y}"
    elif days_left == 0:
        titre, quand = "Pièce expirée aujourd'hui", "expire aujourd'hui"
    else:
        titre, quand = "Pièce expirée", f"a expiré le {expires_on:%d/%m/%Y}"

    notify(
        user=courier.user,
        kind=NotificationKind.ACCOUNT,
        title=titre,
        body=f"Votre {libelle} {quand}. Déposez la nouvelle depuis votre profil.",
        data=donnees,
    )
    for membre in staff_to_alert(restaurant_id=courier.restaurant_id, permission="couriers.read"):
        notify(
            user=membre,
            kind=NotificationKind.ACCOUNT,
            title="Pièce livreur à renouveler",
            body=f"La {libelle} de {courier.user.full_name} {quand}.",
            data=donnees,
        )


#: Ce que le livreur lit pour chaque décision. `pending` n'y est pas : remettre
#: un dossier en instruction ne lui demande rien, et le prévenir le ferait
#: s'inquiéter d'un geste qui le concerne à peine.
_DECISIONS_DE_DOSSIER: dict[str, tuple[str, str]] = {
    VerificationStatus.APPROVED: (
        "Dossier validé",
        "Votre dossier est validé : vous pouvez passer en ligne et recevoir des courses.",
    ),
    VerificationStatus.REJECTED: (
        "Dossier refusé",
        "Votre dossier n'a pas été validé : {motif} Corrigez-le depuis votre profil.",
    ),
    VerificationStatus.SUSPENDED: (
        "Compte suspendu",
        "Vous ne recevez plus de courses : {motif} Contactez votre responsable.",
    ),
}


@receiver(
    verification_decided,
    sender=CourierProfile,
    dispatch_uid="notifications.verification_decided",
)
def on_verification_decided(
    sender: type[CourierProfile],
    *,
    courier: CourierProfile,
    previous_status: str,
    **kwargs: Any,
) -> None:
    """Prévient le livreur de la décision prise sur son dossier.

    Transactionnelle et non commerciale : un refus ou une suspension n'est pas
    une sollicitation, et le couper au motif du consentement laisserait un
    livreur attendre des courses qui ne viendront plus.
    """
    gabarit = _DECISIONS_DE_DOSSIER.get(courier.verification_status)
    if gabarit is None:
        return
    titre, corps = gabarit
    motif = courier.verification_notes.strip()
    if motif and not motif.endswith((".", "!", "?")):
        motif += "."
    notify(
        user=courier.user,
        kind=NotificationKind.ACCOUNT,
        title=titre,
        body=corps.format(motif=motif),
        data={
            "courier": str(courier.pk),
            "status": courier.verification_status,
            "previous_status": previous_status,
        },
    )
