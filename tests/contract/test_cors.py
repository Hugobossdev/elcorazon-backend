"""Contrat CORS — ce que le navigateur a le droit d'envoyer.

Le reste de la suite passe par le client de test Django, qui appelle les vues
en direct : aucune requête préalable n'y est jamais émise, donc **aucun de ces
tests ne peut voir un blocage CORS**. Les trois applications Flutter tournent
pourtant en web, sur une origine distincte de l'API.

Le défaut que ce module attrape a exactement cette forme : `Idempotency-Key`
(ADR-009) n'était pas dans `CORS_ALLOW_HEADERS`, la requête préalable le
refusait, et la création de commande échouait depuis le navigateur — sans rien
laisser dans les journaux du serveur, puisque la vraie requête n'était jamais
émise. Toute la suite restait verte.

La règle qui en découle : **un en-tête personnalisé ajouté à une requête client
se déclare ici en même temps que dans le client.**
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings

from apps.orders.views import IDEMPOTENCY_HEADER

pytestmark = pytest.mark.contract

# Origine quelconque : elle représente l'application Flutter Web, servie sur un
# autre port que l'API — c'est cette différence, et elle seule, qui déclenche
# la requête préalable.
ORIGINE = "http://localhost:8080"


def preflight(chemin: str, *, methode: str, entetes: str) -> str:
    """Émet une requête préalable et rend les en-têtes que le serveur autorise.

    `CORS_ALLOW_ALL_ORIGINS` est posé ici plutôt que dans les réglages de test :
    ce module est le seul à en avoir besoin, et le poser globalement rendrait
    permissifs des tests qui n'ont rien à voir avec CORS.
    """
    with override_settings(CORS_ALLOW_ALL_ORIGINS=True):
        response = Client().options(
            chemin,
            HTTP_ORIGIN=ORIGINE,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD=methode,
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=entetes,
        )
    return response.headers.get("access-control-allow-headers", "")


class TestEntetesAutorises:
    def test_la_creation_de_commande_peut_porter_sa_cle_d_idempotence(self) -> None:
        """Le cas réel : `POST /orders/` est la seule route qui pose un en-tête
        à nous, et c'était la seule que le navigateur bloquait."""
        autorises = preflight(
            "/api/v1/orders/", methode="POST", entetes=f"content-type,{IDEMPOTENCY_HEADER}"
        )

        # Comparaison en minuscules : la spécification veut l'en-tête
        # insensible à la casse, et `django-cors-headers` rend la liste telle
        # qu'elle est configurée — le client, lui, écrit `Idempotency-Key`.
        assert IDEMPOTENCY_HEADER.lower() in autorises.lower()

    def test_les_entetes_standards_restent_autorises(self) -> None:
        """Garde contre un remplacement de la liste par la nôtre seule :
        `CORS_ALLOW_HEADERS` *étend* les défauts de `django-cors-headers`, et
        sans `authorization` aucune requête authentifiée ne partirait."""
        autorises = preflight(
            "/api/v1/orders/", methode="POST", entetes="content-type,authorization"
        ).lower()

        assert "authorization" in autorises
        assert "content-type" in autorises
