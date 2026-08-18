"""Événements de domaine émis par les paiements — ADR-002.

Même mécanisme que `orders.signals` : un encaissement peut régler autre chose
qu'une commande — un abonnement, demain peut-être un portefeuille rechargé —
et `payments` ne doit connaître aucun de ces domaines pour rester réutilisable
par le prochain. L'abonné se branche depuis son propre `AppConfig.ready()` ;
`payments` ne change pas d'une ligne quand `loyalty` s'y met.

Émis **dans** la transaction qui solde la `Transaction` : un abonné qui écrit
en base — l'activation d'un abonnement en est une — doit le faire de façon
atomique avec l'encaissement qui la déclenche.
"""

from __future__ import annotations

import django.dispatch

__all__ = ["payment_transaction_settled"]

#: Argument : `transaction` (l'instance `payments.models.Transaction` soldée).
payment_transaction_settled = django.dispatch.Signal()
