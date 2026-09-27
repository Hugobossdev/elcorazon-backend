"""Back-office du suivi.

En lecture seule et volontairement pauvre. Les relevés de position servent au
litige — « le livreur dit être passé » — et à rien d'autre : ils sont purgés
au bout d'un mois, et la table est de loin la plus volumineuse du produit.

Aucune recherche par livreur n'est offerte, et c'est délibéré : suivre
quelqu'un course par course est un service rendu au client pendant sa
livraison, pas un outil de surveillance du personnel.
"""

from __future__ import annotations

from django.contrib import admin

from apps.tracking.models import LocationPing
from common.admin import ReadOnlyAdmin

__all__ = ["LocationPingAdmin"]


@admin.register(LocationPing)
class LocationPingAdmin(ReadOnlyAdmin):
    list_display = ("assignment", "recorded_at", "received_at", "speed_mps", "accuracy_m")
    list_filter = ("recorded_at",)
    search_fields = ("assignment__order__reference",)
    list_select_related = ("assignment__order",)
    date_hierarchy = "recorded_at"
