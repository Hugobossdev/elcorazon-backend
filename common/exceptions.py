"""Erreurs métier et leur traduction en réponses HTTP.

Voir ADR-009. Le format est celui de la RFC 9457 (`application/problem+json`),
avec un champ `code` **stable** : le client s'appuie dessus, jamais sur
`detail`, qui est traduisible et peut changer sans préavis.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from common.state_machine import IllegalTransition

__all__ = [
    "BusinessRuleViolation",
    "ConcurrentModification",
    "InsufficientBalance",
    "InsufficientStock",
    "RequestInFlight",
    "problem_detail_handler",
]

ERROR_BASE_URI = "https://api.elcorazon.app/errors"

#: Membres du corps de réponse que la RFC 9457 réserve — plus `headers`, qui est
#: un paramètre de construction de la réponse.
#:
#: Un appelant qui les emploie comme donnée contextuelle est refusé à la
#: construction. C'est délibérément brutal : `code` est un nom naturel pour
#: « le code d'invitation » ou « le code promotionnel », et la collision se
#: manifesterait sinon par un doublon de mot-clé à la sérialisation — donc par un
#: 500 sur un refus par ailleurs parfaitement légitime, et seulement le jour où ce
#: refus se produit.
RESERVED_MEMBERS = frozenset({"code", "detail", "errors", "headers", "status", "title", "type"})


class BusinessRuleViolation(Exception):
    """Règle métier non respectée.

    Distincte d'une erreur de validation : les données sont bien formées, c'est
    l'état du système qui interdit l'opération.  D'où un 409 et non un 400.
    """

    code = "business_rule_violation"
    status_code = status.HTTP_409_CONFLICT
    title = "Opération impossible dans l'état actuel"

    def __init__(self, detail: str, **extra: Any) -> None:
        collisions = RESERVED_MEMBERS & set(extra)
        if collisions:
            raise ValueError(
                f"{', '.join(sorted(collisions))} : membre(s) réservé(s) de la RFC 9457. "
                "Nommez la donnée autrement — `invitation_code`, `promo_code`, "
                "`current_status` — pour qu'elle voyage sans écraser le contrat."
            )

        self.detail = detail
        self.extra = extra
        super().__init__(detail)


class InsufficientStock(BusinessRuleViolation):
    code = "insufficient_stock"
    title = "Stock insuffisant"


class InsufficientBalance(BusinessRuleViolation):
    """Solde insuffisant — points de fidélité ou portefeuille.

    Levée par le débit conditionnel atomique (F1) lorsqu'il n'affecte aucune
    ligne : c'est le signal qu'une opération concurrente a consommé le solde
    entre-temps.
    """

    code = "insufficient_balance"
    title = "Solde insuffisant"


class ConcurrentModification(BusinessRuleViolation):
    """La ressource a changé entre la lecture et l'écriture."""

    code = "concurrent_modification"
    title = "Ressource modifiée entre-temps"


class RequestInFlight(BusinessRuleViolation):
    """Une requête portant la même clé d'idempotence est en cours.

    Distincte d'un rejeu : là, la première requête n'a pas encore terminé, donc
    il n'y a aucune réponse à rendre. Inventer un succès risquerait d'annoncer
    une commande qui échouera ; répondre 409 dit au client d'attendre un
    instant, ce qu'un mobile sait faire.
    """

    code = "request_in_progress"
    title = "Requête déjà en cours"


def _problem(
    *,
    code: str,
    title: str,
    status_code: int,
    detail: str | None = None,
    errors: Any = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> Response:
    body: dict[str, Any] = {
        "type": f"{ERROR_BASE_URI}/{code.replace('_', '-')}",
        "title": title,
        "status": status_code,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    body.update(extra)

    response = Response(body, status=status_code, content_type="application/problem+json")
    # Les en-têtes que DRF avait posés sur sa propre réponse doivent survivre à
    # la reconstruction. `Retry-After` en particulier : sans lui, un client
    # limité ne sait pas combien de temps attendre, et la seule stratégie qui
    # lui reste est de réessayer tout de suite — c'est-à-dire d'aggraver
    # exactement ce que la limitation cherche à contenir.
    for name, value in (headers or {}).items():
        response[name] = value

    return response


def problem_detail_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Gestionnaire d'exceptions DRF (`EXCEPTION_HANDLER`).

    Traite d'abord les exceptions métier, qui ne descendent pas de
    `APIException` — sans quoi DRF les laisserait remonter en 500 et le client
    verrait une panne là où il y a un refus légitime.
    """
    if isinstance(exc, IllegalTransition):
        return _problem(
            code="illegal_transition",
            title="Transition de statut refusée",
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            current_status=exc.source,
            requested_status=exc.target,
            allowed_transitions=exc.allowed,
        )

    if isinstance(exc, BusinessRuleViolation):
        return _problem(
            code=exc.code,
            title=exc.title,
            status_code=exc.status_code,
            detail=exc.detail,
            **exc.extra,
        )

    if isinstance(exc, DjangoValidationError):
        return _problem(
            code="validation_error",
            title="Données invalides",
            status_code=status.HTTP_400_BAD_REQUEST,
            errors=exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
        )

    if isinstance(exc, Http404):
        return _problem(
            code="not_found",
            title="Ressource introuvable",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if isinstance(exc, PermissionDenied):
        return _problem(
            code="permission_denied",
            title="Accès refusé",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # 500 : journalisé, jamais détaillé au client

    detail = response.data
    code = getattr(exc, "default_code", "error")
    errors = detail if isinstance(detail, dict) else None
    message = detail.get("detail") if isinstance(detail, dict) else None

    return _problem(
        code=str(code),
        title=getattr(exc, "default_detail", "Erreur"),
        status_code=response.status_code,
        detail=str(message) if message else None,
        errors=errors if errors and "detail" not in errors else None,
        # DRF pose `Retry-After` sur un 429 et `WWW-Authenticate` sur un 401 ;
        # reconstruire la réponse sans les reprendre les ferait disparaître.
        headers=dict(response.headers),
    )
