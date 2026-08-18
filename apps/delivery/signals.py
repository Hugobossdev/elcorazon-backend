"""Événements de domaine émis par la livraison — ADR-002.

Même mécanisme que pour les commandes : `delivery` annonce qu'une course est
proposée, et ne sait pas qui l'écoute. C'est `notifications` aujourd'hui, ce
sera `analytics` demain, sans modifier ce module.
"""

from __future__ import annotations

import django.dispatch

__all__ = ["assignment_offered"]

#: Arguments : `assignment`.
assignment_offered = django.dispatch.Signal()
