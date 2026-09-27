"""Erreurs métier et leur traduction en réponses HTTP.

Voir ADR-009. Le format est celui de la RFC 9457 (`application/problem+json`),
avec un champ `code` **stable** : le client s'appuie dessus, jamais sur
`detail`, qui est traduisible et peut changer sans préavis.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from common.quantities import DimensionMismatch
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

#: Contraintes d'unicité dont le heurt se dit au client, et comment le lui dire.
#:
#: Une contrainte absente de cette table n'est pas une omission : elle produit un
#: refus générique. C'est délibéré — voir `_integrity_problem`.
#:
#: La clé est le nom de la contrainte PostgreSQL, pas un fragment de message : il
#: est stable, il vient du catalogue système, et il ne dépend ni de la langue du
#: serveur ni de la version de psycopg.
UNIQUE_CONSTRAINT_PROBLEMS: dict[str, tuple[str, str, str]] = {
    # (code stable, message affichable, champ concerné)
    "accounts_user_phone_key": (
        "phone_already_exists",
        "Ce numéro de téléphone est déjà utilisé.",
        "phone",
    ),
    "accounts_user_email_key": (
        "email_already_exists",
        "Cette adresse e-mail est déjà utilisée.",
        "email",
    ),
}

#: `unique_violation` — le seul SQLSTATE que cette traduction prétende couvrir.
_UNIQUE_VIOLATION = "23505"


class BusinessRuleViolation(Exception):
    """Règle métier non respectée.

    Distincte d'une erreur de validation : les données sont bien formées, c'est
    l'état du système qui interdit l'opération.  D'où un 409 et non un 400.
    """

    code = "business_rule_violation"
    # Annoté `int` et non laissé au type inféré : les stubs de DRF donnent à
    # `HTTP_409_CONFLICT` le type `Literal[409]`, ce dont le vérificateur déduit
    # qu'aucune sous-classe ne peut annoncer un autre statut. Or c'est
    # précisément ce que fait `InvalidVerificationCode` (400) — la famille
    # partage la forme de l'erreur, pas son code.
    status_code: int = status.HTTP_409_CONFLICT
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


def _integrity_problem(exc: IntegrityError) -> Response | None:
    """Traduit un heurt de contrainte en refus lisible, ou rend `None`.

    ## Pourquoi ce filet existe alors que les serializers valident déjà

    `RegisterSerializer.validate_phone` interroge la base avant d'écrire. Entre
    cette lecture et l'`INSERT`, une seconde inscription portant le même numéro
    peut s'intercaler : les deux passent la validation, la seconde heurte
    `accounts_user_phone_key`. La fenêtre est étroite, mais c'est exactement
    celle qu'ouvre un client mobile qui réémet sa requête sur un réseau lent.

    Sans ce filet, ce heurt remonte en **500** : le client lit une panne
    serveur là où il y a un refus parfaitement légitime, et n'a aucun moyen de
    savoir qu'il lui suffit de changer de numéro. La contrainte, elle, reste :
    c'est elle qui garantit l'unicité, et aucune validation applicative ne peut
    s'y substituer.

    ## Ce qu'il ne traduit pas, et pourquoi

    Seules les violations d'unicité **nommées** dans
    [`UNIQUE_CONSTRAINT_PROBLEMS`] deviennent un refus qualifié. Une unicité
    inconnue devient un refus générique. Tout le reste — clé étrangère, `NOT
    NULL`, `CHECK` — rend `None` et redevient un 500 journalisé : ce sont des
    défauts de notre côté, et les habiller en 4xx les ferait disparaître des
    alertes tout en laissant croire au client qu'il a mal demandé.

    ## Rien du message d'origine ne sort

    `str(exc)` porte l'instruction SQL entière et le `DETAIL` de PostgreSQL,
    valeur heurtée comprise — `Key (phone)=(+228…) already exists`. Le renvoyer
    divulguerait le numéro d'un autre compte à qui tente une inscription, et le
    schéma des tables à tout le monde. Seuls le nom de la contrainte et le
    SQLSTATE sont lus ; le texte affiché vient de la table ci-dessus.
    """
    # `psycopg` expose le diagnostic structuré du serveur sur la cause de
    # l'exception Django. L'analyse du message est volontairement écartée : elle
    # dépendrait de la langue du serveur et du pilote.
    diagnostic = getattr(getattr(exc, "__cause__", None), "diag", None)
    if diagnostic is None:
        return None

    if getattr(diagnostic, "sqlstate", None) != _UNIQUE_VIOLATION:
        return None

    constraint = getattr(diagnostic, "constraint_name", None) or ""
    known = UNIQUE_CONSTRAINT_PROBLEMS.get(constraint)
    if known is None:
        return _problem(
            code="resource_already_exists",
            title="Ressource déjà existante",
            status_code=status.HTTP_409_CONFLICT,
            detail="Une ressource portant ces valeurs existe déjà.",
        )

    code, message, field = known
    return _problem(
        code=code,
        title="Ressource déjà existante",
        status_code=status.HTTP_409_CONFLICT,
        detail=message,
        # Le champ fautif est nommé sous la même forme qu'une erreur de
        # validation, pour que le client le surligne sans distinguer les deux
        # chemins — celui du serializer et celui de la course.
        errors={field: [message]},
    )


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

    if isinstance(exc, DimensionMismatch):
        # « 20 ml » d'un ingrédient pesé : une donnée mal formée, pas un état du
        # système — donc 400 et non 409. Sans cette branche, l'erreur, qui n'est
        # pas une `BusinessRuleViolation` (`common.quantities` ne connaît pas
        # DRF), remontait en **500** : le client lisait une panne là où il avait
        # simplement choisi la mauvaise unité.
        return _problem(
            code="dimension_mismatch",
            title="Unité incompatible",
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    if isinstance(exc, DjangoValidationError):
        # Deux formes, et la seconde n'était pas affichable. Une
        # `ValidationError` levée hors sérialiseur — le registre des
        # permissions, une méthode `clean()` — n'a pas de `message_dict` : ses
        # messages sortaient donc en **liste**, sous `errors`, sans `detail`.
        # Or les trois applications lisent `detail`, et retombent sur `errors`
        # seulement quand c'est un objet : la raison était dans la réponse, et
        # l'écran affichait « Une erreur est survenue ».
        par_champ = getattr(exc, "message_dict", None)
        return _problem(
            code="validation_error",
            title="Données invalides",
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=None if par_champ else " ".join(exc.messages),
            errors=par_champ if par_champ else {"non_field_errors": exc.messages},
        )

    if isinstance(exc, Http404):
        return _problem(
            code="not_found",
            title="Ressource introuvable",
            status_code=status.HTTP_404_NOT_FOUND,
            # Une phrase, parce que le client n'en a aucune autre : sans elle,
            # un 404 s'affichait « Une erreur est survenue ». Elle ne dit rien
            # de l'objet — ce qui est introuvable n'a pas à se décrire.
            detail="Cet élément est introuvable : il a peut-être été supprimé.",
        )

    if isinstance(exc, PermissionDenied):
        return _problem(
            code="permission_denied",
            title="Accès refusé",
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous n'avez pas l'autorisation d'effectuer cette action.",
        )

    if isinstance(exc, IntegrityError):
        # Après le métier et la validation : une règle qui sait se dire le fait
        # mieux que la contrainte qui la garde. Ce filet ne se déclenche que
        # lorsque personne n'a vu venir le heurt.
        problem = _integrity_problem(exc)
        if problem is not None:
            return problem
        return None  # défaut serveur : 500 journalisé, jamais détaillé

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # 500 : journalisé, jamais détaillé au client

    detail = response.data
    code = getattr(exc, "default_code", "error")
    errors = detail if isinstance(detail, dict) else None
    message = detail.get("detail") if isinstance(detail, dict) else None

    # DRF rend une **liste** quand la validation échoue hors d'un champ nommé —
    # `raise ValidationError("…")` dans une vue, ou un `ListSerializer`. Ni
    # `detail` ni `errors` n'étaient alors renseignés, et le client n'avait
    # plus rien à afficher que son message par défaut.
    if isinstance(detail, list) and detail:
        message = " ".join(str(ligne) for ligne in detail)
        errors = {"non_field_errors": [str(ligne) for ligne in detail]}

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
