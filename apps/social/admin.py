"""Back-office du social — modération.

Tout est en lecture seule ici : un groupe, une publication ou un commentaire
naissent d'un geste client (créer, poster, commenter), jamais du back-office.
Modérer un contenu litigieux se fait en le supprimant — la suppression reste
ouverte, contrairement au reste du back-office, parce qu'un post ou un
commentaire n'est l'origine d'aucune écriture comptable qu'il faudrait
préserver.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from apps.social.models import GroupMembership, Post, PostComment, PostLike, SocialGroup

__all__ = ["PostAdmin", "PostCommentAdmin", "PostLikeAdmin", "SocialGroupAdmin"]


class _NoAddAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    fields = ("user", "role", "joined_at", "is_active")
    readonly_fields = ("joined_at",)

    def has_add_permission(self, request: HttpRequest, obj: SocialGroup | None = None) -> bool:
        return False


@admin.register(SocialGroup)
class SocialGroupAdmin(_NoAddAdmin):
    list_display = ("name", "kind", "creator", "member_count", "max_members", "is_active")
    list_filter = ("kind", "is_private", "is_active")
    search_fields = ("name", "invite_code", "creator__email")
    list_select_related = ("creator",)
    readonly_fields = ("invite_code", "member_count")
    inlines = (GroupMembershipInline,)


@admin.register(Post)
class PostAdmin(_NoAddAdmin):
    list_display = (
        "author",
        "kind",
        "group",
        "is_public",
        "likes_count",
        "comments_count",
        "created_at",
    )
    list_filter = ("kind", "is_public")
    search_fields = ("author__email", "content")
    list_select_related = ("author", "group")
    date_hierarchy = "created_at"


@admin.register(PostComment)
class PostCommentAdmin(_NoAddAdmin):
    list_display = ("author", "post", "created_at")
    search_fields = ("author__email", "content")
    list_select_related = ("author", "post")


@admin.register(PostLike)
class PostLikeAdmin(_NoAddAdmin):
    list_display = ("user", "post", "created_at")
    search_fields = ("user__email",)
    list_select_related = ("user", "post")
