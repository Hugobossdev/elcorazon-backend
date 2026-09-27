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

__all__ = [
    "payment_transaction_failed",
    "payment_transaction_settled",
    "refund_settled",
    "withdrawal_failed",
    "withdrawal_requested",
    "withdrawal_settled",
]

#: Argument : `transaction` (l'instance `payments.models.Transaction` soldée).
payment_transaction_settled = django.dispatch.Signal()

#: Argument : `transaction` (l'instance soldée en échec).
#:
#: Émis **en plus** de `payment_transaction_settled`, et non à sa place : les
#: deux issues n'intéressent pas les mêmes abonnés. Un encaissement réussi
#: active un abonnement et confirme une commande ; un échec ne fait avancer
#: aucun domaine — il se dit, au client qui doit reprendre son paiement et à
#: l'exploitation qui verra la commande rester en attente.
#:
#: Sans lui, un paiement refusé était **entièrement muet** : la transaction
#: passait en `failed`, la commande restait où elle était, et personne
#: n'apprenait rien. `NotificationKind.PAYMENT` existait sans jamais être émis.
payment_transaction_failed = django.dispatch.Signal()


#: Argument : `refund` (l'instance `payments.models.Refund` constatée versée).
#:
#: Un remboursement versé ne disait rien au client : l'annulation lui était
#: notifiée, le remboursement jamais, et il guettait son compte sans savoir si
#: quelque chose était parti.
refund_settled = django.dispatch.Signal()


# Retraits des livreurs — argument : `withdrawal` (l'instance
# `payments.models.Withdrawal`).
#
# Sans eux, un retrait était muet de bout en bout : l'exploitation n'apprenait
# pas qu'on lui demandait un versement, et le livreur n'apprenait pas qu'il
# avait été fait — ou refusé, gains rendus. `payments` ne connaît pas
# `notifications` (ADR-002) : c'est elle qui s'abonne.

#: Une demande vient d'être déposée — les gains sont déjà débités.
withdrawal_requested = django.dispatch.Signal()

#: L'exploitation a constaté le versement.
withdrawal_settled = django.dispatch.Signal()

#: Le versement est refusé, ou a échoué ; les gains sont rendus au livreur.
withdrawal_failed = django.dispatch.Signal()
