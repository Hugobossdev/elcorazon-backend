"""Appels client ↔ livreur.

Le média reste en pair-à-pair chez Agora ; ce module ne porte que la
**signalisation** — qui appelle qui, à propos de quelle commande, et où en est
l'appel. C'est le minimum pour qu'un téléphone sonne et qu'un canal RTC puisse
être rejoint par les deux parties, et rien de plus : ni enregistrement, ni
contenu.

Le nom du canal RTC est **dérivé de l'appel** et non choisi par le client.
L'implémentation précédente le composait côté mobile (`order_{id}_call`), donc
n'importe qui connaissant un identifiant de commande pouvait rejoindre la
conversation en cours.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from apps.calls.states import CALL_MACHINE, CallStatus
from apps.orders.models import Order
from common.models import TimeStampedModel, UUIDModel, state_check_constraint

__all__ = ["Call", "CallKind"]


class CallKind(models.TextChoices):
    VOICE = "voice", "Audio"
    VIDEO = "video", "Vidéo"


class Call(UUIDModel, TimeStampedModel):
    """Un appel passé à propos d'une commande.

    `caller` et `callee` sont figés à la création : l'appelant est déduit du
    jeton, le destinataire de la commande. Aucun des deux n'est accepté en
    entrée — les accepter permettrait de faire sonner le téléphone de n'importe
    qui, au nom de n'importe qui.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="calls")
    caller = models.ForeignKey(User, on_delete=models.PROTECT, related_name="calls_made")
    callee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="calls_received")

    kind = models.CharField(max_length=8, choices=CallKind.choices, default=CallKind.VOICE)
    status = models.CharField(
        max_length=16, choices=CallStatus.choices, default=CallStatus.RINGING, db_index=True
    )

    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    #: Durée en secondes, figée à la fin de l'appel. Stockée plutôt que
    #: recalculée : `answered_at`/`ended_at` suffisent aujourd'hui, mais une
    #: durée facturable ne doit pas dépendre d'une soustraction refaite à
    #: chaque lecture.
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "appel"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(CALL_MACHINE, "status", "call_status_in_enum"),
            # Un seul appel en cours par commande. Deux sonneries simultanées
            # sur la même course produiraient deux canaux RTC, dont un resterait
            # ouvert sans personne dedans.
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status__in=[CallStatus.RINGING, CallStatus.ACCEPTED]),
                name="one_active_call_per_order",
            ),
        ]
        indexes = [models.Index(fields=["callee", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.caller.full_name} → {self.callee.full_name} ({self.get_status_display()})"

    @property
    def channel_name(self) -> str:
        """Canal RTC de cet appel — dérivé, jamais fourni par un client.

        L'identifiant de l'appel est un UUIDv7 : imprévisible pour qui ne l'a
        pas reçu, et différent à chaque nouvel appel sur la même commande. Un
        canal réutilisé laisserait le rappel suivant tomber dans la même pièce
        que le précédent.
        """
        return f"call-{self.pk}"

    @property
    def is_active(self) -> bool:
        return self.status in {CallStatus.RINGING, CallStatus.ACCEPTED}
