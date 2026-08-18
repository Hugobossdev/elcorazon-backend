"""Validation et consommation d'un code promo — invariant F4.

Deux temps distincts, et c'est ce qui rend le mécanisme sûr :

* **le devis** (`quote`) ne réserve rien. Il répond à « ce code vaut-il quelque
  chose sur ce panier ? », ce que le client demande avant de commander, parfois
  plusieurs fois de suite. Le laisser consommer un quota permettrait d'épuiser
  un code en le tapant sans jamais commander ;
* **la consommation** (`redeem`) réserve, sous verrou, à la création de la
  commande. C'est là que le quota se décompte, et nulle part ailleurs.

Le verrou n'est pas décoratif. Sans lui, deux clients qui utilisent le dernier
coupon disponible lisent tous deux « il en reste un » et passent tous deux :
c'est la même course que F1 sur le solde de points, et elle se règle de la même
façon — vérifier et écrire dans la même opération.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import User
from apps.promotions.models import DiscountKind, Promotion, PromotionRedemption
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["PromotionQuote", "PromotionService"]


@dataclass(frozen=True, slots=True)
class PromotionQuote:
    promotion: Promotion
    discount: Money


class PromotionRefused(BusinessRuleViolation):
    """Code refusé — période, montant, quota.

    Un code distinct de la violation générique : le client saisit un code et
    attend qu'on lui dise **pourquoi** il ne passe pas. « Vous n'atteignez pas
    le minimum » se corrige en ajoutant un article ; « le code est expiré » non.
    """

    code = "promotion_refused"
    title = "Code promotionnel refusé"


class PromotionService:
    @staticmethod
    def quote(
        *,
        code: str,
        user: User,
        restaurant: Restaurant,
        subtotal: Money,
        delivery_fee: Money,
        moment: dt.datetime | None = None,
    ) -> PromotionQuote:
        """Que vaut ce code sur ce panier ? Sans rien réserver.

        Les cinq conditions de F4 sont vérifiées dans l'ordre où elles
        intéressent le client : d'abord ce qui ne se rattrape pas — le code
        n'existe pas, il est expiré —, ensuite ce qu'il peut corriger — le
        minimum de commande.
        """
        promotion = Promotion.objects.filter(code__iexact=code.strip()).first()
        if promotion is None:
            raise PromotionRefused("Ce code n'existe pas.")

        instant = moment or timezone.now()
        if not promotion.is_open_at(instant):
            raise PromotionRefused(
                "Ce code n'est plus valable.",
                starts_at=promotion.starts_at.isoformat(),
                ends_at=promotion.ends_at.isoformat(),
            )

        if promotion.owner_id is not None and promotion.owner_id != user.pk:
            # Code nominatif présenté par quelqu'un d'autre. Refusé sans dire
            # à qui il appartient — le message serait un annuaire.
            raise PromotionRefused("Ce code n'existe pas.")

        if promotion.restaurant_id is not None and promotion.restaurant_id != restaurant.pk:
            # Un code d'établissement sur un autre établissement : refusé sans
            # dire lequel, pour ne pas transformer la saisie en annuaire.
            raise PromotionRefused("Ce code ne s'applique pas à cet établissement.")

        if promotion.is_exhausted:
            raise PromotionRefused("Ce code a atteint sa limite d'utilisation.")

        if promotion.usage_limit_per_user is not None:
            deja = PromotionRedemption.objects.filter(promotion=promotion, user=user).count()
            if deja >= promotion.usage_limit_per_user:
                raise PromotionRefused(
                    "Vous avez déjà utilisé ce code.",
                    usage_limit_per_user=promotion.usage_limit_per_user,
                )

        if promotion.min_order_amount is not None:
            PromotionService._assert_same_currency(promotion.min_order_amount, subtotal)
            if subtotal < promotion.min_order_amount:
                raise PromotionRefused(
                    f"Ce code demande une commande d'au moins {promotion.min_order_amount}.",
                    min_order_amount=str(promotion.min_order_amount.amount_minor),
                    currency=promotion.min_order_amount.currency,
                )

        discount = PromotionService._compute(promotion, subtotal, delivery_fee)
        if not discount.is_positive:
            raise PromotionRefused("Ce code ne réduit rien sur cette commande.")

        return PromotionQuote(promotion=promotion, discount=discount)

    @staticmethod
    def _compute(promotion: Promotion, subtotal: Money, delivery_fee: Money) -> Money:
        """Montant de la remise, plafonné deux fois.

        Par `max_discount` d'abord — la volonté de l'exploitation — puis par ce
        qu'il y a à remiser. Le second plafond n'est pas de la prudence : la
        base refuse une remise supérieure au sous-total plus les frais, parce
        qu'au-delà la « commande » rapporterait de l'argent au client. Mieux
        vaut la borner ici que se heurter à une violation d'intégrité au milieu
        d'un passage de commande.
        """
        if promotion.kind == DiscountKind.FREE_DELIVERY:
            brut = delivery_fee
        elif promotion.kind == DiscountKind.FIXED:
            montant = promotion.amount
            if montant is None:  # pragma: no cover - refusé par contrainte
                raise PromotionRefused("Ce code est mal configuré.")
            PromotionService._assert_same_currency(montant, subtotal)
            brut = montant
        else:
            brut = subtotal.percentage(promotion.percentage)

        if promotion.max_discount is not None and brut > promotion.max_discount:
            brut = promotion.max_discount

        remisable = subtotal + delivery_fee
        return brut if brut <= remisable else remisable

    @staticmethod
    def _assert_same_currency(left: Money, right: Money) -> None:
        if left.currency != right.currency:
            raise PromotionRefused(
                f"Ce code est libellé en {left.currency}, la commande en {right.currency}."
            )

    # ------------------------------------------------------- consommation

    @staticmethod
    @transaction.atomic
    def redeem(
        *, promotion: Promotion, user: User, order_id: uuid.UUID, discount: Money
    ) -> PromotionRedemption:
        """Consomme une utilisation, sous verrou.

        Le quota est **revérifié à l'intérieur du verrou**, et pas seulement au
        devis : entre le moment où le client a vu « code valide » et celui où
        il valide sa commande, quelqu'un d'autre a pu prendre le dernier
        coupon. Vérifier au devis seul rendrait la limite indicative.

        L'incrément passe par `F()` : lire puis écrire en Python perdrait une
        utilisation sur deux commandes simultanées, ce que le verrou empêche
        déjà — mais la ceinture ne coûte rien et survit à un refactoring qui
        déplacerait le verrou.

        **Le rejeu est traité avant les quotas, et l'ordre n'est pas
        indifférent.** Il était vérifié en dernier, par le rattrapage de la
        violation d'unicité ci-dessous — un chemin que les quotas rendaient
        inatteignable. Rejouer la création d'une commande portant un code limité
        à une utilisation par personne butait sur « Vous avez déjà utilisé ce
        code » : le compte des utilisations de l'utilisateur incluait celle de
        la commande qu'on était précisément en train de rejouer. Le client se
        voyait refuser son propre code parce qu'il l'avait employé sur cette
        commande-là. Même mécanique sur `is_exhausted` lorsque la commande
        rejouée était celle qui avait épuisé le quota global.

        Chercher d'abord l'utilisation de cette commande rend l'idempotence de
        l'ADR-009 réelle, et fait du rejeu une simple lecture : plus d'insertion
        vouée à violer `one_redemption_per_promotion_and_order`, donc plus
        d'erreur inscrite au journal PostgreSQL pour un cas normal.
        """
        verrouillee = Promotion.objects.select_for_update().get(pk=promotion.pk)

        # Le verrou ci-dessus sérialise les consommations d'un même code : cette
        # lecture voit donc tout ce qui a été validé avant elle.
        deja_consommee = PromotionRedemption.objects.filter(
            promotion=verrouillee, order_id=order_id
        ).first()
        if deja_consommee is not None:
            return deja_consommee

        if verrouillee.is_exhausted:
            raise PromotionRefused("Ce code vient d'atteindre sa limite d'utilisation.")

        if verrouillee.usage_limit_per_user is not None:
            deja = PromotionRedemption.objects.filter(promotion=verrouillee, user=user).count()
            if deja >= verrouillee.usage_limit_per_user:
                raise PromotionRefused("Vous avez déjà utilisé ce code.")

        try:
            # Point de reprise imbriqué : une violation d'unicité casse la
            # transaction courante, et sans lui plus aucune requête ne passerait
            # après — y compris la relecture qui suit. Conservé pour la course
            # que la lecture ci-dessus ne peut pas fermer à elle seule : deux
            # transactions concurrentes sur des promotions différentes ne se
            # verrouillent pas mutuellement.
            with transaction.atomic():
                # `discount` est un `MoneyField` — deux colonnes derrière une
                # propriété, que le greffon django-stubs ne relie pas au nom.
                redemption = PromotionRedemption.objects.create(  # type: ignore[misc]
                    promotion=verrouillee, user=user, order_id=order_id, discount=discount
                )
        except IntegrityError:
            return PromotionRedemption.objects.get(promotion=verrouillee, order_id=order_id)

        Promotion.objects.filter(pk=verrouillee.pk).update(used_count=F("used_count") + 1)
        return redemption

    @staticmethod
    @transaction.atomic
    def release(*, order_id: uuid.UUID) -> int:
        """Rend l'utilisation consommée par une commande annulée.

        Sans cela, un client dont la commande est annulée par le restaurant
        perd son code : il a été décompté pour un repas qu'il n'a jamais reçu.
        Le compteur global est décrémenté d'autant.
        """
        rendues = 0
        for redemption in PromotionRedemption.objects.select_related("promotion").filter(
            order_id=order_id
        ):
            Promotion.objects.filter(pk=redemption.promotion_id, used_count__gt=0).update(
                used_count=F("used_count") - 1
            )
            redemption.delete()
            rendues += 1

        return rendues
