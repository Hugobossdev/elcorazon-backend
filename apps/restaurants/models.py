"""Établissements — ADR-006.

`Restaurant` est le point de rattachement du multi-site : le catalogue, les
commandes et la flotte portent tous une clé de restaurant **non nulle** dès le
premier jour. C'est cette colonne, présente dès l'origine, qui rendra
l'ouverture d'un second établissement indolore.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.gis.db import models as gis
from django.db import models

from apps.accounts.models import User
from apps.geography.models import DeliveryZone
from common.models import TimeStampedModel, UUIDModel
from common.storage import banners

__all__ = ["OpeningHours", "Restaurant", "StaffMembership", "Weekday"]


class Restaurant(UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)

    zone = models.ForeignKey(DeliveryZone, on_delete=models.PROTECT, related_name="restaurants")
    address = models.TextField()
    location = gis.PointField(
        geography=True,
        srid=4326,
        help_text="Point de retrait des courses ; origine du calcul de distance.",
    )

    phone = models.CharField(max_length=16)
    email = models.EmailField(blank=True)
    # Compartiment public, avec les bannières et visuels de campagne : c'est
    # l'image d'accueil de l'établissement, faite pour être vue (ADR-011).
    cover_image = models.ImageField(
        upload_to="restaurants/", storage=banners, null=True, blank=True
    )

    # `is_active` est structurel — l'établissement existe-t-il ? —, tandis que
    # `accepts_orders` est conjoncturel : un coup de feu en cuisine, une panne
    # de four. Les confondre obligerait à désactiver un restaurant pour arrêter
    # les commandes une heure, ce qui le ferait disparaître de l'application.
    is_active = models.BooleanField(default=True)
    accepts_orders = models.BooleanField(default=True)

    default_preparation_minutes = models.PositiveSmallIntegerField(default=20)

    # Vue « des deux côtés » du rattachement, à travers `StaffMembership` — donc
    # sans table supplémentaire ni migration de schéma. Elle n'existe que pour
    # le sens de lecture qui manquait : `user.restaurants` répond à « où
    # travaille cette personne ? », question que le back-office pose sur chaque
    # fiche de personnel. L'écrire à l'envers, en partant des rattachements,
    # obligeait chaque appelant à recomposer la même liste.
    #
    # Elle vit ici et non sur `User` parce que `accounts` ne connaît pas les
    # établissements (ADR-002) ; `related_name` rend l'accès disponible dans le
    # bon sens sans inverser le graphe.
    staff = models.ManyToManyField(
        User,
        through="restaurants.StaffMembership",
        related_name="restaurants",
        blank=True,
    )

    class Meta:
        verbose_name = "restaurant"
        ordering = ["name"]
        indexes = [
            gis.Index(fields=["location"]),
            models.Index(fields=["zone", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def currency(self) -> str:
        """Devise héritée du pays — jamais choisie au niveau du restaurant."""
        return self.zone.city.country.currency

    @property
    def timezone(self) -> str:
        """Fuseau hérité du pays, comme la devise."""
        return self.zone.city.country.timezone

    def is_open_at(self, moment: dt.datetime) -> bool:
        """L'établissement est-il dans une plage d'ouverture à cet instant ?

        **Horaires seulement.** `is_active` et `accepts_orders` ne sont pas
        consultés ici : un restaurant ouvert qui a suspendu la prise de
        commande reste ouvert, et confondre les trois rendrait l'API incapable
        de dire au client *pourquoi* il ne peut pas commander.

        L'instant est converti dans le fuseau du pays avant comparaison. Sans
        cette conversion, un serveur en UTC fermerait un restaurant de Lomé une
        heure trop tôt en heure d'été européenne — le genre de décalage qu'on
        ne découvre qu'en production, un dimanche soir.
        """
        local = moment.astimezone(ZoneInfo(self.timezone))
        now, weekday = local.time(), local.weekday()
        yesterday = (weekday - 1) % 7

        for slot in self.opening_hours.all():
            if slot.weekday == weekday and slot.opens_at <= now:
                if slot.crosses_midnight or now < slot.closes_at:
                    return True
            # Une plage ouverte hier et à cheval sur minuit couvre encore le
            # petit matin d'aujourd'hui : c'est le créneau de nuit du week-end,
            # pas un cas de bord théorique.
            if slot.weekday == yesterday and slot.crosses_midnight and now < slot.closes_at:
                return True

        return False


class StaffMembership(UUIDModel, TimeStampedModel):
    """Rattachement d'un membre du personnel à un établissement.

    Sans cette table, « le personnel » est une population indistincte : un
    opérateur du restaurant de Kara voit — et fait avancer — les commandes de
    Lomé. La permission dit *ce qu'on a le droit de faire* (ADR-005) ; ce
    rattachement dit **sur quoi**. Confondre les deux revient à donner à chaque
    embauche l'accès à toute l'enseigne.

    La table est distincte de `Role` parce que les deux varient
    indépendamment : un même gérant peut couvrir deux établissements sans
    changer de rôle, et deux gérants du même établissement peuvent avoir des
    permissions différentes.

    Elle vit dans `restaurants` et non dans `accounts` : c'est l'établissement
    qui a du personnel, et `accounts` est le socle dont tout le reste dépend —
    lui faire connaître les restaurants inverserait le sens du graphe.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="staff_memberships")
    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name="staff_memberships"
    )
    is_manager = models.BooleanField(
        default=False,
        help_text="Informatif : les droits viennent des permissions, pas de ce drapeau.",
    )

    class Meta:
        verbose_name = "rattachement du personnel"
        verbose_name_plural = "rattachements du personnel"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "restaurant"], name="one_membership_per_user_and_restaurant"
            )
        ]
        indexes = [models.Index(fields=["user"]), models.Index(fields=["restaurant"])]

    def __str__(self) -> str:
        return f"{self.user.full_name} — {self.restaurant.name}"


class Weekday(models.IntegerChoices):
    # Aligné sur `date.weekday()` : lundi = 0. Cet alignement évite la
    # conversion manuelle qui est la source classique du décalage d'un jour.
    MONDAY = 0, "Lundi"
    TUESDAY = 1, "Mardi"
    WEDNESDAY = 2, "Mercredi"
    THURSDAY = 3, "Jeudi"
    FRIDAY = 4, "Vendredi"
    SATURDAY = 5, "Samedi"
    SUNDAY = 6, "Dimanche"


class OpeningHours(UUIDModel):
    """Plage d'ouverture.

    Plusieurs plages par jour sont possibles — service du midi et du soir. Une
    plage qui franchit minuit (`22:00 → 02:00`) est représentée par
    `closes_at < opens_at` ; le service d'ouverture en tient compte, plutôt que
    d'obliger à saisir deux plages sur deux jours.
    """

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name="opening_hours"
    )
    weekday = models.SmallIntegerField(choices=Weekday.choices)
    opens_at = models.TimeField()
    closes_at = models.TimeField()

    class Meta:
        verbose_name = "horaire d'ouverture"
        verbose_name_plural = "horaires d'ouverture"
        ordering = ["weekday", "opens_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "weekday", "opens_at"],
                name="opening_hours_unique_slot",
            ),
            models.CheckConstraint(
                condition=~models.Q(opens_at=models.F("closes_at")),
                name="opening_hours_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.opens_at:%H:%M}–{self.closes_at:%H:%M}"

    @property
    def crosses_midnight(self) -> bool:
        return self.closes_at < self.opens_at

    def covers(self, moment: dt.time) -> bool:
        if self.crosses_midnight:
            return moment >= self.opens_at or moment < self.closes_at
        return self.opens_at <= moment < self.closes_at
