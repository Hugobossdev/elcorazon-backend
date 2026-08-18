"""Back-office de l'analytics — consultation seule.

Un événement est un fait déjà survenu ; l'éditer ou le supprimer ferait mentir
le journal sur ce que le client a réellement fait.
"""

from __future__ import annotations

from django.contrib import admin

from apps.analytics.models import AnalyticsEvent
from common.admin import ReadOnlyAdmin

__all__ = ["AnalyticsEventAdmin"]


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(ReadOnlyAdmin):
    list_display = ("event_type", "user", "session_id", "created_at")
    list_filter = ("event_type",)
    search_fields = ("event_type", "user__email", "session_id")
    list_select_related = ("user",)
    date_hierarchy = "created_at"
