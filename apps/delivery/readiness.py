"""Ce que la livraison exige avant qu'un établissement ouvre.

Même sens que `apps.catalog.readiness` : l'abonné connaît l'émetteur.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.delivery.models import CourierProfile
from apps.delivery.states import VerificationStatus

if TYPE_CHECKING:  # pragma: no cover
    from apps.restaurants.models import Restaurant

__all__ = ["fleet_gaps"]


def fleet_gaps(restaurant: Restaurant) -> list[str]:
    """Un établissement sans livreur approuvé ne peut honorer aucune course.

    Le contrôle porte sur les dossiers **approuvés** et non sur les
    rattachements : une candidature en attente ne livre pas, et compter les
    candidatures ferait ouvrir un établissement dont la flotte entière est
    encore à l'instruction.
    """
    approuves = CourierProfile.objects.filter(
        restaurant=restaurant, verification_status=VerificationStatus.APPROVED
    )
    if not approuves.exists():
        return ["Aucun livreur approuvé n'est rattaché à cet établissement."]
    return []
