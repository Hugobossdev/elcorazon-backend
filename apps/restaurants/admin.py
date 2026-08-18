"""Back-office des établissements — ADR-006, ADR-005.

`StaffMembership` est ici et pas ailleurs : rattacher quelqu'un à un
établissement est le geste qui lui donne un périmètre. Sans rattachement, un
membre du personnel muni de toutes les permissions ne voit **rien** — ce qui
est le bon défaut, mais qui se corrige depuis cet écran et nulle part ailleurs.
"""

from __future__ import annotations

from django.contrib import admin

from apps.restaurants.models import OpeningHours, Restaurant, StaffMembership

__all__ = ["RestaurantAdmin", "StaffMembershipAdmin"]


class OpeningHoursInline(admin.TabularInline):
    """Plages d'ouverture.

    Plusieurs par jour sont possibles — service du midi et du soir. Une plage
    qui franchit minuit se saisit telle quelle (`22:00 → 02:00`), pas en deux
    plages sur deux jours : la saisie reste conforme à ce qu'un restaurateur a
    en tête.
    """

    model = OpeningHours
    extra = 0
    fields = ("weekday", "opens_at", "closes_at")
    ordering = ("weekday", "opens_at")


class StaffMembershipInline(admin.TabularInline):
    model = StaffMembership
    extra = 0
    fields = ("user", "is_manager")
    autocomplete_fields = ("user",)


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "currency", "is_active", "accepts_orders")
    list_filter = ("is_active", "accepts_orders", "zone__city")
    search_fields = ("name", "slug", "address")
    prepopulated_fields = {"slug": ("name",)}
    list_select_related = ("zone__city__country",)
    inlines = (OpeningHoursInline, StaffMembershipInline)
    readonly_fields = ("currency", "created_at", "updated_at")

    fieldsets = (
        ("Établissement", {"fields": ("name", "slug", "description", "cover_image")}),
        ("Rattachement", {"fields": ("zone", "address", "location", "currency")}),
        ("Contact", {"fields": ("phone", "email")}),
        (
            "Exploitation",
            {
                "fields": ("is_active", "accepts_orders", "default_preparation_minutes"),
                "description": (
                    "Deux drapeaux distincts, et il faut qu'ils le restent : "
                    "« actif » dit si l'établissement existe, « accepte les commandes » "
                    "s'il peut en prendre là, maintenant. Les confondre obligerait à "
                    "faire disparaître un restaurant de l'application pour arrêter les "
                    "commandes une heure."
                ),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Ville")
    def city(self, obj: Restaurant) -> str:
        return obj.zone.city.name


@admin.register(StaffMembership)
class StaffMembershipAdmin(admin.ModelAdmin):
    """Périmètre du personnel — le troisième étage de l'ADR-005.

    La permission dit ce qu'on a le droit de faire, ce rattachement dit sur
    quoi. Un opérateur sans ligne ici ne voit aucune commande, aucun livreur,
    aucune transaction : la panne est visible et se corrige en une ligne, là où
    un accès trop large serait silencieux.
    """

    list_display = ("user", "restaurant", "is_manager", "created_at")
    list_filter = ("is_manager", "restaurant")
    search_fields = ("user__email", "user__full_name", "restaurant__name")
    list_select_related = ("user", "restaurant")
    autocomplete_fields = ("user", "restaurant")
    readonly_fields = ("created_at", "updated_at")
