"""Signalisation d'appel — le seul chemin d'écriture du statut d'un appel.

Deux règles y sont défendues, et aucune n'est vérifiable côté client :

* **Qui peut appeler qui.** Un client ne joint que le livreur de sa commande, un
  livreur que le client de sa course, et seulement tant que la livraison est en
  cours. L'implémentation précédente laissait le client fournir `receiver_id` :
  n'importe quel compte pouvait faire sonner n'importe quel autre.
* **Quand le canal RTC s'ouvre.** Le jeton n'est délivré qu'aux deux parties de
  l'appel, borné à son canal et à sa durée.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.calls.models import Call, CallKind
from apps.calls.states import CALL_MACHINE, CallStatus
from apps.delivery.models import Assignment
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from common.agora import RtcRole, build_rtc_token
from common.exceptions import BusinessRuleViolation
from common.realtime import publish, user_group

__all__ = ["CallService", "RtcCredentials"]

#: Statuts de course pendant lesquels un appel a un sens. Avant, personne n'est
#: en route ; après, la livraison est finie et la conversation n'a plus d'objet.
CALLABLE_STATUSES = frozenset(
    {
        DeliveryStatus.ACCEPTED,
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.ON_THE_WAY,
    }
)


@dataclass(frozen=True)
class RtcCredentials:
    """De quoi rejoindre le canal — et rien de plus."""

    channel_name: str
    token: str
    uid: int
    app_id: str
    expires_in: int


class CallService:
    @staticmethod
    @transaction.atomic
    def place(*, order: Order, caller: User, kind: str = CallKind.VOICE) -> Call:
        """Ouvre un appel sur une commande, entre son client et son livreur."""
        assignment = (
            Assignment.objects.select_related("courier__user")
            .filter(order=order, status__in=CALLABLE_STATUSES)
            .first()
        )
        if assignment is None:
            raise BusinessRuleViolation(
                "Aucune livraison en cours sur cette commande : il n'y a personne à appeler.",
                order_status=order.status,
            )

        courier_user = assignment.courier.user
        if caller.pk == order.customer_id:
            callee = courier_user
        elif caller.pk == courier_user.pk:
            callee = order.customer
        else:
            raise BusinessRuleViolation("Cette commande ne vous concerne pas.")

        if Call.objects.filter(
            order=order, status__in=[CallStatus.RINGING, CallStatus.ACCEPTED]
        ).exists():
            raise BusinessRuleViolation("Un appel est déjà en cours sur cette commande.")

        call = Call.objects.create(order=order, caller=caller, callee=callee, kind=kind)
        CallService._announce(call, "call.incoming", recipient=callee)
        return call

    @staticmethod
    @transaction.atomic
    def accept(*, call: Call, actor: User) -> Call:
        """Le destinataire décroche — lui seul le peut."""
        if actor.pk != call.callee_id:
            raise BusinessRuleViolation("Seul le destinataire peut répondre à cet appel.")

        CallService._transition(call, CallStatus.ACCEPTED)
        call.answered_at = timezone.now()
        call.save(update_fields=["status", "answered_at", "updated_at"])

        CallService._announce(call, "call.accepted", recipient=call.caller)
        return call

    @staticmethod
    @transaction.atomic
    def decline(*, call: Call, actor: User) -> Call:
        if actor.pk != call.callee_id:
            raise BusinessRuleViolation("Seul le destinataire peut refuser cet appel.")

        CallService._transition(call, CallStatus.DECLINED)
        call.ended_at = timezone.now()
        call.save(update_fields=["status", "ended_at", "updated_at"])

        CallService._announce(call, "call.declined", recipient=call.caller)
        return call

    @staticmethod
    @transaction.atomic
    def end(*, call: Call, actor: User) -> Call:
        """Raccroche — l'une ou l'autre des deux parties.

        Un appel qui sonnait encore devient « manqué » et non « terminé » : la
        distinction est ce que lit l'historique, et raccrocher avant décrochage
        n'est pas la même chose qu'une conversation qui s'achève.
        """
        if actor.pk not in {call.caller_id, call.callee_id}:
            raise BusinessRuleViolation("Cet appel ne vous concerne pas.")

        target = CallStatus.ENDED if call.status == CallStatus.ACCEPTED else CallStatus.MISSED
        CallService._transition(call, target)

        call.ended_at = timezone.now()
        if call.answered_at is not None:
            call.duration_seconds = max(0, int((call.ended_at - call.answered_at).total_seconds()))
        call.save(update_fields=["status", "ended_at", "duration_seconds", "updated_at"])

        other = call.callee if actor.pk == call.caller_id else call.caller
        CallService._announce(call, "call.ended", recipient=other)
        return call

    # ------------------------------------------------------------ jeton RTC

    @staticmethod
    def credentials_for(*, call: Call, user: User) -> RtcCredentials:
        """Jeton RTC de [user] pour cet appel.

        Refusé à un tiers, et refusé une fois l'appel terminé : un jeton encore
        valable après un raccrochage laisserait rouvrir le canal.
        """
        if user.pk not in {call.caller_id, call.callee_id}:
            raise BusinessRuleViolation("Cet appel ne vous concerne pas.")
        if not call.is_active:
            raise BusinessRuleViolation("Cet appel est terminé.")

        ttl = int(settings.AGORA_TOKEN_TTL_SECONDS)
        uid = CallService._uid_for(call, user)

        return RtcCredentials(
            channel_name=call.channel_name,
            token=build_rtc_token(
                app_id=settings.AGORA_APP_ID,
                app_certificate=settings.AGORA_APP_CERTIFICATE,
                channel_name=call.channel_name,
                uid=uid,
                role=RtcRole.PUBLISHER,
                ttl_seconds=ttl,
            ),
            uid=uid,
            app_id=settings.AGORA_APP_ID,
            expires_in=ttl,
        )

    @staticmethod
    def _uid_for(call: Call, user: User) -> int:
        """`1` pour l'appelant, `2` pour le destinataire.

        Agora veut un entier 32 bits ; nos identifiants sont des UUID. Un hachage
        tronqué — ce que faisait l'app — peut entrer en collision, et deux
        participants au même `uid` s'expulsent mutuellement du canal. Deux
        constantes suffisent : le canal n'a jamais que deux occupants, et il est
        propre à cet appel.
        """
        return 1 if user.pk == call.caller_id else 2

    # ------------------------------------------------------------ diffusion

    @staticmethod
    def _transition(call: Call, target: str) -> None:
        CALL_MACHINE.validate(call.status, target)
        call.status = target

    @staticmethod
    def _announce(call: Call, event: str, *, recipient: User) -> None:
        """Prévient l'autre partie sur **sa** file personnelle.

        Un canal par commande ne suffirait pas : le destinataire d'un appel n'a
        aucune raison d'avoir ouvert l'écran de cette commande au moment où son
        téléphone doit sonner.
        """
        body = {
            "call": str(call.pk),
            "order": str(call.order_id),
            "status": call.status,
            "kind": call.kind,
            "caller": str(call.caller_id),
            "caller_name": call.caller.full_name,
            "callee": str(call.callee_id),
        }
        transaction.on_commit(lambda: publish(user_group(recipient.pk), event, body))
