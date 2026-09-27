"""Événements de domaine émis par la livraison — ADR-002.

Même mécanisme que pour les commandes : `delivery` annonce qu'une course est
proposée, et ne sait pas qui l'écoute. C'est `notifications` aujourd'hui, ce
sera `analytics` demain, sans modifier ce module.
"""

from __future__ import annotations

import django.dispatch

__all__ = [
    "assignment_accepted",
    "assignment_cancelled",
    "assignment_declined",
    "assignment_offered",
    "courier_went_online",
    "document_expiring",
    "verification_decided",
]

#: Arguments : `assignment`.
assignment_offered = django.dispatch.Signal()

#: Arguments : `assignment`.
#:
#: Distinct de `assignment_offered`, et la distinction compte : une course
#: **proposée** peut être refusée, et prévenir le client à ce moment-là
#: reviendrait à lui annoncer un livreur qui ne viendra pas. C'est
#: l'acceptation qui engage quelqu'un.
#:
#: La commande, elle, ne bouge pas à cet instant — `accepted` n'est
#: volontairement pas projeté sur son statut (voir `ORDER_STATUS_PROJECTION`) :
#: le repas n'est pas parti, la commande reste `ready`. C'est bien pourquoi ce
#: signal existe séparément.
assignment_accepted = django.dispatch.Signal()


#: Arguments : `assignment`, `reason`.
#:
#: Émis quand le **personnel** retire une course à un livreur — un livreur
#: injoignable, un incident de flotte, une réaffectation.
#:
#: Ce signal manquait, et son absence se voyait sur la route : le livreur
#: n'était prévenu par rien. Il continuait vers le restaurant, y arrivait pour
#: une course annulée, et l'apprenait de la cuisine. La seule autre trace était
#: la diffusion sur le canal de la **commande** (`order_group`), que le livreur
#: n'écoute pas — il n'écoute que sa propre file.
#:
#: Distinct de `declined`, qui est le refus du livreur lui-même : personne n'a
#: besoin d'être prévenu de sa propre décision.
assignment_cancelled = django.dispatch.Signal()


#: Arguments : `assignment`.
#:
#: Le refus **du livreur lui-même**. Personne n'a à le prévenir de sa propre
#: décision ; c'est l'affectation automatique qui l'écoute, pour proposer la
#: course au suivant (`apps.delivery.dispatch`).
assignment_declined = django.dispatch.Signal()

#: Arguments : `courier`.
#:
#: Émis quand un livreur **passe** en ligne — pas quand il y reste. Les commandes
#: prêtes qui attendaient faute de livreur lui sont alors proposées.
courier_went_online = django.dispatch.Signal()

#: Arguments : `courier`, `piece` (nom du champ de la pièce), `expires_on`,
#: `days_left` (0 le jour même).
#:
#: Émis par le rappel quotidien (`remind_document_expiry`) aux échéances qui
#: laissent le temps d'agir — un mois, une semaine, la veille, le jour même.
document_expiring = django.dispatch.Signal()

#: Arguments : `courier` (après décision), `previous_status`.
#:
#: Émis quand le **personnel** change le statut d'un dossier — validé, refusé,
#: suspendu, rouvert. Ce signal manquait : le livreur n'apprenait un refus
#: qu'en rouvrant l'application, et une suspension décidée un samedi soir ne lui
#: parvenait qu'au moment où plus aucune course n'arrivait. Pas émis quand la
#: décision ne change rien (`is_noop`) : personne n'a à être prévenu d'un état
#: qui n'a pas bougé.
verification_decided = django.dispatch.Signal()
