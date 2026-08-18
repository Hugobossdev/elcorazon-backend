"""Back-office des notifications.

En lecture seule : une notification est produite par le serveur en réaction à
un événement de domaine. En saisir une à la main enverrait un message que rien
ne justifie, et qui ne correspondrait à aucun état du système.

L'écran sert au diagnostic — « le client dit ne pas avoir été prévenu » : on y
voit si la notification a été produite, et si elle a été lue.
"""

from __future__ import annotations

from django.contrib import admin

from apps.notifications.models import Notification
from common.admin import ReadOnlyAdmin

__all__ = ["NotificationAdmin"]


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ("user", "kind", "title", "lue", "created_at")
    list_filter = ("kind", "created_at")
    search_fields = ("user__email", "title", "body")
    list_select_related = ("user",)
    date_hierarchy = "created_at"

    @admin.display(description="Lue", boolean=True)
    def lue(self, obj: Notification) -> bool:
        return obj.is_read
