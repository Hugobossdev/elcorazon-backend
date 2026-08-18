"""Configuration du projet El Corazón.

L'application Celery est importée ici pour que le décorateur `@shared_task`
trouve toujours une application configurée, quel que soit le point d'entrée
(runserver, worker, beat, shell).
"""

from __future__ import annotations

from config.celery import app as celery_app

__all__ = ["celery_app"]
