"""Affectation et cycle de vie des courses — invariants L1, L2, L4, L5.

Ce module est le seul chemin d'écriture du statut d'une course, et le seul
endroit qui projette ce statut sur la commande. La projection est **déclarée**
dans `states.ORDER_STATUS_PROJECTION` et appliquée ici : c'est une projection
écrite à la main, dispersée dans les contrôleurs, qui avait produit C4.

L2 mérite un mot. La contrainte d'unicité partielle en base suffit à empêcher
deux courses actives sur une commande ; le verrou applicatif qu'on pose en plus
n'est pas une redondance décorative. Sans lui, le second livreur reçoit une
`IntegrityError` — une erreur 500 illisible — au lieu d'un refus métier qui lui
dit que la course vient d'être prise.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, CourierRating
from apps.delivery.signals import assignment_offered
from apps.delivery.states import (
    DELIVERY_MACHINE,
    ORDER_STATUS_PROJECTION,
    VERIFICATION_MACHINE,
    DeliveryStatus,
    VerificationStatus,
)
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.realtime import courier_group, order_group, publish

__all__ = [
    "AssignmentService",
    "CourierApplication",
    "CourierRatingService",
    "CourierService",
    "courier_fee_for",
]

#: Statuts de commande depuis lesquels une course peut être proposée.
#:
#: Ni avant `confirmed` — le paiement n'est pas acquis et la cuisine n'a rien
#: lancé — ni après `ready`, où le repas est déjà parti. Proposer plus tôt
#: mobiliserait un livreur pour une commande qui peut encore être annulée sans
#: frais.
OFFERABLE_FROM = frozenset({OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY})


def courier_fee_for(order: Order) -> Money:
    """Part revenant au livreur, calculée sur la **valeur** de la course.

    Sur `delivery_fee_gross` et non sur `delivery_fee` : le second est ce que
    le client a payé, et il tombe à zéro dès que le franco s'applique. Fonder
    la commission dessus ferait rouler le livreur gratuitement chaque fois
    qu'un panier dépasse le seuil — une remise commerciale offerte au client
    aux frais de quelqu'un qui n'a rien décidé.

    Un pourcentage configurable plutôt qu'un montant recopié : un point de
    commission ne doit pas demander un déploiement. La part est **figée sur la
    course à l'acceptation** — le barème peut évoluer, ce qui est dû pour cette
    course ne bouge plus.
    """
    # Les commandes antérieures à `delivery_fee_gross` n'ont que le montant
    # facturé ; c'était alors la seule valeur connue.
    valeur = order.delivery_fee_gross or order.delivery_fee
    return valeur.percentage(settings.COURIER_FEE_SHARE_PERCENT)


@dataclass(frozen=True, slots=True)
class CourierApplication:
    """Ce qu'il faut pour ouvrir un compte livreur.

    Un DTO gelé et non neuf paramètres nommés : c'est la frontière que
    l'ADR-003 désigne pour les services de livraison, et l'immuabilité garantit
    que le service ne se réécrit pas ses propres entrées entre la validation et
    la création.

    **`verification_status` n'y figure pas**, et c'est le point : un dossier
    naît en attente, quel que soit celui qui l'ouvre. Le personnel qui embauche
    n'instruit pas le dossier dans le même geste — les pièces ne sont pas encore
    déposées, il n'y a rien à valider — et laisser le champ en entrée
    permettrait de créer un livreur déjà validé sans qu'aucune pièce n'ait été
    lue.
    """

    email: str
    password: str
    full_name: str
    restaurant: Restaurant
    vehicle_type: str
    phone: str = ""
    vehicle_plate: str = ""
    national_id_number: str = ""
    licence_number: str = ""


class CourierService:
    """Dossier livreur : ouverture, validation, disponibilité, pièces."""

    @staticmethod
    @transaction.atomic
    def provision(*, application: CourierApplication) -> CourierProfile:
        """Ouvre un compte livreur et son dossier — les deux, ou aucun.

        C'est le pendant de `AuthService.register`, qui ne crée que des clients
        par conception : un livreur n'est pas quelqu'un qui s'inscrit, c'est
        quelqu'un qu'on embauche, et son dossier le rattache à un établissement
        (`CourierProfile.restaurant`, obligatoire). Il n'y a donc pas
        d'inscription en self-service à laquelle répondre — décision actée en
        session le 2026-07-29 : le provisioning est un geste du personnel, sous
        `couriers.write`.

        La transaction est ce qui compte ici. Créer le compte puis échouer sur
        le dossier laisserait un `User` de type livreur sans `CourierProfile` —
        exactement l'anomalie que `courier_of` traite en 404, un compte qui
        peut se connecter à l'application livreur et n'y trouver aucun dossier.

        Le mot de passe est posé par le personnel et communiqué au livreur ;
        rien n'en force le changement à la première connexion, faute de
        mécanisme pour cela — le livreur le change depuis son application.
        """
        user = User.objects.create_user(
            email=application.email,
            password=application.password,
            full_name=application.full_name,
            phone=application.phone or None,
            # Jamais lu d'une requête : c'est ce champ qui décide de ce qu'un
            # jeton autorise, et l'accepter en entrée ferait de cette route un
            # chemin d'escalade — on s'y créerait un compte du personnel.
            user_type=UserType.COURIER,
        )
        return CourierProfile.objects.create(
            user=user,
            restaurant=application.restaurant,
            vehicle_type=application.vehicle_type,
            vehicle_plate=application.vehicle_plate,
            national_id_number=application.national_id_number,
            licence_number=application.licence_number,
        )

    @staticmethod
    @transaction.atomic
    def review(
        *,
        courier: CourierProfile,
        target: str,
        actor: User,
        notes: str = "",
    ) -> CourierProfile:
        """Fait avancer le dossier — validation, rejet, suspension.

        La machine du dossier est la seule **cyclique** du projet : un dossier
        se ré-instruit, alors qu'une course ne se re-livre pas. Le passage par
        `VERIFICATION_MACHINE` garantit qu'on ne saute pas d'étape pour autant
        — on ne suspend pas un dossier jamais validé.
        """
        locked = CourierProfile.objects.select_for_update().get(pk=courier.pk)
        if VERIFICATION_MACHINE.is_noop(locked.verification_status, target):
            return locked

        VERIFICATION_MACHINE.validate(locked.verification_status, target)

        locked.verification_status = target
        locked.verification_notes = notes
        locked.verified_by = actor
        locked.verified_at = timezone.now()

        # Un dossier qui cesse d'être validé remet le livreur hors ligne. Sans
        # cela, il resterait « en ligne » et continuerait d'apparaître dans les
        # listes d'affectation, où seul `can_accept_orders` l'écarterait — une
        # garde de plus à ne pas oublier ailleurs.
        if target != VerificationStatus.APPROVED:
            locked.is_online = False

        locked.save(
            update_fields=[
                "verification_status",
                "verification_notes",
                "verified_by",
                "verified_at",
                "is_online",
                "updated_at",
            ]
        )
        return locked

    @staticmethod
    def set_online(*, courier: CourierProfile, is_online: bool) -> CourierProfile:
        """Bascule de disponibilité, à l'initiative du livreur.

        Se mettre en ligne exige un dossier validé (L1). Le refus est explicite
        plutôt que silencieux : un livreur qui bascule l'interrupteur et ne
        reçoit aucune course ne doit pas avoir à deviner que son dossier est en
        attente.
        """
        if is_online and courier.verification_status != VerificationStatus.APPROVED:
            raise BusinessRuleViolation(
                "Votre dossier n'est pas validé ; vous ne pouvez pas encore recevoir de courses.",
                verification_status=courier.verification_status,
            )

        courier.is_online = is_online
        courier.save(update_fields=["is_online", "updated_at"])
        return courier

    @staticmethod
    @transaction.atomic
    def replace_documents(*, courier: CourierProfile, **documents: object) -> CourierProfile:
        """Remplace des pièces justificatives — **et repasse le dossier en attente** (L5).

        C'est une règle de conformité, pas une commodité : un dossier validé
        sur des pièces qu'on a ensuite remplacées n'est plus un dossier validé.
        Laisser l'approbation en place reviendrait à valider des documents que
        personne n'a lus.
        """
        for field, value in documents.items():
            setattr(courier, field, value)

        touched = [*documents]
        if courier.verification_status == VerificationStatus.APPROVED:
            courier.verification_status = VerificationStatus.PENDING
            courier.is_online = False
            touched += ["verification_status", "is_online"]

        courier.save(update_fields=[*touched, "updated_at"])
        return courier

    @staticmethod
    def available_for(order: Order) -> QuerySet[CourierProfile]:
        """Livreurs éligibles pour cette commande, du plus proche au plus loin.

        Le tri est fait par PostGIS depuis la position du **restaurant** : le
        livreur doit d'abord y arriver. Trier depuis l'adresse de livraison
        privilégierait quelqu'un déjà à l'autre bout de la course.

        Un livreur sans position connue reste dans la liste, en fin de tri :
        l'écarter reviendrait à exclure celui qui vient de démarrer son
        application.
        """
        return (
            CourierProfile.objects.filter(
                restaurant=order.restaurant,
                is_online=True,
                verification_status=VerificationStatus.APPROVED,
                user__is_active=True,
            )
            .select_related("user")
            .annotate(to_restaurant=Distance("last_location", order.restaurant.location))
            .order_by("to_restaurant")
        )


class AssignmentService:
    # ---------------------------------------------------------------- offre

    @staticmethod
    @transaction.atomic
    def offer(*, order: Order, courier: CourierProfile, actor: User | None = None) -> Assignment:
        """Propose une course à un livreur.

        Le verrou porte sur la **commande** et non sur la course : ce qu'on
        protège est l'unicité de la course active, qui est une propriété de la
        commande. Verrouiller la course qu'on s'apprête à créer ne protégerait
        rien.
        """
        locked = Order.objects.select_for_update().get(pk=order.pk)

        if locked.status not in OFFERABLE_FROM:
            raise BusinessRuleViolation(
                "Une course ne se propose qu'entre la confirmation et la mise à "
                "disposition du repas.",
                current_status=locked.status,
            )
        if not courier.can_accept_orders:
            # L1 — relu depuis le dossier, jamais déduit d'un jeton ni d'un
            # champ envoyé par le client.
            raise BusinessRuleViolation(
                "Ce livreur n'est pas éligible : dossier non validé, hors ligne "
                "ou compte désactivé.",
                courier_id=str(courier.pk),
            )
        if courier.restaurant_id != locked.restaurant_id:
            raise BusinessRuleViolation(
                "Ce livreur n'est pas rattaché à l'établissement de la commande."
            )

        active = AssignmentService._active_for(locked)
        if active is not None:
            raise BusinessRuleViolation(
                "Cette commande a déjà une course en cours.",
                assignment_id=str(active.pk),
                assignment_status=active.status,
            )

        assignment = Assignment.objects.create(order=locked, courier=courier)

        # C'est le flux où rater un événement coûte le plus cher : une course
        # non vue est un repas qui refroidit. La notification push la doublera,
        # parce qu'un livreur n'a pas son application au premier plan en
        # roulant (ADR-008).
        transaction.on_commit(
            lambda: publish(
                courier_group(courier.pk),
                "delivery.offered",
                {
                    "assignment": str(assignment.pk),
                    "order": str(locked.pk),
                    "reference": locked.reference,
                    "restaurant": locked.restaurant.name,
                    "delivery_address_line": locked.delivery_address_line,
                },
            )
        )
        assignment_offered.send(sender=Assignment, assignment=assignment)
        return assignment

    @staticmethod
    def _active_for(order: Order) -> Assignment | None:
        return order.assignments.exclude(
            status__in=[
                DeliveryStatus.DECLINED,
                DeliveryStatus.CANCELLED,
                DeliveryStatus.DELIVERED,
            ]
        ).first()

    # ----------------------------------------------------------- acceptation

    @staticmethod
    @transaction.atomic
    def accept(*, assignment: Assignment, courier: CourierProfile) -> Assignment:
        """Acceptation par le livreur — exclusive et atomique (L2).

        L'ancien code n'avait aucun verrou : deux livreurs pouvaient prendre la
        même course, et l'un des deux faisait le trajet pour rien. Le verrou
        est posé sur la commande, dans le même ordre que `offer`, pour que deux
        chemins concurrents ne s'interbloquent pas.
        """
        Order.objects.select_for_update().get(pk=assignment.order_id)
        current = Assignment.objects.select_related("order").get(pk=assignment.pk)

        if current.courier_id != courier.pk:
            raise BusinessRuleViolation("Cette course est proposée à un autre livreur.")
        if not courier.can_accept_orders:
            raise BusinessRuleViolation(
                "Votre dossier ne vous permet pas d'accepter une course.",
                verification_status=courier.verification_status,
            )

        DELIVERY_MACHINE.validate(current.status, DeliveryStatus.ACCEPTED)

        current.status = DeliveryStatus.ACCEPTED
        current.accepted_at = timezone.now()
        # Rémunération figée maintenant : le barème peut changer d'ici la
        # livraison, ce qui est dû pour cette course ne change plus.
        current.courier_fee = courier_fee_for(current.order)
        current.save(
            update_fields=[
                "status",
                "accepted_at",
                "courier_fee_minor",
                "courier_fee_currency",
                "updated_at",
            ]
        )
        return current

    @staticmethod
    @transaction.atomic
    def decline(*, assignment: Assignment, courier: CourierProfile, reason: str = "") -> Assignment:
        """Refus par le livreur : la commande redevient proposable à un autre."""
        if assignment.courier_id != courier.pk:
            raise BusinessRuleViolation("Cette course est proposée à un autre livreur.")

        DELIVERY_MACHINE.validate(assignment.status, DeliveryStatus.DECLINED)

        assignment.status = DeliveryStatus.DECLINED
        assignment.decline_reason = reason
        assignment.save(update_fields=["status", "decline_reason", "updated_at"])
        return assignment

    # ---------------------------------------------------------- progression

    @staticmethod
    @transaction.atomic
    def transition_to(
        *,
        assignment: Assignment,
        target: str,
        actor: User | None = None,
        reason: str = "",
    ) -> Assignment:
        """Fait avancer une course et **projette** son statut sur la commande.

        Un rejeu vers le statut courant ne fait rien : un livreur qui tapote
        deux fois « récupéré » dans une zone à réseau instable ne doit pas
        recevoir d'erreur, ni voir la commande avancer deux fois.
        """
        locked = (
            Assignment.objects.select_for_update()
            .select_related("order", "courier")
            .get(pk=assignment.pk)
        )
        if DELIVERY_MACHINE.is_noop(locked.status, target):
            return locked

        DELIVERY_MACHINE.validate(locked.status, target)

        # Une commande annulée arrête la course. La règle vit ici et non dans
        # une projection inverse : `orders` ne connaît pas `delivery` (ADR-002),
        # c'est donc à la course de se tenir au courant de sa commande. Sans
        # cette garde, un livreur continuerait à faire avancer — et à se faire
        # créditer — une course dont le repas ne partira jamais.
        if locked.order.status == OrderStatus.CANCELLED and target != DeliveryStatus.CANCELLED:
            raise BusinessRuleViolation(
                "La commande a été annulée ; cette course ne peut plus avancer.",
                order_status=locked.order.status,
            )

        locked.status = target
        touched = ["status"]
        if target == DeliveryStatus.PICKED_UP:
            locked.picked_up_at = timezone.now()
            touched.append("picked_up_at")
        elif target == DeliveryStatus.DELIVERED:
            locked.delivered_at = timezone.now()
            touched.append("delivered_at")
        elif target == DeliveryStatus.CANCELLED:
            locked.decline_reason = reason
            touched.append("decline_reason")

        locked.save(update_fields=[*touched, "updated_at"])

        # L'étape de course est diffusée pour elle-même, en plus du statut de
        # commande que la projection émettra peut-être : « votre livreur a
        # récupéré la commande » et « commande récupérée » sont le même instant
        # mais pas la même information — l'une nomme le livreur, l'autre pas.
        transaction.on_commit(
            lambda: publish(
                order_group(locked.order_id),
                "delivery.status",
                {
                    "assignment": str(locked.pk),
                    "order": str(locked.order_id),
                    "status": target,
                    "courier": locked.courier.user.full_name,
                },
            )
        )

        AssignmentService._project(locked, target, actor=actor, reason=reason)

        if target == DeliveryStatus.DELIVERED:
            AssignmentService._credit(locked)
        elif target == DeliveryStatus.CANCELLED:
            # `F(...) + 1` plutôt qu'une lecture suivie d'une écriture : deux
            # annulations concurrentes en perdraient une, et le compteur du
            # livreur mentirait sans que rien ne le signale.
            CourierProfile.objects.filter(pk=locked.courier_id).update(
                deliveries_cancelled=F("deliveries_cancelled") + 1
            )

        return locked

    @staticmethod
    def _project(assignment: Assignment, target: str, *, actor: User | None, reason: str) -> None:
        """Répercute l'étape de course sur la commande, si elle en a une.

        `offered`, `accepted` et `declined` ne projettent rien : ce sont des
        événements internes à l'affectation. La commande reste `ready` tant que
        le repas n'est pas parti — c'est en voulant projeter `accepted` que
        l'ancien code écrivait un statut hors énumération.
        """
        projected = ORDER_STATUS_PROJECTION.get(target)
        if projected is None:
            return
        if not ORDER_MACHINE.can(assignment.order.status, projected):
            # La commande a été menée ailleurs entre-temps — annulée par le
            # restaurant, par exemple. La course, elle, a bien avancé : on ne
            # force pas la commande à suivre, et on ne fait pas échouer le
            # livreur pour une décision prise sans lui.
            return

        OrderService.transition_to(
            order=assignment.order, target=projected, actor=actor, reason=reason
        )

    @staticmethod
    def _credit(assignment: Assignment) -> None:
        """Incrémente les compteurs et les gains du livreur — **une seule fois** (L4).

        La garde n'est pas ici mais dans le graphe : `delivered` est terminal,
        donc la transition ne peut pas être rejouée, donc les compteurs ne
        peuvent pas l'être non plus. C'est ce qui ferme C3, où rejouer
        `delivered` réincrémentait les compteurs à chaque appel.
        """
        courier = CourierProfile.objects.select_for_update().get(pk=assignment.courier_id)
        courier.deliveries_completed += 1

        earned = assignment.courier_fee
        if earned is not None:
            current = courier.total_earnings or Money.zero(earned.currency)
            courier.total_earnings = current + earned

        courier.save(
            update_fields=[
                "deliveries_completed",
                "total_earnings_minor",
                "total_earnings_currency",
                "updated_at",
            ]
        )


class CourierRatingService:
    """Note d'une course par le client qui l'a reçue.

    Deux règles, et elles tiennent en base autant qu'ici : on ne note qu'une
    course **livrée**, et on ne la note **qu'une fois** (lien un-à-un). La
    moyenne du livreur est recalculée dans la même transaction, sous verrou :
    deux clients notant deux courses du même livreur au même instant
    additionneraient sinon leurs lectures et en perdraient une.
    """

    @staticmethod
    @transaction.atomic
    def rate(
        *, assignment: Assignment, customer: User, score: int, comment: str = ""
    ) -> CourierRating:
        if assignment.status != DeliveryStatus.DELIVERED:
            raise BusinessRuleViolation(
                "Cette course n'est pas encore livrée : elle ne peut pas être notée.",
                assignment_status=assignment.status,
            )

        if CourierRating.objects.filter(assignment=assignment).exists():
            raise BusinessRuleViolation("Cette livraison a déjà été notée.")

        rating = CourierRating.objects.create(
            assignment=assignment, customer=customer, score=score, comment=comment
        )
        CourierRatingService._recompute_average(assignment.courier_id, score)
        return rating

    @staticmethod
    def _recompute_average(courier_id: UUID, score: int) -> None:
        """Moyenne incrémentale plutôt qu'un `Avg` sur toutes les notes.

        Le verrou sérialise les écritures concurrentes ; l'incrément évite de
        relire l'historique complet à chaque note, qui grossit sans borne. Le
        champ est un `DecimalField(3, 2)` : sans arrondi explicite, la division
        rendrait plus de décimales que la colonne n'en accepte.
        """
        courier = CourierProfile.objects.select_for_update().get(pk=courier_id)

        total = courier.rating_average * courier.rating_count + score
        courier.rating_count += 1
        courier.rating_average = (total / courier.rating_count).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        courier.save(update_fields=["rating_average", "rating_count", "updated_at"])
