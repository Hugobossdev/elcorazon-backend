"""Pagination.

Voir ADR-009. Deux stratégies, choisies selon la cardinalité :

* `StandardPagination` — pagination par numéro de page, pour les listes que
  l'utilisateur parcourt et dont il veut connaître le volume (commandes,
  catalogue, clients).
* `HighVolumeCursorPagination` — pagination par curseur, pour les flux à forte
  cardinalité (positions, notifications, événements) où `OFFSET` dégénère : à
  la page 5 000, PostgreSQL lit et jette 100 000 lignes à chaque appel.
"""

from __future__ import annotations

from rest_framework.pagination import CursorPagination, PageNumberPagination

__all__ = ["HighVolumeCursorPagination", "StandardPagination"]


class StandardPagination(PageNumberPagination):
    """Pagination par page, avec taille ajustable et bornée.

    La borne est le point important : sans `max_page_size`, `?page_size=100000`
    est une attaque par déni de service à un paramètre.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class HighVolumeCursorPagination(CursorPagination):
    """Pagination par curseur, ordonnée par clé primaire.

    L'ordre par `-id` est chronologique sans index supplémentaire : les clés
    primaires sont des UUIDv7, donc croissantes dans le temps (ADR-007).
    """

    page_size = 50
    max_page_size = 200
    page_size_query_param = "page_size"
    ordering = "-id"
