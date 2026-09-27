"""Règles du catalogue qui ne tiennent pas dans une vue.

L'ADR-003 est explicite : le CRUD du catalogue va du ViewSet à l'ORM, sans
service. Ce module ne contient donc **que** ce qui porte une décision métier ou
une transaction :

* l'avis, qui a les deux — la mention « achat vérifié » est décidée par le
  serveur (S1), et écrire l'avis puis rafraîchir la note de l'article doivent
  réussir ou échouer ensemble, sinon la moyenne affichée cesse de correspondre
  aux avis affichés ;
* le stock, dont le retrait doit être **conditionnel et atomique** : deux
  commandes simultanées ne peuvent pas emporter la dernière unité chacune.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Avg, Count, F
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import MenuItem, Review, VerifiedPurchase
from common.audit import AuditAction, record_change
from common.exceptions import BusinessRuleViolation

__all__ = ["ReviewService", "StockService", "record_purchase"]


def record_purchase(*, user: User, menu_item: MenuItem, moment: dt.datetime) -> VerifiedPurchase:
    """Enregistre qu'un client a bien reçu un article.

    Appelée par `orders` à la livraison — le sens autorisé par le graphe de
    dépendances (ADR-002). Idempotente : dix commandes du même article
    n'écrivent qu'une ligne, dont seule la date se rafraîchit.
    """
    purchase, _ = VerifiedPurchase.objects.update_or_create(
        user=user, menu_item=menu_item, defaults={"last_purchased_at": moment}
    )
    return purchase


class StockService:
    """Mouvements de stock — appelés par `orders`, dans le sens du graphe.

    `catalog` ne connaît pas les commandes (ADR-002) : il expose un verbe, et
    c'est la commande qui l'appelle, comme elle appelle déjà `record_purchase`
    à la livraison.
    """

    @staticmethod
    def consume(quantities: Mapping[uuid.UUID, int]) -> None:
        """Retire du stock ce qu'une commande emporte.

        Le retrait est **conditionnel en une seule instruction** : le filtre
        `stock_quantity__gte` et le `update(F(...) - n)` sont évalués par
        PostgreSQL dans la même requête, sous le verrou de ligne qu'elle prend.
        Deux commandes simultanées ne peuvent donc pas lire le même stock et
        emporter la dernière unité chacune.

        C'est F1 transposé au catalogue. Lire puis écrire en deux temps
        laisserait une fenêtre — étroite, et c'est précisément le coup de feu
        du samedi soir qui l'élargit, c'est-à-dire le moment où le stock
        compte.

        Les articles sans suivi de stock traversent sans rien décompter : c'est
        le cas courant d'un plat préparé à la demande.
        """
        for item_id, quantity in quantities.items():
            touchees = MenuItem.objects.filter(
                pk=item_id, tracks_stock=True, stock_quantity__gte=quantity
            ).update(stock_quantity=F("stock_quantity") - quantity)
            if touchees:
                continue

            # Aucune ligne touchée : soit l'article ne suit pas de stock — rien
            # à faire —, soit il n'en reste pas assez. Les deux cas se
            # distinguent par une relecture, faite seulement dans ce cas rare.
            item = MenuItem.objects.filter(pk=item_id).first()
            if item is None or not item.tracks_stock:
                continue

            raise BusinessRuleViolation(
                f"Il ne reste que {item.stock_quantity} unité(s) de « {item.name} ».",
                menu_item_id=str(item_id),
                remaining=item.stock_quantity,
            )

    @staticmethod
    def restore(quantities: Mapping[uuid.UUID, int]) -> None:
        """Rend au stock ce qu'une commande annulée n'emportera pas.

        Sans ce retour, chaque annulation retirerait définitivement des unités
        jamais servies, et le stock affiché dériverait à la baisse jusqu'à
        fermer un article encore disponible en réserve.

        Un article dont le suivi de stock a été **activé après** la commande ne
        reçoit rien : la commande n'avait rien décompté, et le créditer
        inventerait des unités. L'inverse — suivi désactivé entre-temps — perd
        le retour, ce qui est le bon sens de la panne : on préfère un stock
        sous-estimé, qu'un réapprovisionnement corrige, à un stock inventé qui
        fait vendre ce qu'on n'a pas.
        """
        for item_id, quantity in quantities.items():
            MenuItem.objects.filter(pk=item_id, tracks_stock=True).update(
                stock_quantity=F("stock_quantity") + quantity
            )


class ReviewService:
    """Écriture d'un avis et entretien des agrégats de note."""

    @staticmethod
    @transaction.atomic
    def submit(
        *, user: User, menu_item: MenuItem, rating: int, title: str = "", comment: str = ""
    ) -> Review:
        """Dépose un avis.

        S5 — un seul avis par article et par utilisateur. La contrainte est en
        base ; la vérification préalable n'est là que pour rendre le refus
        lisible (409 avec un code stable) plutôt qu'une violation d'intégrité.

        S1 — `is_verified_purchase` n'est jamais lu depuis la requête. Le champ
        est `editable=False`, donc absent des sérialiseurs générés : l'oubli
        est impossible, pas seulement improbable.
        """
        if Review.objects.filter(menu_item=menu_item, user=user).exists():
            raise BusinessRuleViolation(
                "Un avis a déjà été déposé sur cet article.", menu_item_id=str(menu_item.pk)
            )

        review = Review(
            menu_item=menu_item,
            user=user,
            rating=rating,
            title=title,
            comment=comment,
        )
        review.is_verified_purchase = VerifiedPurchase.objects.filter(
            user=user, menu_item=menu_item
        ).exists()
        review.save()

        ReviewService.refresh_rating(menu_item)
        return review

    @staticmethod
    def refresh_rating(menu_item: MenuItem) -> None:
        """Recalcule `rating_average` et `rating_count` depuis les avis.

        Recalcul complet plutôt que moyenne glissante : une moyenne entretenue
        par incréments dérive dès qu'un avis est supprimé ou modéré, et l'écart
        ne se voit jamais. Le coût est celui d'un `AVG` sur l'index
        `(menu_item, -created_at)`, payé à l'écriture d'un avis — soit
        plusieurs milliers de fois moins souvent qu'une lecture de menu.
        """
        # Les avis masqués sortent de la note : un avis retiré pour insulte
        # qui continuerait de peser sur la moyenne resterait publié par un
        # autre moyen.
        aggregate = Review.objects.filter(menu_item=menu_item, hidden_at__isnull=True).aggregate(
            average=Avg("rating"), total=Count("id")
        )
        average = Decimal(aggregate["average"] or 0).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        MenuItem.objects.filter(pk=menu_item.pk).update(
            rating_average=average, rating_count=aggregate["total"]
        )
        # L'instance en mémoire est ressynchronisée : la vue la sérialise juste
        # après, et elle rendrait sinon la note d'avant l'avis qu'on vient
        # d'écrire.
        menu_item.rating_average = average
        menu_item.rating_count = aggregate["total"]


class ReviewModerationService:
    """Masquer et réafficher un avis — le seul geste de modération.

    Le module disait l'attendre : « ce sont des gestes de modération, qui
    appellent une trace d'audit et une permission dédiée ». Les voici — sous
    `catalog.write`, journalisés, motif exigé pour masquer.
    """

    @staticmethod
    @transaction.atomic
    def hide(*, review: Review, actor: User, reason: str) -> Review:
        if not reason.strip():
            raise BusinessRuleViolation("Dites pourquoi cet avis est masqué.")
        verrouille = (
            Review.objects.select_for_update().select_related("menu_item").get(pk=review.pk)
        )
        if verrouille.hidden_at is not None:
            return verrouille
        verrouille.hidden_at = timezone.now()
        verrouille.hidden_reason = reason.strip()
        verrouille.hidden_by = actor
        verrouille.save(update_fields=["hidden_at", "hidden_reason", "hidden_by", "updated_at"])
        ReviewModerationService._consigner(verrouille, actor, visible_avant=True)
        ReviewService.refresh_rating(verrouille.menu_item)
        return verrouille

    @staticmethod
    @transaction.atomic
    def show(*, review: Review, actor: User) -> Review:
        verrouille = (
            Review.objects.select_for_update().select_related("menu_item").get(pk=review.pk)
        )
        if verrouille.hidden_at is None:
            return verrouille
        verrouille.hidden_at = None
        verrouille.hidden_reason = ""
        verrouille.hidden_by = None
        verrouille.save(update_fields=["hidden_at", "hidden_reason", "hidden_by", "updated_at"])
        ReviewModerationService._consigner(verrouille, actor, visible_avant=False)
        ReviewService.refresh_rating(verrouille.menu_item)
        return verrouille

    @staticmethod
    def _consigner(review: Review, actor: User, *, visible_avant: bool) -> None:
        record_change(
            actor=actor,
            action=AuditAction.REVIEW_VISIBILITY,
            target_type="review",
            target_id=review.pk,
            target_label=f"{review.rating}/5 — {review.menu_item.name}",
            before={"visible": visible_avant},
            after={"visible": not visible_avant, "reason": review.hidden_reason},
            # La cuisine de l'article : sans elle, le gérant qui vient de masquer
            # un avis ne relisait pas son propre geste — le journal est cloisonné.
            scope_restaurant_id=review.menu_item.restaurant_id,
        )
