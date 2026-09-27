"""Événements émis par l'identité et les droits — ADR-002.

`accounts` est le socle : tout le monde en dépend, il ne dépend de personne. Il
ne peut donc pas savoir **sur quoi** porte un compte du personnel — c'est
`restaurants` qui connaît les périmètres. Quand une décision sur les droits
demande de le savoir, `accounts` émet, et `restaurants` répond.
"""

from __future__ import annotations

import django.dispatch

__all__ = ["role_changing"]

#: Arguments : `role` (l'instance, **avant** enregistrement), `actor`.
#:
#: Émis avant d'enregistrer la modification d'un rôle existant. Un abonné qui
#: refuse lève une `PermissionDenied` : l'écriture n'a pas lieu. C'est le seul
#: signal du projet qui peut interdire, et c'est voulu — modifier un rôle,
#: c'est modifier d'un geste tous ceux qui le portent, et `accounts` ne sait
#: pas où ils travaillent.
role_changing = django.dispatch.Signal()
