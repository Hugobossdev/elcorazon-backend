"""Contrats du social — S3, S4.

`GroupCreateSerializer`, `JoinGroupSerializer` et `PostWriteSerializer` sont
les seuls points d'entrée en écriture. Aucun n'accepte `member_count`,
`likes_count`, `comments_count` ni `is_public` : ce sont des valeurs que le
serveur calcule (voir `apps.social.services`), jamais que le client déclare —
C1 transposé au social.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.orders.models import Order
from apps.social.models import GroupKind, Post, PostComment, PostKind, SocialGroup

__all__ = [
    "AuthorSerializer",
    "CommentWriteSerializer",
    "GroupCreateSerializer",
    "JoinGroupSerializer",
    "PostCommentSerializer",
    "PostSerializer",
    "PostWriteSerializer",
    "SocialGroupSerializer",
]


class AuthorSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ["id", "full_name", "avatar"]
        read_only_fields = fields


class SocialGroupSerializer(serializers.ModelSerializer[SocialGroup]):
    """Un groupe, tel que le voit l'un de ses membres — `invite_code` compris :
    ne figurer que dans les groupes où l'appelant est déjà membre (voir
    `apps.social.views.SocialGroupViewSet.get_queryset`) est ce qui empêche ce
    champ de devenir une clé qu'on distribue par erreur à qui ne l'a pas."""

    class Meta:
        model = SocialGroup
        fields = [
            "id",
            "name",
            "description",
            "kind",
            "invite_code",
            "is_private",
            "max_members",
            "member_count",
            "created_at",
        ]
        read_only_fields = fields


class GroupCreateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    kind = serializers.ChoiceField(choices=GroupKind.choices, default=GroupKind.CUSTOM)
    is_private = serializers.BooleanField(default=False)
    max_members = serializers.IntegerField(min_value=2, max_value=500, default=50)


class JoinGroupSerializer(serializers.Serializer[Any]):
    invite_code = serializers.CharField(max_length=12)


class PostSerializer(serializers.ModelSerializer[Post]):
    author = AuthorSerializer(read_only=True)
    liked_by_me = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "author",
            "group",
            "kind",
            "content",
            "order",
            "image_url",
            "is_public",
            "likes_count",
            "comments_count",
            "liked_by_me",
            "created_at",
        ]
        read_only_fields = fields

    def get_liked_by_me(self, obj: Post) -> bool:
        liked = self.context.get("liked_post_ids")
        return obj.pk in liked if liked is not None else False


class PostWriteSerializer(serializers.Serializer[Any]):
    """Entrée d'une publication.

    Ni `is_public` ni les compteurs : `is_public` découle de `group` (S4),
    calculé par le service — jamais déclaré ici.
    """

    content = serializers.CharField()
    kind = serializers.ChoiceField(choices=PostKind.choices, default=PostKind.TEXT)
    group = serializers.PrimaryKeyRelatedField(
        queryset=SocialGroup.objects.filter(is_active=True), required=False, allow_null=True
    )
    order = serializers.PrimaryKeyRelatedField(
        queryset=Order.objects.all(), required=False, allow_null=True
    )
    image_url = serializers.URLField(required=False, allow_blank=True, default="")


class PostCommentSerializer(serializers.ModelSerializer[PostComment]):
    author = AuthorSerializer(read_only=True)

    class Meta:
        model = PostComment
        fields = ["id", "post", "author", "content", "created_at"]
        read_only_fields = fields


class CommentWriteSerializer(serializers.Serializer[Any]):
    content = serializers.CharField()
