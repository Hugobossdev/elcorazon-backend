"""Rejeu sûr des créations — ADR-009.

Le réseau mobile coupe pendant l'envoi bien plus souvent qu'on ne le croit. Un
client qui perd la connexion après avoir posté sa commande ne sait pas si elle
est passée ; il retente. Sans clé d'idempotence, il en crée une seconde, et le
problème se découvre à la livraison de deux repas.

**La clé est réservée avant toute écriture métier.** C'est l'ordre qui compte,
et la première rédaction de ce module l'avait à l'envers : elle créait la
commande, puis mémorisait la réponse. Deux requêtes simultanées portant la même
clé passaient alors toutes deux la vérification initiale, créaient chacune une
commande, et se disputaient l'enregistrement. La perdante renvoyait bien la
réponse de la gagnante — le client n'y voyait rien — mais **sa propre commande
restait en base**, orpheline et non payée, visible du restaurant. Le doublon
que le mécanisme existe pour empêcher était simplement déplacé du cas
séquentiel vers le cas concurrent, c'est-à-dire vers un client impatient sur
réseau lent.

En réservant d'abord, c'est la contrainte d'unicité qui arbitre — avant que
quoi que ce soit de comptable ait été écrit.

La réponse d'origine est mémorisée, pas seulement le fait qu'un appel a eu
lieu : un rejeu doit renvoyer **exactement** la même chose, sans quoi le client
retenterait encore, faute de reconnaître ce qu'il reçoit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.renderers import JSONRenderer

from apps.accounts.models import User
from apps.orders.models import IdempotencyKey, Order
from common.exceptions import RequestInFlight

__all__ = ["StoredResponse", "complete", "release", "reserve"]

#: Statut de remplissage d'une réservation non terminée.
#:
#: Sans valeur de sentinelle, la colonne resterait vide et le modèle exigerait
#: une valeur. C'est `completed_at` qui fait autorité sur l'achèvement, jamais
#: ce nombre — un zéro n'est pas un code HTTP.
PENDING_STATUS = 0


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: Any


def reserve(*, user: User, endpoint: str, key: str) -> StoredResponse | None:
    """Prend la clé, ou rend la réponse déjà produite.

    Trois issues :

    * `None` — la clé est acquise, l'appelant peut écrire ;
    * une `StoredResponse` — un appel précédent a terminé, on rejoue sa réponse ;
    * `RequestInFlight` — une autre requête détient la clé et n'a pas fini. Le
      client doit réessayer dans un instant plutôt que de recevoir une réponse
      inventée. C'est un 409, pas une erreur serveur : rien n'est cassé, deux
      envois se sont simplement croisés.

    La réservation est committée **immédiatement**, dans sa propre transaction.
    Enveloppée dans celle de l'appelant, elle resterait invisible aux autres
    processus jusqu'au commit final — c'est-à-dire jusqu'après la création de
    la commande, ce qui ne protégerait plus de rien.
    """
    try:
        with transaction.atomic():
            IdempotencyKey.objects.create(
                user=user,
                endpoint=endpoint,
                key=key,
                response_status=PENDING_STATUS,
                response_body={},
            )
    except IntegrityError:
        return _replay_or_wait(user=user, endpoint=endpoint, key=key)

    return None


def _replay_or_wait(*, user: User, endpoint: str, key: str) -> StoredResponse:
    record = IdempotencyKey.objects.filter(user=user, endpoint=endpoint, key=key).first()
    if record is None:  # pragma: no cover - la clé vient d'être libérée
        raise RequestInFlight("Requête en cours de traitement ; réessayez dans un instant.")

    if record.completed_at is None:
        raise RequestInFlight(
            "Une requête portant cette clé est en cours ; réessayez dans un instant."
        )

    return StoredResponse(record.response_status, record.response_body)


def complete(
    *, user: User, endpoint: str, key: str, order: Order, status: int, body: Any
) -> StoredResponse:
    """Attache la réponse à la réservation, qui devient rejouable."""
    # Le corps est repassé par le rendu JSON de DRF avant d'être stocké : une
    # réponse fraîche contient des `UUID`, des `Decimal` et des `datetime` que
    # `JSONField` refuse. Le rejeu doit rendre *exactement* ce qu'a reçu le
    # premier appel, donc c'est bien la forme rendue qu'on mémorise.
    stored_body = json.loads(JSONRenderer().render(body))

    IdempotencyKey.objects.filter(user=user, endpoint=endpoint, key=key).update(
        order=order,
        response_status=status,
        response_body=stored_body,
        completed_at=timezone.now(),
    )
    return StoredResponse(status, stored_body)


def release(*, user: User, endpoint: str, key: str) -> None:
    """Rend la clé après un échec, pour que le client puisse réessayer.

    Sans cette libération, une commande refusée — panier vide, adresse hors
    zone — bloquerait sa clé jusqu'à la purge. Le client corrigerait son panier
    et se heurterait à « requête en cours » sur une requête qui n'existe plus.

    Seules les réservations **non terminées** sont libérées : un `UPDATE` déjà
    passé signifie qu'une réponse est enregistrée, et l'effacer ferait rejouer
    une création.
    """
    IdempotencyKey.objects.filter(
        user=user, endpoint=endpoint, key=key, completed_at__isnull=True
    ).delete()
