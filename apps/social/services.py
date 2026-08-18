"""Groupes et publications — capacité atomique, partage vérifié (S3).

**L'adhésion à un groupe suit le même motif que le débit de points (F1)** :
`SocialGroup.member_count` et `max_members` sont comparés et écrits dans un
seul `UPDATE ... WHERE member_count < max_members`. Deux personnes qui
rejoignent au même instant la dernière place disponible ne peuvent pas passer
toutes les deux — l'une trouve zéro ligne affectée et se voit refuser, l'autre
prend la place. Un `COUNT` suivi d'une comparaison en Python laisserait
exactement la course que F1 a fermée sur la fidélité.

**S3 — partager une commande exige d'en être le propriétaire.** Elle expose
l'adresse de livraison ; l'implémentation précédente validait `order_id` par
simple existence, ce qui permettait de partager la commande de quelqu'un
d'autre.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import F

from apps.accounts.models import User
from apps.orders.models import Order
from apps.social.models import (
    GroupMembership,
    GroupRole,
    Post,
    PostComment,
    PostKind,
    PostLike,
    SocialGroup,
)
from common.exceptions import BusinessRuleViolation

__all__ = ["GroupFull", "InvalidInviteCode", "PostRefused", "SocialService"]


class InvalidInviteCode(BusinessRuleViolation):
    code = "invalid_invite_code"
    title = "Code d'invitation invalide"


class GroupFull(BusinessRuleViolation):
    code = "group_full"
    title = "Groupe complet"


class PostRefused(BusinessRuleViolation):
    """Publication refusée — appartenance au groupe, propriété de la commande."""

    code = "post_refused"
    title = "Publication refusée"


@dataclass(frozen=True, slots=True)
class GroupWithMembership:
    group: SocialGroup
    membership: GroupMembership


class SocialService:
    @staticmethod
    @transaction.atomic
    def create_group(
        *,
        creator: User,
        name: str,
        description: str = "",
        kind: str,
        is_private: bool = False,
        max_members: int = 50,
    ) -> GroupWithMembership:
        group = SocialGroup.objects.create(
            creator=creator,
            name=name,
            description=description,
            kind=kind,
            is_private=is_private,
            max_members=max_members,
            member_count=1,
        )
        membership = GroupMembership.objects.create(
            group=group, user=creator, role=GroupRole.CREATOR
        )
        return GroupWithMembership(group=group, membership=membership)

    @staticmethod
    @transaction.atomic
    def join(*, user: User, invite_code: str) -> GroupMembership:
        """Rejoint un groupe par son code — **capacité vérifiée sous verrou**.

        Rejouer l'adhésion d'un membre déjà actif est sans effet : il n'y a
        rien à réserver une seconde fois, et le traiter en erreur obligerait
        le client à distinguer « déjà membre » de « bienvenue », ce qui ne
        change rien à ce qu'il voit.
        """
        group = SocialGroup.objects.filter(
            invite_code__iexact=invite_code.strip(), is_active=True
        ).first()
        if group is None:
            raise InvalidInviteCode("Ce code d'invitation n'existe pas.")

        existing = GroupMembership.objects.filter(group=group, user=user).first()
        if existing is not None and existing.is_active:
            return existing

        affected = SocialGroup.objects.filter(
            pk=group.pk, member_count__lt=F("max_members")
        ).update(member_count=F("member_count") + 1)
        if not affected:
            raise GroupFull(
                "Ce groupe a atteint sa capacité maximale.", max_members=group.max_members
            )
        # `group` porte encore `member_count` d'avant l'`UPDATE` : le recharger
        # évite de renvoyer un compteur périmé à l'appelant (membership.group).
        group.refresh_from_db()

        if existing is not None:
            GroupMembership.objects.filter(pk=existing.pk).update(is_active=True)
            existing.refresh_from_db()
            return existing

        return GroupMembership.objects.create(group=group, user=user, role=GroupRole.MEMBER)

    @staticmethod
    @transaction.atomic
    def leave(*, user: User, group: SocialGroup) -> None:
        """Départ — symétrique de l'adhésion : la place est rendue à la capacité.

        Conditionné à `is_active=True` pour la même raison que le débit de
        points : un départ rejoué ne doit pas décrémenter deux fois une
        capacité déjà rendue.
        """
        affected = GroupMembership.objects.filter(group=group, user=user, is_active=True).update(
            is_active=False
        )
        if affected:
            SocialGroup.objects.filter(pk=group.pk).update(member_count=F("member_count") - 1)

    @staticmethod
    def create_post(
        *,
        author: User,
        content: str,
        kind: str = PostKind.TEXT,
        group: SocialGroup | None = None,
        order: Order | None = None,
        image_url: str = "",
    ) -> Post:
        if group is not None:
            est_membre = GroupMembership.objects.filter(
                group=group, user=author, is_active=True
            ).exists()
            if not est_membre:
                raise PostRefused("Vous n'êtes pas membre de ce groupe.")

        if kind == PostKind.ORDER_SHARE:
            if order is None or order.customer_id != author.pk:
                # S3 — même refusé, le message ne confirme pas si la commande
                # existe : ce serait révéler qu'une commande d'autrui existe.
                raise PostRefused("Vous ne pouvez partager que vos propres commandes.")

        return Post.objects.create(
            author=author,
            content=content,
            kind=kind,
            group=group,
            order=order if kind == PostKind.ORDER_SHARE else None,
            image_url=image_url,
            # S4 — un post de groupe n'est jamais public, sans exception.
            is_public=group is None,
        )

    @staticmethod
    @transaction.atomic
    def toggle_like(*, user: User, post: Post) -> bool:
        """Bascule le j'aime. Renvoie l'état résultant (aimé ou non).

        `get_or_create` **lit avant d'écrire**, là où le `create` rattrapé qui
        précédait écrivait d'abord et avalait l'erreur. Deux conséquences, dont
        la seconde est la moins visible et la plus gênante :

        1. chaque j'aime concurrent laissait un `duplicate key value violates
           unique constraint "one_like_per_user"` dans le journal PostgreSQL,
           pour un geste aussi banal qu'un double clic sur un cœur. Le journal
           se remplissait d'erreurs qui n'en sont pas, et les vraies s'y
           noyaient ;
        2. le `create` de Django s'exécute dans un `atomic(savepoint=False)`.
           Une violation d'unicité y pose donc `connection.needs_rollback`, et
           le bloc `atomic` de cette méthode — le plus externe, `ATOMIC_REQUESTS`
           valant `False` — en sort par un **rollback silencieux** au lieu d'un
           commit. Ici le tour est sans dommage, la méthode n'ayant rien d'autre
           à écrire ; il ne le resterait pas si quelqu'un ajoutait une écriture
           (une notification, un événement d'analyse) au même bloc, et le défaut
           ne se signalerait alors par aucune erreur.

        `get_or_create` supprime les deux : le chemin courant est un `SELECT`,
        et l'insertion qu'il conserve pour la vraie course est protégée par son
        propre point de reprise. Le j'aime concurrent est réutilisé, et le
        compteur ne s'incrémente que pour le gagnant — `created` le dit —, ce
        qui l'empêche de compter deux fois un unique j'aime.
        """
        like = PostLike.objects.filter(post=post, user=user).first()
        if like is not None:
            like.delete()
            Post.objects.filter(pk=post.pk).update(likes_count=F("likes_count") - 1)
            return False

        _, created = PostLike.objects.get_or_create(post=post, user=user)
        if created:
            Post.objects.filter(pk=post.pk).update(likes_count=F("likes_count") + 1)
        return True

    @staticmethod
    def add_comment(*, user: User, post: Post, content: str) -> PostComment:
        comment = PostComment.objects.create(post=post, author=user, content=content)
        Post.objects.filter(pk=post.pk).update(comments_count=F("comments_count") + 1)
        return comment
