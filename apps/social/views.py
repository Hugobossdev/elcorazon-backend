"""Points d'entrée du social — S2, S3, S4.

**S2 — la visibilité est un filtre de requête, jamais une permission d'objet**
(ADR-005) : `PostViewSet.get_queryset` exclut d'emblée les publications d'un
groupe dont l'appelant n'est pas membre. Une publication qu'on ne peut pas voir
est absente, pas refusée — la distinguer par le code de statut dirait à un
curieux qu'un groupe privé existe à cette adresse.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.social.models import GroupMembership, Post, PostLike, SocialGroup
from apps.social.serializers import (
    CommentWriteSerializer,
    GroupCreateSerializer,
    JoinGroupSerializer,
    PostCommentSerializer,
    PostSerializer,
    PostWriteSerializer,
    SocialGroupSerializer,
)
from apps.social.services import SocialService
from common.permissions import IsCustomer, authenticated_user

__all__ = ["PostViewSet", "SocialGroupViewSet"]


class SocialGroupViewSet(
    ListModelMixin, CreateModelMixin, RetrieveModelMixin, GenericViewSet[SocialGroup]
):
    """`GET|POST /social/groups/` — uniquement les groupes de l'appelant.

    Un groupe où l'on n'est pas (encore) membre n'a rien à montrer ici : ni son
    `invite_code`, qui deviendrait public, ni son fil, couvert par `PostViewSet`.
    """

    permission_classes = [IsCustomer]
    queryset = SocialGroup.objects.none()  # pour le générateur de schéma

    def get_queryset(self) -> QuerySet[SocialGroup]:
        user = authenticated_user(self.request)
        return SocialGroup.objects.filter(
            memberships__user=user, memberships__is_active=True
        ).distinct()

    def get_serializer_class(self) -> type[Any]:
        return GroupCreateSerializer if self.action == "create" else SocialGroupSerializer

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = GroupCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = SocialService.create_group(
            creator=authenticated_user(request), **serializer.validated_data
        )
        return Response(SocialGroupSerializer(result.group).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def join(self, request: Request) -> Response:
        serializer = JoinGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        membership = SocialService.join(
            user=authenticated_user(request), **serializer.validated_data
        )
        return Response(SocialGroupSerializer(membership.group).data)

    @action(detail=True, methods=["post"])
    def leave(self, request: Request, pk: str | None = None) -> Response:
        group = self.get_object()
        SocialService.leave(user=authenticated_user(request), group=group)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PostViewSet(ListModelMixin, CreateModelMixin, RetrieveModelMixin, GenericViewSet[Post]):
    """`GET|POST /social/posts/` — fil public et fils de groupe, filtrés par appartenance."""

    permission_classes = [IsCustomer]
    queryset = Post.objects.none()  # pour le générateur de schéma
    filterset_fields = {"group": ["exact"], "kind": ["exact"]}

    def get_queryset(self) -> QuerySet[Post]:
        user = authenticated_user(self.request)
        mes_groupes = GroupMembership.objects.filter(user=user, is_active=True).values("group_id")
        return Post.objects.filter(Q(is_public=True) | Q(group_id__in=mes_groupes)).select_related(
            "author", "group"
        )

    def get_serializer_class(self) -> type[Any]:
        return PostWriteSerializer if self.action == "create" else PostSerializer

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        user = authenticated_user(self.request)
        context["liked_post_ids"] = set(
            PostLike.objects.filter(user=user).values_list("post_id", flat=True)
        )
        return context

    def create(self, request: Request, *args: object, **kwargs: object) -> Response:
        serializer = PostWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        post = SocialService.create_post(
            author=authenticated_user(request), **serializer.validated_data
        )
        return Response(
            PostSerializer(post, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def like(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        liked = SocialService.toggle_like(user=authenticated_user(request), post=post)
        post.refresh_from_db()
        return Response({"liked": liked, "likes_count": post.likes_count})

    @action(detail=True, methods=["get", "post"])
    def comments(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()

        if request.method == "POST":
            write_serializer = CommentWriteSerializer(data=request.data)
            write_serializer.is_valid(raise_exception=True)
            comment = SocialService.add_comment(
                user=authenticated_user(request), post=post, **write_serializer.validated_data
            )
            return Response(PostCommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        # `paginate_queryset` est typé pour le modèle de la vue (`Post`) : la
        # pagination d'un `QuerySet[PostComment]` passe par le paginateur
        # directement plutôt que par la méthode générique de `GenericAPIView`.
        # Le paginateur est toujours configuré (réglage global du projet) —
        # `assert` le dit au vérificateur de types sans en faire une garde.
        assert self.paginator is not None
        page = self.paginator.paginate_queryset(
            post.comments.select_related("author"), request, view=self
        )
        read_serializer = PostCommentSerializer(page, many=True)
        return self.get_paginated_response(read_serializer.data)
