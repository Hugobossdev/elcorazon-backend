"""Limitation de débit sur l'authentification — T1.

L'implémentation précédente n'avait **aucun** limiteur sur `/auth/login` : la
force brute était ouverte.

Deux niveaux, et c'est le second qui compte réellement :

* **par adresse IP** — arrête l'attaquant naïf, celui qui martèle depuis une
  machine ;
* **par identifiant tenté** — arrête l'attaquant distribué. Sans lui, un botnet
  répartissant ses tentatives sur mille adresses passe sous le premier limiteur
  sans le déclencher, tout en essayant mille mots de passe sur le même compte.

Le second est indexé sur l'identifiant **soumis**, pas sur le compte trouvé :
compter uniquement les comptes existants révélerait lesquels existent, par
différence de comportement.
"""

from __future__ import annotations

import hashlib
from typing import Any

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

__all__ = ["AuthIPThrottle", "AuthIdentifierThrottle"]


class AuthIPThrottle(SimpleRateThrottle):
    scope = "auth_ip"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthIdentifierThrottle(SimpleRateThrottle):
    """Compte les tentatives visant un même identifiant, toutes origines confondues."""

    scope = "auth_identifier"
    identifier_field = "email"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        data: Any = getattr(request, "data", None)
        if not isinstance(data, dict):
            return None

        identifier = data.get(self.identifier_field)
        if not identifier or not isinstance(identifier, str):
            # Requête malformée : elle sera rejetée en validation. La compter
            # ici permettrait de saturer le compteur d'autrui en envoyant du
            # vide, ce qui transformerait la protection en déni de service.
            return None

        # Haché : les clés de cache finissent dans les journaux Redis et les
        # exports de diagnostic. Une adresse e-mail est une donnée personnelle,
        # et le comptage n'a pas besoin de sa valeur en clair.
        digest = hashlib.sha256(identifier.strip().lower().encode()).hexdigest()[:32]
        return self.cache_format % {"scope": self.scope, "ident": digest}
