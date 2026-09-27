"""Événements de domaine émis par le support — ADR-002.

Même mécanisme que `payments.signals` : `support` annonce, `notifications`
écoute. Sans eux, un client qui écrivait au support n'apprenait jamais qu'on
lui avait répondu — la réponse, s'il y en avait une, vivait dans
l'administration Django, et rien ne partait vers lui.

Chaque signal porte l'instance concernée ; ceux qui naissent d'un geste du
personnel sont émis **après** l'écriture, dans la même transaction.
"""

from __future__ import annotations

import django.dispatch

__all__ = [
    "complaint_decided",
    "complaint_filed",
    "return_decided",
    "return_requested",
    "ticket_answered",
    "ticket_status_changed",
]

#: Argument : `ticket`, `message` — le personnel a répondu.
ticket_answered = django.dispatch.Signal()

#: Argument : `ticket` — son statut a changé (résolu, fermé, rouvert).
ticket_status_changed = django.dispatch.Signal()

#: Argument : `complaint` — un client vient de réclamer sur une commande.
complaint_filed = django.dispatch.Signal()

#: Argument : `complaint` — l'exploitation a statué (résolue ou rejetée).
complaint_decided = django.dispatch.Signal()

#: Argument : `return_request` — un client demande un retour.
return_requested = django.dispatch.Signal()

#: Argument : `return_request` — l'exploitation a statué.
return_decided = django.dispatch.Signal()
