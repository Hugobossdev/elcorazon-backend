"""Back-office du profil client.

Les adresses se corrigent ici — c'est le geste de support le plus courant après
la validation d'un dossier livreur : un client appelle parce que son livreur ne
trouve pas, et le repère est vide ou faux.

La suppression est **réelle** et non logique : le RGPD impose un droit à
l'effacement, et aucune écriture financière ne pointe sur une adresse. La
commande en garde une copie figée — c'est elle qui doit rester lisible.
"""

from __future__ import annotations

from django.contrib import admin

from apps.profiles.models import Address, CustomerPreference

__all__ = ["AddressAdmin", "CustomerPreferenceAdmin"]


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("label", "user", "line1", "landmark", "city", "is_default")
    list_filter = ("kind", "is_default", "city")
    search_fields = ("user__email", "user__full_name", "line1", "landmark", "recipient_phone")
    list_select_related = ("user", "city")
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Propriétaire", {"fields": ("user", "label", "kind", "is_default")}),
        (
            "Adresse",
            {
                "fields": ("line1", "line2", "landmark", "city", "location"),
                "description": (
                    "À Lomé, l'adressage postal ne permet pas de trouver une porte : "
                    "c'est le point et le repère dont le livreur se sert réellement. "
                    "« En face de la pharmacie Bel Air » vaut mieux qu'un numéro de rue."
                ),
            },
        ),
        (
            "Destinataire",
            {"fields": ("recipient_name", "recipient_phone", "delivery_instructions")},
        ),
        ("Suivi", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(CustomerPreference)
class CustomerPreferenceAdmin(admin.ModelAdmin):
    """Préférences alimentaires et de notification.

    Le canal push transactionnel n'y figure pas et ne doit pas y figurer :
    « votre livreur arrive » n'est pas du marketing et ne se coupe pas.
    """

    list_display = (
        "user",
        "preferred_language",
        "marketing_push_enabled",
        "marketing_email_enabled",
    )
    list_filter = ("preferred_language", "marketing_push_enabled", "marketing_email_enabled")
    search_fields = ("user__email", "user__full_name")
    list_select_related = ("user",)
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
