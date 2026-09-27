"""Back-office de l'identité — ADR-004, ADR-005.

Le mot de passe ne s'édite pas ici, et c'est délibéré. Le changer par
`AuthService` révoque toutes les sessions ouvertes (T2) ; le changer par un
formulaire d'admin ne révoquerait rien, et laisserait vivantes les sessions
que le changement était censé fermer — c'est-à-dire précisément celles d'un
compte qu'on soupçonne compromis.

Le support dispose à la place d'une action de révocation, qui est le geste
réellement utile quand un client signale un accès suspect.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.accounts.models import Device, Role, User
from apps.accounts.services import AuthService

__all__ = ["DeviceAdmin", "RoleAdmin", "UserAdmin"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "permission_count", "is_system", "created_at")
    list_filter = ("is_system",)
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Permissions")
    def permission_count(self, obj: Role) -> int:
        return len(obj.permissions)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Un rôle système ne se supprime pas.

        `Super Admin` retiré par mégarde, et plus personne ne peut rendre ses
        droits à qui que ce soit — y compris à soi-même.
        """
        return not (obj and obj.is_system)


class DeviceInline(admin.TabularInline):
    model = Device
    extra = 0
    fields = ("platform", "token", "last_used_at")
    readonly_fields = fields
    can_delete = True  # révoquer un appareil perdu est un geste de support

    def has_add_permission(self, request: HttpRequest, obj: User | None = None) -> bool:
        """Un appareil s'enregistre depuis l'application, avec son jeton FCM.
        En saisir un à la main produirait une ligne qui ne recevra rien."""
        return False


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "user_type", "is_active", "last_seen_at", "created_at")
    list_filter = ("user_type", "is_active", "is_superuser")
    search_fields = ("email", "full_name", "phone")
    filter_horizontal = ("roles",)
    inlines = (DeviceInline,)
    actions = ("revoke_sessions", "deactivate")

    # `password` est affiché — son empreinte, jamais sa valeur — mais non
    # modifiable : voir l'en-tête du module.
    readonly_fields = (
        "password",
        "last_login",
        "email_verified_at",
        "phone_verified_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        ("Identité", {"fields": ("email", "phone", "full_name", "avatar")}),
        ("Accès", {"fields": ("user_type", "is_active", "is_superuser", "roles", "password")}),
        (
            "Suivi",
            {
                "fields": (
                    "last_login",
                    "last_seen_at",
                    "email_verified_at",
                    "phone_verified_at",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.action(description="Révoquer toutes les sessions")
    def revoke_sessions(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """T2 — ferme les jetons de rafraîchissement en circulation.

        Le geste que réclame un client qui signale un accès suspect : il ne
        veut pas changer de mot de passe, il veut que l'autre soit déconnecté.
        """
        revoked = sum(AuthService.revoke_all_sessions(user) for user in queryset)
        self.message_user(request, f"{revoked} session(s) révoquée(s).")

    @admin.action(description="Désactiver le compte")
    def deactivate(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """Désactiver **et** révoquer, dans le même geste.

        Désactiver seul laisserait les jetons d'accès valides jusqu'à leur
        expiration : quinze minutes pendant lesquelles un compte fermé
        continue de commander.
        """
        for user in queryset:
            AuthService.revoke_all_sessions(user)
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} compte(s) désactivé(s) et déconnecté(s).")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "last_used_at", "created_at")
    list_filter = ("platform",)
    search_fields = ("user__email", "token")
    list_select_related = ("user",)
    readonly_fields = ("token", "created_at", "updated_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False
