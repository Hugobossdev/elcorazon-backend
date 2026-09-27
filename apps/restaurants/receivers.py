"""Abonnements de `restaurants` aux événements d'autres modules — ADR-002.

`restaurants` connaît les périmètres du personnel ; `accounts`, le socle, ne
les connaît pas. Quand une décision d'`accounts` dépend d'un périmètre, elle
émet, et c'est ici qu'on répond.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import receiver

from apps.accounts.models import Role, User
from apps.accounts.signals import role_changing
from apps.restaurants.scoping import assert_can_manage, is_unscoped


@receiver(role_changing, sender=Role, dispatch_uid="restaurants.role_changing")
def on_role_changing(sender: type[Role], *, role: Role, actor: User, **kwargs: Any) -> None:
    """Refuse de modifier un rôle que porte un compte hors du ressort de l'acteur.

    Élargir un rôle commun, c'est élargir d'un geste tous ceux qui le portent —
    y compris le personnel d'un établissement que l'acteur ne gère pas. La
    règle est celle de la fiche d'un compte (`assert_can_manage`), appliquée à
    chacun des porteurs : un rôle se modifie si chacun d'eux pourrait l'être.
    """
    if is_unscoped(actor):
        return
    for porteur in User.objects.filter(roles=role).prefetch_related("roles"):
        assert_can_manage(actor, porteur)
