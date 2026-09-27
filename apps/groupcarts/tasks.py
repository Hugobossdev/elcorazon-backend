"""Tâches planifiées du panier collaboratif."""

from __future__ import annotations

from celery import shared_task

from apps.groupcarts.services import GroupCartService

__all__ = ["expire_group_carts"]


@shared_task
def expire_group_carts() -> int:
    """Referme les paniers de groupe dont l'échéance est passée.

    L'échéance est déjà opposée à chaque contribution : un panier échu refuse les
    ajouts avant même que cette tâche soit passée. Elle sert donc à autre chose —
    à *dire* aux participants que c'est terminé, et à sortir le panier de leur
    liste des paniers en cours. Sans elle, chacun resterait devant un écran
    « ouvert » qui refuse tout ce qu'on lui demande, ce qui est la pire façon de
    fermer quelque chose.
    """
    return GroupCartService.expire_due()
