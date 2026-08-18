"""Cycle de vie d'un appel — ADR-010.

Machine acyclique et courte : un appel sonne, puis il aboutit ou non. Les
quatre issues sont terminales, ce qui rend le rejeu inexprimable — raccrocher
deux fois ne peut pas compter deux durées, ni rouvrir un canal RTC déjà fermé.
"""

from __future__ import annotations

from django.db import models

from common.state_machine import StateMachine

__all__ = ["CALL_MACHINE", "CallStatus"]


class CallStatus(models.TextChoices):
    RINGING = "ringing", "Sonne"
    ACCEPTED = "accepted", "En cours"
    DECLINED = "declined", "Refusé"
    ENDED = "ended", "Terminé"
    MISSED = "missed", "Manqué"


CALL_TRANSITIONS: dict[str, set[str]] = {
    CallStatus.RINGING: {CallStatus.ACCEPTED, CallStatus.DECLINED, CallStatus.MISSED},
    # Un appel accepté ne peut que se terminer : il n'est ni refusable
    # rétroactivement, ni « manqué » après coup.
    CallStatus.ACCEPTED: {CallStatus.ENDED},
    CallStatus.DECLINED: set(),
    CallStatus.ENDED: set(),
    CallStatus.MISSED: set(),
}

CALL_MACHINE = StateMachine(CALL_TRANSITIONS, name="appel")
