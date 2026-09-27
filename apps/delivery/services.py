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

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import ClassVar
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Sum
from django.utils import timezone

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, CourierRating
from apps.delivery.signals import (
    assignment_accepted,
    assignment_cancelled,
    assignment_declined,
    assignment_offered,
    courier_went_online,
    verification_decided,
)
from apps.delivery.states import (
    DELIVERY_MACHINE,
    ENGAGED_STATUSES,
    ORDER_STATUS_PROJECTION,
    TERMINAL_STATUSES,
    VERIFICATION_MACHINE,
    DeliveryStatus,
    VerificationStatus,
)
from apps.geography.models import DeliveryZone
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.restaurants.models import Restaurant
from common.audit import AuditAction, record_change
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.realtime import courier_group, order_group, publish

#: Étapes où une preuve de livraison se dépose : le repas est en route, ou
#: remis. Avant l'enlèvement, il n'y a rien à prouver.
PROOF_STATUSES: frozenset[str] = frozenset({DeliveryStatus.ON_THE_WAY, DeliveryStatus.DELIVERED})

logger = logging.getLogger(__name__)

__all__ = [
    "AssignmentService",
    "CourierApplication",
    "CourierRatingService",
    "CourierService",
    "courier_fee_for",
]

#: Statuts de commande depuis lesquels une course peut être proposée.
#:
#: **Uniquement `ready`** : une course ne se propose que lorsque le repas peut
#: réellement être retiré. Les deux étapes antérieures — `confirmed`,
#: `preparing` — y figuraient, et c'est ce qui rendait possible le défaut que
#: `_exiger_une_commande_qui_suit` ferme : un livreur acceptait pendant la
#: préparation, puis déclarait « récupérée » un repas encore en cuisine.
#:
#: La contrepartie est assumée : on ne pré-affecte plus un livreur pendant la
#: cuisson. C'est déjà le fonctionnement de l'affectation automatique, qui
#: déclenche sur `ready` (`DispatchService.dispatch_on_ready`) ; seul le
#: back-office pouvait proposer plus tôt. Rouvrir la pré-affectation demande de
#: réintroduire les deux statuts **et** de garder la garde de `transition_to`,
#: qui reste la règle de fond.
OFFERABLE_FROM = frozenset({OrderStatus.READY})

#: Ce qu'on dit au livreur quand la commande ne peut pas suivre son geste.
#:
#: Une phrase par étape, écrite pour la personne qui la lira sur un téléphone,
#: au restaurant ou devant une porte — pas un code d'erreur. Le statut de la
#: commande voyage à côté, dans les membres de l'erreur RFC 9457.
_REFUS_DE_PROJECTION: dict[str, str] = {
    DeliveryStatus.PICKED_UP: (
        "La cuisine n'a pas encore déclaré cette commande prête : elle ne peut pas être récupérée."
    ),
    DeliveryStatus.ON_THE_WAY: (
        "Cette commande n'est pas enregistrée comme récupérée : le départ ne peut pas être déclaré."
    ),
    DeliveryStatus.DELIVERED: (
        "Cette commande n'est pas enregistrée comme partie en livraison : la livraison "
        "ne peut pas être déclarée."
    ),
}


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
        expirations: dict[str, date] | None = None,
    ) -> CourierProfile:
        """Fait avancer le dossier — validation, rejet, suspension.

        La machine du dossier est la seule **cyclique** du projet : un dossier
        se ré-instruit, alors qu'une course ne se re-livre pas. Le passage par
        `VERIFICATION_MACHINE` garantit qu'on ne saute pas d'étape pour autant
        — on ne suspend pas un dossier jamais validé.
        """
        locked = CourierProfile.objects.select_for_update().get(pk=courier.pk)

        # Les dates d'expiration se relèvent au moment où quelqu'un a la pièce
        # sous les yeux — donc ici. Elles s'écrivent même si le statut ne
        # change pas : compléter la date d'un dossier déjà validé est un geste
        # ordinaire, et le refuser au motif du « rien à faire » la rendrait
        # impossible à saisir après coup.
        champs_dates = CourierService._poser_expirations(locked, expirations or {})
        if target == VerificationStatus.APPROVED:
            CourierService._refuser_si_expire(locked)

        if VERIFICATION_MACHINE.is_noop(locked.verification_status, target):
            if champs_dates:
                locked.save(update_fields=[*champs_dates, "updated_at"])
            return locked

        VERIFICATION_MACHINE.validate(locked.verification_status, target)

        # Après la légalité de la transition, pour qu'un geste impossible se dise
        # impossible plutôt qu'incomplet. Le livreur lit ce motif dans son
        # profil (`verification_notes`) : un refus ou une suspension sans raison
        # le laisse sans rien à corriger, et c'est la personne qui décide qui
        # sait ce qui ne va pas.
        if target in CourierService.DECISIONS_MOTIVEES and not notes.strip():
            raise DjangoValidationError(
                {"notes": ["Dites au livreur pourquoi : il lira ce motif dans son application."]}
            )

        precedent = locked.verification_status
        locked.verification_status = target
        locked.verification_notes = notes.strip()
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
                *champs_dates,
                "updated_at",
            ]
        )
        record_change(
            actor=actor,
            action=AuditAction.COURIER_VERIFICATION,
            target_type="courier",
            target_id=locked.pk,
            target_label=locked.user.full_name,
            before={"status": precedent},
            after={"status": target, "notes": locked.verification_notes},
            scope_restaurant_id=locked.restaurant_id,
        )
        verification_decided.send(sender=CourierProfile, courier=locked, previous_status=precedent)
        return locked

    #: Les décisions qui exigent un motif : celles qui retirent quelque chose au
    #: livreur. Valider ou rouvrir un dossier n'ont rien à expliquer.
    DECISIONS_MOTIVEES: ClassVar[frozenset[str]] = frozenset(
        {VerificationStatus.REJECTED, VerificationStatus.SUSPENDED}
    )

    #: Les pièces du dossier et le champ qui porte leur date d'expiration.
    PIECES_DATEES: ClassVar[dict[str, str]] = {
        "id_document": "id_document_expires_on",
        "licence_document": "licence_document_expires_on",
        "vehicle_document": "vehicle_document_expires_on",
    }

    #: Les libellés que lit un livreur, ou un opérateur.
    LIBELLES_PIECES: ClassVar[dict[str, str]] = {
        "id_document": "pièce d'identité",
        "licence_document": "permis de conduire",
        "vehicle_document": "carte grise",
    }

    @staticmethod
    def _poser_expirations(courier: CourierProfile, expirations: dict[str, date]) -> list[str]:
        """Écrit les dates reçues, et rend les champs touchés."""
        touches = []
        for champ, valeur in expirations.items():
            if champ not in CourierService.PIECES_DATEES.values():
                continue
            setattr(courier, champ, valeur)
            touches.append(champ)
        return touches

    @staticmethod
    def _refuser_si_expire(courier: CourierProfile) -> None:
        """On ne valide pas un dossier sur une pièce déjà expirée.

        Seule garde **dure** de l'expiration, et placée au seul moment où
        quelqu'un décide : valider, c'est affirmer que les pièces sont bonnes.
        Un dossier déjà validé dont une pièce expire ensuite ne bascule pas de
        lui-même — voir le commentaire du modèle.
        """
        aujourd_hui = timezone.localdate()
        for piece, champ in CourierService.PIECES_DATEES.items():
            echeance = getattr(courier, champ)
            if echeance is not None and echeance < aujourd_hui:
                raise BusinessRuleViolation(
                    f"La {CourierService.LIBELLES_PIECES[piece]} a expiré le "
                    f"{echeance:%d/%m/%Y} : demandez au livreur d'en déposer une nouvelle.",
                    piece=piece,
                    expires_on=echeance.isoformat(),
                )

    @staticmethod
    def earnings(*, courier: CourierProfile) -> dict[str, object]:
        """Gains du livreur, agrégés par période — **en base**.

        ## Pourquoi cette route existe

        L'écran des gains additionnait les courses que l'application avait en
        mémoire, c'est-à-dire au plus **soixante** : `recentlyDelivered` suit
        trois pages de vingt, et c'est délibéré — l'historique d'un livreur en
        poste depuis un an croît sans limite, et le charger entier pour afficher
        un total serait absurde.

        Mais l'écran en tirait « aujourd'hui », « cette semaine » et **« ce
        mois »**. Un livreur à dix courses par jour n'avait donc, dans son
        onglet mensuel, que ses six derniers jours — un total plus petit que la
        réalité, affiché sans la moindre mention de troncature. C'est le genre
        de chiffre qu'on ne met pas en doute : on compte sa paie dessus.

        Une somme se demande au serveur. Les trois périodes sont calculées ici,
        en une requête, sur la totalité des courses livrées.

        ## Le fuseau

        Les bornes sont posées dans le fuseau de **l'établissement**, pas en UTC :
        une course livrée à 23 h 30 à Lomé appartient à cette journée-là pour le
        livreur qui l'a faite, et non au lendemain parce que le serveur compte
        en temps universel.

        La somme porte sur `courier_fee`, figée à l'acceptation : c'est ce qui
        est dû, indépendamment des barèmes qui ont pu changer depuis.
        """
        fuseau = ZoneInfo(courier.restaurant.timezone)
        maintenant = timezone.now().astimezone(fuseau)
        aujourdhui = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)

        bornes = {
            "today": aujourdhui,
            # La semaine commence lundi — convention ISO, celle du planning des
            # livreurs (`CourierShift.day_of_week`).
            "week": aujourdhui - dt.timedelta(days=aujourdhui.weekday()),
            "month": aujourdhui.replace(day=1),
        }

        livrees = Assignment.objects.filter(
            courier=courier,
            status=DeliveryStatus.DELIVERED,
            delivered_at__isnull=False,
        )

        devise = courier.restaurant.currency
        periodes: dict[str, object] = {}
        for nom, depuis in bornes.items():
            agrege = livrees.filter(delivered_at__gte=depuis).aggregate(
                total=Sum("courier_fee_minor"), nombre=Count("id")
            )
            periodes[nom] = {
                "earned": Money(agrege["total"] or 0, devise),
                "deliveries": agrege["nombre"],
            }

        # Le cumul de carrière vient du dossier, qui le tient à jour à chaque
        # livraison (`_credit`) : le recalculer ici ferait deux sources pour un
        # même chiffre, et c'est ainsi qu'elles divergent.
        periodes["lifetime"] = {
            "earned": courier.total_earnings or Money.zero(devise),
            "deliveries": courier.deliveries_completed,
        }
        return periodes

    @staticmethod
    def set_online(*, courier: CourierProfile, is_online: bool) -> CourierProfile:
        """Bascule de disponibilité, à l'initiative du livreur.

        `is_online` est une **déclaration** : le livreur seul sait s'il roule.
        Elle est donc toujours acceptée, y compris sur un dossier en attente.
        Ce qu'elle ne donne pas, c'est l'éligibilité : celle-ci est
        `can_accept_orders` (L1), qui exige en plus un dossier validé et un
        compte ouvert, et qui est relue à chaque proposition de course
        (`AssignmentService.offer`) comme au tri des livreurs disponibles
        (`available_for`). Accepter la bascule ne desserre donc aucune garde.

        Ce fut un refus (409), et le refus se retournait contre le livreur : sa
        disponibilité déclarée était **perdue**, si bien qu'au moment où son
        dossier était validé il restait hors ligne sans le savoir, et devait
        rebasculer l'interrupteur pour exister. Accepter la déclaration fait
        qu'une validation le rend disponible à l'instant où elle est prononcée.

        L'explicite n'est pas sacrifié pour autant : la réponse porte
        `can_accept_orders: false`, et l'application le dit — « vous êtes en
        ligne, mais votre dossier n'est pas validé ».
        """
        passe_en_ligne = is_online and not courier.is_online
        courier.is_online = is_online
        courier.save(update_fields=["is_online", "updated_at"])
        if passe_en_ligne:
            courier_went_online.send(sender=CourierProfile, courier=courier)
        return courier

    @staticmethod
    @transaction.atomic
    def set_service_zones(
        *, courier: CourierProfile, zones: Iterable[DeliveryZone]
    ) -> CourierProfile:
        """Affecte le livreur à des zones — **à l'intérieur** de la desserte de sa cuisine.

        Une zone est acceptée si elle peut porter une course de sa cuisine :
        municipale **de la ville de la cuisine**, ou propre **à cette cuisine**.
        Une seule zone hors de ce périmètre fait tout refuser, sans rien écrire :
        affecter un livreur de Lomé à une zone de Kara ne ferait que le rendre
        inéligible partout, en silence.

        Une liste vide lève la restriction — le livreur roule dans toutes les
        zones de sa cuisine.
        """
        cuisine = courier.restaurant
        ville = cuisine.zone.city_id
        retenues = list(zones)
        hors_perimetre = sorted(
            zone.name
            for zone in retenues
            if not (
                zone.restaurant_id == cuisine.pk
                or (zone.restaurant_id is None and zone.city_id == ville)
            )
        )
        if hors_perimetre:
            raise BusinessRuleViolation(
                f"Ces zones ne sont pas desservies par {cuisine.name} : "
                f"{', '.join(hors_perimetre)}.",
                zones=hors_perimetre,
            )
        courier.service_zones.set(retenues)
        logger.info(
            "delivery.courier.zones",
            extra={
                "courier": str(courier.pk),
                "kitchen": cuisine.slug,
                "zones": [zone.name for zone in retenues],
            },
        )
        return courier

    #: Statuts qu'un dépôt de pièces ramène à `pending` — et pourquoi ces deux-là.
    #:
    #: `approved` y est depuis l'origine : un dossier validé sur des pièces
    #: qu'on a ensuite remplacées n'est plus un dossier validé, et laisser
    #: l'approbation en place reviendrait à valider des documents que personne
    #: n'a lus. C'est une règle de conformité (L5).
    #:
    #: `rejected` **manquait**, et son absence fermait la seule issue d'un
    #: dossier refusé. La machine l'autorise pourtant explicitement
    #: (`REJECTED → PENDING`), l'écran du livreur le lui promet — « déposez de
    #: nouvelles pièces pour que votre dossier soit réexaminé » — et le
    #: back-office ne liste comme « à instruire » que les dossiers `pending`.
    #: Un livreur refusé pour une photo illisible pouvait donc en redéposer
    #: dix : elles arrivaient bien en base, son dossier restait `rejected`, et
    #: personne ne les regardait jamais.
    #:
    #: `suspended` n'y est **pas**, et c'est délibéré : une suspension est une
    #: sanction d'exploitation, pas un défaut de pièce. S'en relever en
    #: téléversant une carte grise ferait de la sanction une formalité — et la
    #: machine refuse d'ailleurs `SUSPENDED → PENDING`.
    ROUVRE_L_INSTRUCTION = (VerificationStatus.APPROVED, VerificationStatus.REJECTED)

    @staticmethod
    @transaction.atomic
    def replace_documents(*, courier: CourierProfile, **documents: object) -> CourierProfile:
        """Remplace des pièces justificatives — **et rouvre l'instruction** (L5).

        Voir [`ROUVRE_L_INSTRUCTION`] pour les statuts concernés et le motif de
        chacun. Le livreur repasse hors ligne du même geste : son dossier
        n'étant plus validé, `can_accept_orders` est faux de toute façon, et le
        laisser « en ligne » le maintiendrait dans les listes d'affectation où
        seule cette garde-là l'écarterait.
        """
        for field, value in documents.items():
            setattr(courier, field, value)

        # Une nouvelle pièce porte sa propre date : garder celle de l'ancienne
        # ferait croire à un document valide jusqu'à une échéance qui n'est
        # pas la sienne.
        dates = [
            CourierService.PIECES_DATEES[field]
            for field in documents
            if field in CourierService.PIECES_DATEES
        ]
        for champ in dates:
            setattr(courier, champ, None)

        touched = [*documents, *dates]
        if courier.verification_status in CourierService.ROUVRE_L_INSTRUCTION:
            courier.verification_status = VerificationStatus.PENDING
            courier.is_online = False
            # Le motif du refus précédent porte sur des pièces qui ne sont plus
            # là. Le garder ferait lire au livreur, sur un dossier qu'il vient
            # de corriger, le reproche auquel il vient de répondre.
            courier.verification_notes = ""
            touched += ["verification_status", "is_online", "verification_notes"]

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

        Un livreur **déjà engagé** en est en revanche exclu (L6). Il ne l'était
        pas, et le back-office proposait donc en toute confiance quelqu'un qui
        roulait déjà vers un autre client — `StatutLivreur` n'ayant par ailleurs
        aucun état « en livraison » pour le dire au superviseur.

        Une proposition en attente n'exclut pas : elle n'occupe personne, et un
        livreur qui laisse traîner une offre bloquerait sinon sa propre file.
        """
        # Le périmètre de zone : un livreur sans zone roule partout où sa
        # cuisine livre ; restreint, seulement dans ses zones — et jamais pour
        # une commande dont la zone est inconnue (voir `serves_zone`, la même
        # règle en Python, que `offer` relit).
        liaisons = CourierProfile.service_zones.through.objects
        perimetre = Q(~Exists(liaisons.filter(courierprofile_id=OuterRef("pk"))))
        if order.delivery_zone_id is not None:
            perimetre |= Q(
                Exists(
                    liaisons.filter(
                        courierprofile_id=OuterRef("pk"), deliveryzone_id=order.delivery_zone_id
                    )
                )
            )
        return (
            CourierProfile.objects.filter(
                restaurant=order.restaurant,
                is_online=True,
                verification_status=VerificationStatus.APPROVED,
                user__is_active=True,
            )
            .filter(perimetre)
            .exclude(assignments__status__in=ENGAGED_STATUSES)
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
                "Une course ne se propose que lorsque le repas est prêt à être "
                "retiré : cette commande est encore en cuisine, ou déjà partie.",
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
        if not courier.serves_zone(locked.delivery_zone_id):
            zone = f" ({locked.delivery_zone_name})" if locked.delivery_zone_name else ""
            raise BusinessRuleViolation(
                f"Ce livreur n'est pas affecté à la zone de livraison de cette commande{zone}.",
                courier_id=str(courier.pk),
            )

        # L6 — le livreur ne porte qu'une course à la fois. Relu ici et pas
        # seulement à l'acceptation : proposer une course à quelqu'un qui roule
        # déjà, c'est faire attendre le repas jusqu'à ce qu'il refuse.
        engagee = AssignmentService._engaged_for(courier)
        if engagee is not None:
            raise BusinessRuleViolation(
                "Ce livreur porte déjà une course. Attendez qu'il l'ait livrée, "
                "ou confiez celle-ci à quelqu'un d'autre.",
                courier_id=str(courier.pk),
                assignment_id=str(engagee.pk),
                assignment_status=engagee.status,
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
        return order.assignments.exclude(status__in=TERMINAL_STATUSES).first()

    @staticmethod
    def _engaged_for(courier: CourierProfile) -> Assignment | None:
        """La course que ce livreur porte déjà, s'il en porte une (L6).

        Distincte de `_active_for`, qui répond pour une **commande**. La
        confusion entre les deux est exactement le défaut corrigé ici :
        `Dely` justifiait de ne suivre qu'une course en citant `_active_for`,
        qui ne dit rien du livreur.
        """
        return Assignment.objects.filter(courier=courier, status__in=ENGAGED_STATUSES).first()

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
        commande = Order.objects.select_for_update().get(pk=assignment.order_id)
        current = Assignment.objects.select_related("order").get(pk=assignment.pk)

        if current.courier_id != courier.pk:
            raise BusinessRuleViolation("Cette course est proposée à un autre livreur.")
        # La commande verrouillée ci-dessus n'était jamais lue : la garde
        # « commande annulée » ne vivait que dans `transition_to`, et un livreur
        # acceptait (200) la course d'un repas qui ne partirait jamais.
        if commande.status == OrderStatus.CANCELLED:
            raise BusinessRuleViolation(
                "La commande a été annulée ; cette course ne peut plus être acceptée.",
                order_status=commande.status,
            )
        if not courier.can_accept_orders:
            raise BusinessRuleViolation(
                "Votre dossier ne vous permet pas d'accepter une course.",
                verification_status=courier.verification_status,
            )

        # L6 — c'est ici que la règle se joue vraiment. Un livreur peut recevoir
        # plusieurs propositions ; il n'en accepte qu'une. Sans ce contrôle, il
        # lui suffisait d'appuyer deux fois, sur deux offres différentes, pour se
        # retrouver avec deux courses dont son application n'en suivrait qu'une.
        #
        # Le verrou posé plus haut porte sur la commande, pas sur le livreur : il
        # ne sérialise donc pas deux acceptations de commandes distinctes. C'est
        # la contrainte `one_engaged_assignment_per_courier` qui tient ce cas de
        # course, et ce contrôle qui lui évite de se manifester en 500.
        engagee = AssignmentService._engaged_for(courier)
        if engagee is not None:
            raise BusinessRuleViolation(
                "Vous portez déjà une course. Terminez-la avant d'en accepter une autre.",
                assignment_id=str(engagee.pk),
                assignment_status=engagee.status,
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

        # Après l'écriture, dans la transaction : un abonné qui écrit en base
        # doit le faire de façon atomique avec l'acceptation qui le déclenche.
        assignment_accepted.send(sender=Assignment, assignment=current)
        return current

    @staticmethod
    @transaction.atomic
    def attach_proof(
        *, assignment: Assignment, courier: CourierProfile, photo: UploadedFile
    ) -> Assignment:
        """Dépose la preuve de livraison — la photo prise à la remise.

        Une fois le repas parti (`on_the_way`) ou livré : avant l'enlèvement, il
        n'y a rien à prouver. La course livrée accepte encore une **première**
        preuve — le réseau manque souvent à la porte, et la photo part après
        « livré » —, mais plus son remplacement : c'est elle qu'on relira en
        cas de litige, et la laisser réécrire lui ôterait sa valeur.
        """
        locked = Assignment.objects.select_for_update().get(pk=assignment.pk)
        if locked.courier_id != courier.pk:
            raise BusinessRuleViolation("Cette course n'est pas la vôtre.")
        if locked.status not in PROOF_STATUSES:
            raise BusinessRuleViolation(
                "La preuve se dépose une fois le repas en route vers le client.",
                assignment_status=locked.status,
            )
        if locked.status == DeliveryStatus.DELIVERED and locked.proof_of_delivery:
            raise BusinessRuleViolation(
                "La preuve de cette livraison est déjà enregistrée.",
                assignment_status=locked.status,
            )

        locked.proof_of_delivery.save(f"{locked.pk}.jpg", photo, save=False)
        locked.save(update_fields=["proof_of_delivery", "updated_at"])
        return locked

    @staticmethod
    def _annoncer_l_annulation(course: Assignment, reason: str) -> None:
        """Dit au livreur, sur **sa** file, que cette course lui est retirée.

        La notification poussée le lui disait, et elle seule : la diffusion
        temps réel portait sur le canal de la commande, que l'application du
        livreur n'écoute pas. Application ouverte, il continuait donc de suivre
        un itinéraire vers une course annulée jusqu'au rechargement suivant.

        Après le commit, comme `delivery.offered` : une annulation que la
        transaction rejette ne doit pas lui faire quitter sa course.
        """
        transaction.on_commit(
            partial(
                publish,
                courier_group(course.courier_id),
                "delivery.cancelled",
                {"assignment": str(course.pk), "order": str(course.order_id), "reason": reason},
            )
        )

    @staticmethod
    def close_for_cancelled_order(*, order: Order, reason: str) -> list[Assignment]:
        """Referme les courses encore ouvertes d'une commande qu'on vient d'annuler.

        Appelée par le récepteur de `order_status_changed`, **dans** la
        transaction de l'annulation : la commande et sa course basculent
        ensemble, ou pas du tout. Sans elle, la course restait « proposée »,
        visible et acceptable par le livreur.

        Pas `transition_to` : celui-ci compte l'annulation contre le livreur
        (`deliveries_cancelled`, qui mesure sa fiabilité), alors qu'il n'y est
        pour rien. Seules `offered` et `accepted` sont concernées — une commande
        ne s'annule plus une fois le repas enlevé, et une course close le reste.
        """
        fermees: list[Assignment] = []
        ouvertes = (
            Assignment.objects.select_for_update()
            .select_related("courier__user")
            .filter(order=order, status__in=(DeliveryStatus.OFFERED, DeliveryStatus.ACCEPTED))
        )
        for course in ouvertes:
            DELIVERY_MACHINE.validate(course.status, DeliveryStatus.CANCELLED)
            course.status = DeliveryStatus.CANCELLED
            course.decline_reason = reason
            course.save(update_fields=["status", "decline_reason", "updated_at"])

            charge = {
                "assignment": str(course.pk),
                "order": str(course.order_id),
                "status": DeliveryStatus.CANCELLED,
                "courier": course.courier.user.full_name,
            }
            transaction.on_commit(
                partial(publish, order_group(order.pk), "delivery.status", charge)
            )
            # Le livreur l'apprend par son propre canal — même signal que
            # l'annulation d'une course, que `notifications` relaie déjà.
            AssignmentService._annoncer_l_annulation(course, reason)
            assignment_cancelled.send(sender=Assignment, assignment=course, reason=reason)
            fermees.append(course)
        return fermees

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
        assignment_declined.send(sender=Assignment, assignment=assignment)
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

        # Une étape qui se projette sur la commande ne s'applique que si la
        # commande peut réellement la recevoir. **Avant** toute écriture : une
        # course qui avance sur une commande qui ne suit pas est le défaut que
        # cette garde ferme (voir `_exiger_une_commande_qui_suit`).
        AssignmentService._exiger_une_commande_qui_suit(locked, target)

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
            # Le livreur doit l'apprendre de nous, pas de la cuisine en
            # arrivant. La diffusion faite plus haut porte sur le canal de la
            # **commande**, que le client écoute et que le livreur n'écoute
            # pas : sans ce signal, il roulait vers une course annulée.
            AssignmentService._annoncer_l_annulation(locked, reason)
            assignment_cancelled.send(sender=Assignment, assignment=locked, reason=reason)

        return locked

    @staticmethod
    def _exiger_une_commande_qui_suit(assignment: Assignment, target: str) -> None:
        """Refuse une étape de course que la commande ne peut pas suivre.

        ## Le défaut que cette garde ferme

        `allowed_transitions` est calculé sur la seule machine de la **course**
        (`AssignmentSerializer.get_allowed_transitions`) : le bouton « J'ai
        récupéré la commande » s'affichait donc dès l'acceptation, quelle que
        soit l'avancée de la cuisine. Le serveur l'acceptait, puis `_project`
        constatait que `ready → picked_up` n'était pas jouable depuis
        `preparing` et **retournait en silence**.

        La course poursuivait alors sa vie — récupérée, en route, livrée,
        livreur crédité — pendant que la commande restait « en préparation ».
        Le client ne voyait jamais sa livraison, ses points de fidélité
        n'étaient pas crédités (ils le sont à la livraison de la *commande*),
        et le personnel pouvait encore annuler un repas déjà remis.

        Deux règles ici, et une seule phrase pour les dire au livreur :

        * l'étape est refusée si la commande ne peut pas la recevoir ;
        * elle est acceptée si la commande y est **déjà** — un rejeu ne doit
          pas bloquer une course dont la commande a été menée à la main.
        """
        projected = ORDER_STATUS_PROJECTION.get(target)
        if projected is None:
            # `offered`, `accepted`, `declined`, `cancelled` : rien à projeter.
            return

        order = assignment.order
        if order.status == projected or ORDER_MACHINE.can(order.status, projected):
            return

        raise BusinessRuleViolation(
            _REFUS_DE_PROJECTION[target],
            order_status=order.status,
            assignment_status=assignment.status,
            expected_order_status=projected,
        )

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
        if assignment.order.status == projected:
            # La commande y est déjà — le personnel l'y a menée à la main. Il
            # n'y a rien à projeter, et rien à refuser non plus.
            return

        # Plus de repli silencieux ici : la faisabilité a été vérifiée avant
        # toute écriture par `_exiger_une_commande_qui_suit`. Si la transition
        # échouait malgré tout, `InvalidTransition` remonterait et ferait
        # annuler la transaction entière — course comprise. C'est le
        # comportement voulu : **une course ne se termine pas sans sa
        # commande**. L'ancien `return` muet est précisément ce qui laissait
        # une course livrée sur une commande restée en préparation.
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
