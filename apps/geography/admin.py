"""Back-office de la géographie — ADR-006.

C'est le seul endroit où la hiérarchie pays → ville → zone se saisit. Le
barème de livraison en fait partie : c'est ici qu'on décide qu'une zone
facture 500 F de base et 100 F du kilomètre, et l'exploitation doit pouvoir le
changer sans développement — c'était précisément ce qui manquait quand les
frais étaient une constante en dur.
"""

from __future__ import annotations

from django.contrib import admin

from apps.geography.models import City, Country, DeliveryZone
from common.admin import money_display

__all__ = ["CityAdmin", "CountryAdmin", "DeliveryZoneAdmin"]


class CityInline(admin.TabularInline):
    model = City
    extra = 0
    fields = ("name", "slug", "is_active")
    show_change_link = True


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "iso_code", "currency", "phone_prefix", "timezone", "is_active")
    list_filter = ("is_active", "currency")
    search_fields = ("name", "iso_code")
    inlines = (CityInline,)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Marché", {"fields": ("name", "iso_code", "is_active")}),
        (
            "Propriétés du marché",
            {
                "fields": ("currency", "phone_prefix", "timezone", "default_language"),
                "description": (
                    "La devise et le fuseau appartiennent au pays, pas au restaurant : "
                    "deux établissements d'un même pays ne peuvent pas facturer dans "
                    "deux devises différentes."
                ),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "slug", "is_active")
    list_filter = ("is_active", "country")
    search_fields = ("name", "slug")
    list_select_related = ("country",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "city",
        "base_fee_display",
        "fee_per_km_display",
        "max_distance_km",
        "estimated_delivery_minutes",
        "is_active",
    )
    list_filter = ("is_active", "city__country", "city")
    search_fields = ("name", "city__name")
    list_select_related = ("city",)
    readonly_fields = ("created_at", "updated_at")

    base_fee_display = money_display("base_fee", "Base")
    fee_per_km_display = money_display("fee_per_km", "Par km")

    fieldsets = (
        ("Zone", {"fields": ("city", "name", "is_active")}),
        (
            "Contour",
            {
                "fields": ("boundary",),
                "description": (
                    "MultiPolygon en WGS 84. Une zone réelle est souvent discontinue — "
                    "un fleuve, une voie ferrée, une enclave non desservie la coupent."
                ),
            },
        ),
        (
            "Barème",
            {
                "fields": (
                    "base_fee_minor",
                    "base_fee_currency",
                    "fee_per_km_minor",
                    "fee_per_km_currency",
                    "free_delivery_threshold_minor",
                    "free_delivery_threshold_currency",
                    "min_order_amount_minor",
                    "min_order_amount_currency",
                ),
                "description": (
                    "Montants en unité mineure : 500 XOF s'écrit 500, le franc CFA "
                    "n'ayant pas de décimale. Le seuil de franco est une remise faite "
                    "au client — il ne réduit pas la rémunération du livreur."
                ),
            },
        ),
        (
            "Service",
            {
                "fields": ("max_distance_km", "estimated_delivery_minutes"),
                "description": (
                    "Au-delà de la distance maximale, la course est refusée même si "
                    "l'adresse est dans le contour : un contour se dessine large, la "
                    "distance parcourue est ce qui coûte."
                ),
            },
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
