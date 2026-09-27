"""Flotte et courses.

Trois invariants prouvés de la Phase 1 sont défendus ici :

* **L1** — seul un dossier `approved` peut accepter une course. Le statut de
  vérification est sur le profil, et le service le relit ; il n'est jamais
  déduit d'un jeton ni d'un champ client.
* **L2** — l'acceptation est exclusive. La contrainte d'unicité sur la course
  active d'une commande la rend impossible à violer même si deux requêtes
  concurrentes passent la garde applicative.
* **L4** — les compteurs ne sont incrémentés qu'à la transition vers
  `delivered`, qui est terminale : le graphe rend le rejeu inexprimable.
"""

from __future__ import annotations

from typing import ClassVar

from django.contrib.gis.db import models as gis
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.accounts.models import User
from apps.delivery.states import (
    DELIVERY_MACHINE,
    ENGAGED_STATUSES,
    TERMINAL_STATUSES,
    VERIFICATION_MACHINE,
    DeliveryStatus,
    VerificationStatus,
)
from apps.geography.models import DeliveryZone
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel, state_check_constraint
from common.storage import courier_documents

__all__ = [
    "Assignment",
    "CourierProfile",
    "CourierRating",
    "CourierShift",
    "DocumentReminder",
    "VehicleType",
]


class VehicleType(models.TextChoices):
    MOTORCYCLE = "motorcycle", "Moto"
    BICYCLE = "bicycle", "Vélo"
    CAR = "car", "Voiture"
    SCOOTER = "scooter", "Scooter"


class CourierProfile(UUIDModel, TimeStampedModel):
    """Dossier livreur : pièces, validation, disponibilité, position.

    Séparé de `User` parce que la vérification, les statistiques et la position
    ne concernent qu'une population, et que la position est réécrite toutes les
    dix secondes — ce qu'on ne veut pas faire sur la table d'authentification.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="courier_profile")
    restaurant = models.ForeignKey(Restaurant, on_delete=models.PROTECT, related_name="couriers")

    # Zones où ce livreur accepte de rouler, **à l'intérieur** de la desserte de
    # sa cuisine.
    #
    # **Vide veut dire : toutes les zones de sa cuisine**, et c'est le cas
    # courant — le seul qui existait avant ce champ, si bien qu'aucun livreur
    # en poste ne perd une course à son ajout. Renseigné, il restreint : un
    # livreur affecté à Cocody et Riviera ne reçoit pas une course pour
    # Yopougon, même partie de la même cuisine.
    #
    # Le rattachement à la cuisine reste la frontière dure (L1, cloisonnement du
    # personnel) ; la zone l'affine, elle ne l'élargit jamais — une zone d'une
    # autre ville est refusée à la saisie (`CourierService.set_service_zones`).
    service_zones = models.ManyToManyField(
        DeliveryZone,
        blank=True,
        related_name="couriers",
        help_text="Vide : toutes les zones desservies par sa cuisine.",
    )

    # --- Dossier ---------------------------------------------------------
    verification_status = models.CharField(
        max_length=16,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
        db_index=True,
    )
    verification_notes = models.TextField(blank=True)
    verified_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    national_id_number = models.CharField(max_length=64, blank=True)
    licence_number = models.CharField(max_length=64, blank=True)
    vehicle_type = models.CharField(max_length=16, choices=VehicleType.choices)
    vehicle_plate = models.CharField(max_length=32, blank=True)

    # Pièces justificatives. **Dossier privé** : déposées en `type=private`,
    # que Cloudinary ne sert jamais en accès anonyme, et chaque URL est émise
    # par le serveur pour une durée bornée (`CLOUDINARY_SIGNED_URL_EXPIRE`,
    # ADR-011). Ces documents ont vécu dans un espace public dans
    # l'implémentation précédente — une pièce d'identité y était lisible
    # indéfiniment par qui connaissait l'adresse.
    id_document = models.FileField(
        upload_to="couriers/id/", storage=courier_documents, null=True, blank=True
    )
    licence_document = models.FileField(
        upload_to="couriers/licence/", storage=courier_documents, null=True, blank=True
    )
    vehicle_document = models.FileField(
        upload_to="couriers/vehicle/", storage=courier_documents, null=True, blank=True
    )

    # Date d'expiration de chaque pièce — relevée par le personnel à la
    # validation, quand il a la pièce sous les yeux (`CourierService.review`),
    # et remise à nul quand le livreur dépose une nouvelle pièce, qui porte sa
    # propre date (`replace_documents`).
    #
    # Le cahier des charges demande ce suivi (§4.2.5). Un permis expiré ne rend
    # pas le livreur inéligible de lui-même : L1 ne dépend que du dossier, et
    # une règle qui le retirerait du service à minuit, en pleine tournée,
    # serait pire que le mal. L'équipe est prévenue avant l'échéance
    # (`remind_document_expiry`) et suspend si elle le décide.
    id_document_expires_on = models.DateField(null=True, blank=True)
    licence_document_expires_on = models.DateField(null=True, blank=True)
    vehicle_document_expires_on = models.DateField(null=True, blank=True)

    # --- Disponibilité et position ---------------------------------------
    is_online = models.BooleanField(
        default=False, help_text="Bascule volontaire du livreur : accepte-t-il des courses ?"
    )
    last_location = gis.PointField(geography=True, srid=4326, null=True, blank=True)
    last_location_at = models.DateTimeField(null=True, blank=True)

    # --- Statistiques (L4) ------------------------------------------------
    deliveries_completed = models.PositiveIntegerField(default=0)
    deliveries_cancelled = models.PositiveIntegerField(default=0)
    rating_average = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    rating_count = models.PositiveIntegerField(default=0)
    total_earnings = MoneyField(null=True)

    class Meta:
        verbose_name = "profil livreur"
        verbose_name_plural = "profils livreurs"
        constraints = [
            state_check_constraint(
                VERIFICATION_MACHINE, "verification_status", "courier_verification_in_enum"
            ),
        ]
        indexes = [
            # La requête d'affectation : les livreurs joignables d'un
            # restaurant. Le filtre géographique s'applique ensuite, sur un
            # ensemble déjà réduit.
            models.Index(fields=["restaurant", "is_online", "verification_status"]),
            gis.Index(fields=["last_location"]),
        ]

    def __str__(self) -> str:
        return f"{self.user.full_name} ({self.get_vehicle_type_display()})"

    def serves_zone(self, zone_id: object) -> bool:
        """Ce livreur roule-t-il dans cette zone ?

        Sans restriction, oui partout. Restreint, seulement dans ses zones — et
        **jamais** pour une commande dont la zone est inconnue (antérieure à la
        géographie figée) : on ne l'envoie pas là où l'on ne sait pas qu'il
        accepte d'aller.
        """
        zones = {zone.pk for zone in self.service_zones.all()}
        if not zones:
            return True
        return zone_id is not None and zone_id in zones

    @property
    def can_accept_orders(self) -> bool:
        """L1 — la seule porte d'entrée de l'éligibilité.

        Un livreur hors ligne ou dont le dossier n'est pas validé ne prend
        aucune course. Exposé en propriété pour qu'aucun appelant ne
        recompose la condition à sa façon, en en oubliant un terme.
        """
        return (
            self.is_online
            and self.verification_status == VerificationStatus.APPROVED
            and self.user.is_active
        )


class Assignment(UUIDModel, TimeStampedModel):
    """Course : l'attribution d'une commande à un livreur.

    Plusieurs affectations peuvent exister pour une même commande — une course
    proposée puis refusée, une autre proposée ensuite. Une seule peut être
    **active** à la fois, ce que garantit un index unique partiel.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="assignments")
    courier = models.ForeignKey(
        CourierProfile, on_delete=models.PROTECT, related_name="assignments"
    )

    status = models.CharField(
        max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.OFFERED
    )

    offered_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    # Rémunération figée à l'acceptation : le barème peut changer, ce qui est
    # dû au livreur pour cette course ne change pas.
    courier_fee = MoneyField(null=True)

    decline_reason = models.TextField(blank=True)
    # Privé également : une preuve de livraison montre une porte, une adresse,
    # parfois la personne qui a reçu la commande. Elle sert à trancher un
    # litige, pas à être vue.
    proof_of_delivery = models.ImageField(
        upload_to="deliveries/", storage=courier_documents, null=True, blank=True
    )

    class Meta:
        verbose_name = "course"
        ordering = ["-offered_at"]
        constraints = [
            state_check_constraint(DELIVERY_MACHINE, "status", "assignment_status_in_enum"),
            # L2 — une seule course active par commande.
            #
            # La garde applicative (SELECT FOR UPDATE dans le service) traite le
            # cas courant ; cette contrainte traite le cas où elle est
            # contournée — un script d'exploitation, un correctif à chaud, un
            # bug futur. Deux livreurs acceptant simultanément : le second se
            # heurte à la base, pas à un état incohérent.
            models.UniqueConstraint(
                fields=["order"],
                condition=~models.Q(status__in=TERMINAL_STATUSES),
                name="one_active_assignment_per_order",
            ),
            # L6 — une seule course **engagée** par livreur.
            #
            # Symétrique de la précédente, et elle manquait. L'unicité par
            # commande empêche deux livreurs de porter le même repas ; elle ne
            # dit rien du cas inverse — un livreur qui en porte deux. Rien ne
            # l'interdisait, et `available_for` proposait sans broncher un
            # livreur déjà en route.
            #
            # Ce que cela cassait est plus loin que la base : `Dely` n'émet ses
            # relevés de position que pour **une** course (`activeCourse`), et
            # son écran de navigation ne guide que vers celle-là. Le client de la
            # seconde commande suivait donc un livreur immobile, avec une
            # estimation d'arrivée qui n'avançait plus.
            #
            # `offered` est hors du champ, et c'est le point délicat : recevoir
            # plusieurs propositions n'occupe personne, et le livreur doit
            # pouvoir choisir. C'est l'acceptation qui engage, et elle seule.
            models.UniqueConstraint(
                fields=["courier"],
                condition=models.Q(status__in=ENGAGED_STATUSES),
                name="one_engaged_assignment_per_courier",
            ),
        ]
        indexes = [
            models.Index(fields=["courier", "-offered_at"]),
            models.Index(fields=["order", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.order.reference} → {self.courier.user.full_name}"

    @property
    def is_active(self) -> bool:
        return self.status in {
            DeliveryStatus.OFFERED,
            DeliveryStatus.ACCEPTED,
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.ON_THE_WAY,
        }


class CourierRating(UUIDModel, TimeStampedModel):
    """Note laissée par le client sur une course livrée.

    Rattachée à la **course** et non au couple (commande, livreur) : c'est la
    course qui dit qui a livré quoi, et le lien un-à-un interdit en base de
    noter deux fois la même livraison. Une commande relivrée après incident
    aurait une autre course, donc une autre note — ce qui est le comportement
    voulu.

    `customer` est stocké alors qu'il se déduit de `assignment.order.customer` :
    la note survit à ce chemin (une commande peut être réattribuée, l'auteur de
    la note ne change pas) et l'index rend directe la question « ce client
    a-t-il noté ? ».
    """

    assignment = models.OneToOneField(Assignment, on_delete=models.CASCADE, related_name="rating")
    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="courier_ratings")
    score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True)

    class Meta:
        verbose_name = "note de livreur"
        verbose_name_plural = "notes de livreurs"
        ordering = ["-created_at"]
        constraints = [
            # Le validateur ci-dessus ne s'applique qu'aux formulaires et
            # sérialiseurs ; la contrainte s'applique à tout le monde, y
            # compris à un script d'exploitation.
            models.CheckConstraint(
                condition=models.Q(score__gte=1) & models.Q(score__lte=5),
                name="courier_rating_score_range",
            ),
        ]
        indexes = [models.Index(fields=["customer", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.score}/5 — {self.assignment.courier.user.full_name}"


class CourierShift(UUIDModel, TimeStampedModel):
    """Créneau planifié d'un livreur — jour de la semaine, heure de début, de fin.

    **Indicatif, et non opposable.** L'éligibilité d'un livreur reste
    `can_accept_orders` (L1) : en ligne, dossier validé, compte actif. Un
    créneau ne s'y ajoute pas, et c'est délibéré — un livreur présent, en ligne,
    à qui le serveur refuserait une course parce qu'il est 18 h 05 alors que son
    créneau finissait à 18 h, verrait un refus qu'aucun écran ne sait expliquer,
    et la commande resterait sans porteur. Le planning sert à l'exploitation :
    savoir qui elle attend, et constater les écarts.

    Le jour est un entier ISO — 1 lundi, 7 dimanche — plutôt qu'un nom : il se
    trie, il s'indexe, et il ne dépend pas de la langue de l'interface.

    Les heures sont **locales à l'établissement** et non horodatées : « le mardi
    de 9 h à 17 h » se répète, et l'exprimer en instants UTC obligerait à
    régénérer les lignes chaque semaine, avec un décalage à chaque changement
    d'heure.
    """

    courier = models.ForeignKey(CourierProfile, on_delete=models.CASCADE, related_name="shifts")
    day_of_week = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        help_text="Jour ISO : 1 = lundi, 7 = dimanche.",
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_available = models.BooleanField(
        default=True,
        help_text="Décoché plutôt que supprimé : une absence ponctuelle se lit dans le planning.",
    )

    class Meta:
        verbose_name = "créneau de livreur"
        verbose_name_plural = "créneaux de livreurs"
        ordering = ["day_of_week", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(day_of_week__gte=1) & models.Q(day_of_week__lte=7),
                name="courier_shift_day_range",
            ),
            # Un créneau qui finit avant de commencer ne se lit pas : il
            # afficherait une barre de longueur négative dans le planning, ou
            # rien du tout. Pas de créneau à cheval sur minuit non plus — il
            # s'exprime en deux lignes, sur deux jours, ce qui reste juste.
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="courier_shift_window_ordered",
            ),
            # Deux créneaux identiques le même jour sont une double saisie, pas
            # une intention : ils s'afficheraient l'un sur l'autre.
            models.UniqueConstraint(
                fields=["courier", "day_of_week", "start_time"],
                name="courier_shift_unique_start",
            ),
        ]
        indexes = [models.Index(fields=["courier", "day_of_week"])]

    def __str__(self) -> str:
        return (
            f"{self.courier.user.full_name} — J{self.day_of_week} {self.start_time}–{self.end_time}"
        )


class DocumentReminder(UUIDModel):
    """Le rappel d'expiration **déjà envoyé** — la mémoire de la tâche quotidienne.

    ## Pourquoi une table plutôt qu'un test de date

    Le rappel partait quand une pièce expirait *exactement* dans 30, 7, 1 ou 0
    jours. Cette écriture a deux défauts qui se voient seulement en
    exploitation :

    * **le rejeu double les envois.** Une tâche relancée à la main, un
      redémarrage de `beat`, un `retry` du worker : la même journée est
      repassée, et le livreur reçoit deux fois le même avis ;
    * **une journée manquée est perdue.** `beat` écrit son dernier passage dans
      un fichier que le redéploiement efface (`/tmp` chez Render) et une entrée
      planifiée à l'intervalle repart de zéro à chaque démarrage. Sur un projet
      qui déploie plusieurs fois par jour, la tâche quotidienne **ne s'exécutait
      jamais** — et même exécutée, un jour d'arrêt effaçait définitivement le
      rappel de ce jour-là, qui ne reviendrait qu'à l'échéance suivante.

    Une ligne par rappel effectivement émis ferme les deux : la tâche peut
    tourner cent fois par jour sans rien redire, et raisonner en « seuil
    atteint » plutôt qu'en « jour exact » — ce qu'elle ne peut faire que si
    elle sait ce qu'elle a déjà dit.

    L'unicité porte sur la **date d'échéance**, pas sur la date d'envoi : une
    pièce renouvelée porte une nouvelle échéance, donc ses propres rappels.
    """

    courier = models.ForeignKey(
        CourierProfile, on_delete=models.CASCADE, related_name="document_reminders"
    )
    #: Le champ de la pièce — `id_document`, `licence_document`, `vehicle_document`.
    piece = models.CharField(max_length=32)
    expires_on = models.DateField()
    #: Le seuil atteint : 30, 7, 1 ou 0 jour avant l'échéance.
    threshold_days = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "rappel d'expiration envoyé"
        verbose_name_plural = "rappels d'expiration envoyés"
        ordering: ClassVar[list[str]] = ["-sent_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # L'idempotence est **en base** et non dans la tâche : deux
            # exécutions concurrentes — un `beat` en double après un
            # redéploiement — liraient toutes deux « pas encore envoyé ».
            models.UniqueConstraint(
                fields=["courier", "piece", "expires_on", "threshold_days"],
                name="document_reminder_once",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["courier", "expires_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.piece} J-{self.threshold_days} — {self.expires_on}"
