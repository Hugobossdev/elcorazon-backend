"""Social : groupes et publications — invariants S2, S3, S4.

**S2** — la visibilité d'une publication de groupe se vérifie à **chaque
accès**, pas seulement à la création : c'est un filtre de requête (`get_queryset`
de `apps.social.views`), jamais une permission d'objet qui chargerait d'abord la
ligne interdite. L'implémentation précédente laissait un post privé lisible et
commentable par quiconque en connaissait l'UUID.

**S3** — partager une commande exige d'en être le propriétaire : elle expose
l'adresse de livraison. Vérifié à la création (`apps.social.services`), pas ici
— ce n'est pas quelque chose qu'une contrainte de base sait exprimer, `order` et
`author` n'ayant aucune relation déclarée entre eux.

**S4** — une publication rattachée à un groupe ne peut pas être publique. C'est
la contrainte `group_post_not_public` : sur l'implémentation précédente, un post
de groupe remontait dans le fil global faute de ce garde-fou.
"""

from __future__ import annotations

import secrets
from typing import Any

from django.db import models

from apps.accounts.models import User
from apps.orders.models import Order
from common.models import TimeStampedModel, UUIDModel

__all__ = [
    "GroupKind",
    "GroupMembership",
    "GroupRole",
    "Post",
    "PostComment",
    "PostKind",
    "PostLike",
    "SocialGroup",
]


def _invite_code() -> str:
    """12 caractères hexadécimaux — assez pour circuler sur une messagerie
    sans se confondre visuellement, et sans dériver d'un identifiant existant."""
    return secrets.token_hex(6).upper()


class GroupKind(models.TextChoices):
    FAMILY = "family", "Famille"
    FRIENDS = "friends", "Amis"
    WORK = "work", "Travail"
    NEIGHBORHOOD = "neighborhood", "Quartier"
    CUSTOM = "custom", "Personnalisé"


class SocialGroup(UUIDModel, TimeStampedModel):
    """Groupe social — la portée d'un fil de publications privé.

    `member_count` est dénormalisé, et c'est voulu : l'adhésion (voir
    `apps.social.services.SocialService.join`) le compare à `max_members` dans
    un unique `UPDATE ... WHERE member_count < max_members`, la même mécanique
    que le débit conditionnel de F1. Un `COUNT` recalculé à chaque adhésion
    concurrente laisserait deux personnes prendre la dernière place.
    """

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=16, choices=GroupKind.choices, default=GroupKind.CUSTOM)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name="created_groups")

    # Aléatoire et non dérivé de l'identifiant du groupe : un UUIDv7 est
    # ordonné dans le temps, et un code qui circule sur une messagerie ne doit
    # pas laisser deviner l'ordre de création des groupes.
    invite_code = models.CharField(max_length=12, unique=True, default=_invite_code, editable=False)

    is_private = models.BooleanField(default=False)
    max_members = models.PositiveIntegerField(default=50)
    member_count = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "groupe social"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_members__gt=0), name="group_capacity_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(member_count__lte=models.F("max_members")),
                name="group_member_count_within_capacity",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class GroupRole(models.TextChoices):
    CREATOR = "creator", "Créateur"
    ADMIN = "admin", "Administrateur"
    MEMBER = "member", "Membre"


class GroupMembership(UUIDModel):
    group = models.ForeignKey(SocialGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="group_memberships")
    role = models.CharField(max_length=16, choices=GroupRole.choices, default=GroupRole.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "adhésion à un groupe"
        verbose_name_plural = "adhésions aux groupes"
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="one_membership_per_user")
        ]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.group.name}"


class PostKind(models.TextChoices):
    ORDER_SHARE = "order_share", "Partage de commande"
    PHOTO = "photo", "Photo"
    TEXT = "text", "Texte"
    EVENT = "event", "Événement"


class Post(UUIDModel, TimeStampedModel):
    """Publication — libre, ou rattachée à un groupe (S4).

    `likes_count` et `comments_count` sont dénormalisés pour l'affichage en
    liste ; ils n'arbitrent rien (contrairement à `member_count`), donc un
    `F("...") + 1` simple suffit sans le motif conditionnel de F1 — un double
    like est de toute façon empêché en amont par `one_like_per_user`.
    """

    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    group = models.ForeignKey(
        SocialGroup, on_delete=models.CASCADE, null=True, blank=True, related_name="posts"
    )
    content = models.TextField()
    kind = models.CharField(max_length=16, choices=PostKind.choices, default=PostKind.TEXT)
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="shared_posts"
    )
    image_url = models.URLField(blank=True)
    is_public = models.BooleanField(default=True)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "publication"
        ordering = ["-created_at"]
        constraints = [
            # S4 — un post de groupe n'est jamais public.
            models.CheckConstraint(
                condition=models.Q(group__isnull=True) | models.Q(is_public=False),
                name="group_post_not_public",
            ),
            # Un partage de commande désigne la commande partagée.
            models.CheckConstraint(
                condition=~models.Q(kind=PostKind.ORDER_SHARE) | models.Q(order__isnull=False),
                name="order_share_has_order",
            ),
        ]
        indexes = [models.Index(fields=["group", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.author.email} — {self.get_kind_display()}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """S4 — rattache la visibilité au groupe, quel que soit le chemin d'écriture.

        `SocialService.create_post` posait déjà `is_public=group is None`, et le
        sérialiseur tient `is_public` en lecture seule : l'API était donc sûre.
        Elle n'est pas le seul chemin. Le back-office, l'admin Django, une
        commande d'exploitation, une migration de données ou un simple
        `Post(group=g, is_public=True).save()` depuis un shell contournaient
        tous cette dérivation et heurtaient `group_post_not_public` — un
        `IntegrityError` opaque, et une transaction avortée.

        Rattacher un post à un groupe **est** le geste qui le rend privé : la
        visibilité est dérivée, pas choisie. La corriger ici plutôt que refuser
        évite d'inventer une décision que l'appelant n'a pas prise, et rend
        l'invariant vrai par construction plutôt que par vigilance.

        Le déplacement d'un post vers un groupe le rend donc privé ; l'inverse
        ne le rend pas public — sortir un post d'un groupe ne doit pas le
        publier dans le dos de son auteur.
        """
        if self.group_id is not None and self.is_public:
            self.is_public = False
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "is_public" not in update_fields:
                # Sans cela la correction ne partirait pas en base, et la
                # contrainte refuserait la ligne que l'on vient de corriger.
                kwargs["update_fields"] = [*update_fields, "is_public"]

        super().save(*args, **kwargs)


class PostLike(UUIDModel):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="post_likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "j'aime"
        verbose_name_plural = "j'aime"
        constraints = [models.UniqueConstraint(fields=["post", "user"], name="one_like_per_user")]

    def __str__(self) -> str:
        return f"{self.user.email} → {self.post_id}"


class PostComment(UUIDModel, TimeStampedModel):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="post_comments")
    content = models.TextField()

    class Meta:
        verbose_name = "commentaire"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["post", "created_at"])]

    def __str__(self) -> str:
        return f"{self.author.email} — {self.post_id}"
