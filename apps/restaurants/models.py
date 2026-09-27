"""Établissements — ADR-006.

`Restaurant` est le point de rattachement du multi-site : le catalogue, les
commandes et la flotte portent tous une clé de restaurant **non nulle** dès le
premier jour. C'est cette colonne, présente dès l'origine, qui rendra
l'ouverture d'un second établissement indolore.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.gis.db import models as gis
from django.db import models
from django.utils import timezone

from apps.accounts.models import User
from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.readiness import gaps_from_registry
from apps.restaurants.signals import restaurant_status_changed
from apps.restaurants.states import RESTAURANT_MACHINE, RestaurantStatus
from common.exceptions import BusinessRuleViolation
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel
from common.storage import banners

__all__ = [
    "AreaMembership",
    "IncompleteConfiguration",
    "KitchenClosure",
    "OpeningHours",
    "Restaurant",
    "RestaurantStatus",
    "StaffMembership",
    "Weekday",
    "kitchen_state_prefetches",
    "zone_anchoring_problem",
]


class IncompleteConfiguration(BusinessRuleViolation):
    """Mise en service demandée sur un établissement incomplet.

    Distincte d'`IllegalTransition` : l'enchaînement est légitime — on a bien le
    droit de passer de « prêt » à « en service » —, c'est le contenu qui ne
    l'est pas encore. Les confondre ferait répondre « transition refusée » à
    quelqu'un dont le seul tort est de n'avoir pas encore saisi ses horaires.

    Hérite de `BusinessRuleViolation` pour que le gestionnaire d'exceptions du
    socle la rende en 409 RFC 9457 sans le connaître : `common` ne peut pas
    importer `apps.restaurants` (ADR-002), et une branche de plus dans son
    `problem_detail_handler` aurait inversé le graphe.

    `missing` et non `manques` : les membres du corps d'erreur voyagent jusqu'au
    client Flutter, où le reste du contrat est en anglais.
    """

    code = "incomplete_configuration"
    title = "Établissement incomplet"

    def __init__(self, manques: list[str]) -> None:
        self.manques = manques
        super().__init__(
            "Cet établissement ne peut pas être mis en service : " + " ".join(manques),
            missing=manques,
        )


def zone_anchoring_problem(zone: DeliveryZone, restaurant_id: uuid.UUID | None) -> str | None:
    """Pourquoi cette zone ne peut pas porter cet établissement — `None` si elle le peut.

    Une cuisine se pose sur une zone **municipale** de sa ville, ou sur l'une de
    ses propres zones. La zone propre d'une autre cuisine porte le barème de
    celle-ci : s'y poser ferait facturer la course au tarif du voisin, et
    `resolve_zone`, qui écarte les zones des autres établissements, ne la
    retiendrait jamais pour cette cuisine — une zone de rattachement qui ne
    s'applique à rien.

    `restaurant_id` est nul à la création : aucune zone ne peut encore lui être
    propre.
    """
    if zone.restaurant_id is not None and zone.restaurant_id != restaurant_id:
        return (
            f"La zone « {zone.name} » est propre à un autre établissement : une cuisine "
            "se rattache à une zone municipale de sa ville, ou à l'une des siennes."
        )
    return None


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

    # Cycle de vie complet, du brouillon à la suspension — voir
    # `apps.restaurants.states`. C'est la **seule** colonne d'état qu'on écrit.
    status = models.CharField(
        max_length=16,
        choices=RestaurantStatus.choices,
        default=RestaurantStatus.DRAFT,
        help_text="Un établissement n'est visible du public qu'en service.",
    )

    # **Dérivé de `status` par `save()`**, jamais saisi : c'est la projection
    # booléenne « publié ou non ». Elle reste en colonne parce que huit requêtes
    # de production la filtrent — panier, commande, catalogue, candidature
    # livreur — et qu'un `status="active"` recopié à ces huit endroits serait la
    # même règle écrite huit fois, donc huit occasions de la corriger à moitié.
    #
    # `accepts_orders`, lui, reste conjoncturel : un coup de feu en cuisine, une
    # panne de four. Le confondre avec la publication obligerait à dépublier un
    # restaurant pour arrêter les commandes une heure, ce qui le ferait
    # disparaître de l'application au lieu de l'y montrer débordé.
    is_active = models.BooleanField(default=False)
    accepts_orders = models.BooleanField(default=True)

    default_preparation_minutes = models.PositiveSmallIntegerField(default=20)

    # Proposer soi-même la course au livreur compatible le plus proche quand une
    # commande est prête (`apps.delivery.dispatch`). Désactivé, la cuisine
    # affecte à la main depuis le back-office, comme avant ce champ — ce qui
    # reste possible dans les deux cas.
    auto_dispatch_couriers = models.BooleanField(
        default=True,
        help_text="Proposer automatiquement la course à un livreur compatible dès que "
        "la commande est prête.",
    )

    # Plafond de valeur au-delà duquel une perte ou une correction de stock
    # exige une **seconde validation**, par une autre personne que celle qui la
    # déclare (voir `apps.inventory.services`).
    #
    # Par établissement, parce qu'un plafond est un montant, et qu'un montant a
    # la devise du pays : un plafond d'enseigne en francs CFA serait faux pour
    # une cuisine d'Accra.
    #
    # **Nul veut dire : tout se valide**, et non « rien ne se valide ». Une perte
    # est un actif qui sort du bilan sans transaction ; tant que personne n'a
    # décidé à partir de quel montant elle peut passer seule, elle ne passe pas
    # seule. Le défaut sûr est le contrôle, qu'on desserre en fixant le plafond.
    #
    # Modifiable par `restaurants.write` seulement — le siège, pas le gérant de
    # la cuisine dont il encadre les écritures.
    stock_adjustment_ceiling = MoneyField(null=True)

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
            models.Index(fields=["status"], name="restaurants_status_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Maintient `is_active` aligné sur `status`.

        La dérivation est faite ici et non dans un sérialiseur parce qu'elle
        doit tenir pour **toute** écriture : back-office, `django-admin`,
        commande de peuplement, correction en `shell`. Placée dans une seule
        couche d'entrée, elle laisserait les autres produire des lignes où un
        établissement suspendu reste publié.

        `update_fields` est complété quand la valeur change, sans quoi un
        `save(update_fields=["status"])` — la forme qu'écrit naturellement une
        transition — écrirait le nouvel état sans son reflet booléen, et la
        ligne resterait visible du public.
        """
        publie = self.status == RestaurantStatus.ACTIVE
        if self.is_active != publie:
            self.is_active = publie
            champs = kwargs.get("update_fields")
            if champs is not None:
                kwargs["update_fields"] = {*champs, "is_active"}
        super().save(*args, **kwargs)

    # ------------------------------------------------------- cycle de vie

    def configuration_gaps(self) -> list[str]:
        """Ce qui manque encore pour ouvrir au public, en clair.

        Rend une liste de phrases et non un booléen : « ce restaurant n'est pas
        prêt » n'apprend rien à qui vient de remplir un formulaire de dix
        champs. La liste dit quel écran ouvrir ensuite, et c'est elle que le
        back-office affiche sous la fiche.

        Ce que cette méthode vérifie elle-même est ce que `restaurants`
        possède : la position, la zone, les horaires, le personnel. La carte et
        la flotte vivent ailleurs et s'annoncent par le registre — voir
        `apps.restaurants.readiness`, qui explique pourquoi ce n'est pas une
        relation inverse.
        """
        manques: list[str] = []

        # Le point de retrait hors de la zone qu'il dessert est la faute de
        # saisie que rien d'autre n'attraperait : la commande partirait, et le
        # calcul de distance mesurerait depuis une autre ville. `City` porte une
        # clé vers `Country`, si bien qu'une ville d'un autre pays ne peut pas
        # être choisie ; les deux incohérences que le schéma laisse passer sont
        # celle-ci et la suivante.
        if not self.zone.boundary.covers(self.location):
            manques.append("La position de l'établissement tombe hors de sa zone de livraison.")

        # La zone propre d'une autre cuisine. La saisie la refuse
        # (`ManagedRestaurantSerializer.validate_zone`) ; le rappeler ici couvre
        # ce que la saisie ne voit pas — `django-admin`, un `shell`, une zone
        # rattachée *après coup* à un autre établissement.
        if (probleme := zone_anchoring_problem(self.zone, self.pk)) is not None:
            manques.append(probleme)

        # Un marché fermé à un étage quelconque rend la cuisine invisible : la
        # publier la montrerait « En service » au back-office et introuvable
        # dans l'application, sans que rien ne dise pourquoi.
        if not self.zone.is_active:
            manques.append(f"La zone de livraison « {self.zone.name} » est désactivée.")
        if not self.zone.city.is_active:
            manques.append(f"La ville « {self.zone.city.name} » est désactivée.")
        if not self.zone.city.country.is_active:
            manques.append(f"Le marché « {self.zone.city.country.name} » est fermé.")
        if not self.opening_hours.exists():
            manques.append("Aucune plage d'ouverture n'est définie.")
        if not self.staff_memberships.exists():
            manques.append("Aucun membre du personnel n'est rattaché à cet établissement.")

        return manques + gaps_from_registry(self)

    @property
    def is_published(self) -> bool:
        """Visible des applications clientes ?"""
        return self.status == RestaurantStatus.ACTIVE

    def transition_to(self, target: str) -> None:
        """Change d'état en passant par la machine — le seul chemin d'écriture.

        La mise en service est la seule transition que la complétude garde :
        les autres servent précisément à corriger ce qui manque, et les
        interdire tant que quelque chose manque rendrait la configuration
        impossible à commencer.
        """
        RESTAURANT_MACHINE.validate(self.status, target)

        if target == RestaurantStatus.ACTIVE:
            manques = self.configuration_gaps()
            if manques:
                raise IncompleteConfiguration(manques)

        precedent = self.status
        self.status = target
        self.save(update_fields=["status", "updated_at"])

        # Émis **après** l'enregistrement et dans la même transaction : un
        # abonné qui relirait l'établissement avant le `save` verrait l'ancien
        # état, et un abonné qui écrit doit le faire de façon atomique avec la
        # transition. Ce qui sort vers le réseau est reporté après le commit par
        # l'abonné lui-même.
        restaurant_status_changed.send(
            sender=Restaurant, restaurant=self, previous=precedent, target=target
        )

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

    # ------------------------------------------------ fermetures exceptionnelles

    def closures_after(self, moment: dt.datetime) -> list[KitchenClosure]:
        """Fermetures exceptionnelles qui ne sont pas terminées à cet instant.

        Lit le préchargement quand la vue l'a posé (`kitchen_state_prefetches`),
        sinon interroge la base en ne rapatriant que l'avenir : l'historique des
        fermetures grandit d'année en année et n'intéresse aucun verdict.
        """
        cache = getattr(self, "_prefetched_objects_cache", {})
        if "closures" in cache:
            return [fermeture for fermeture in self.closures.all() if fermeture.ends_at > moment]
        return list(self.closures.filter(ends_at__gt=moment).order_by("starts_at"))

    def closure_at(self, moment: dt.datetime) -> KitchenClosure | None:
        """La fermeture exceptionnelle en cours à cet instant, s'il y en a une."""
        for fermeture in self.closures_after(moment):
            if fermeture.starts_at <= moment < fermeture.ends_at:
                return fermeture
        return None


def kitchen_state_prefetches(at: dt.datetime | None = None) -> tuple[Any, ...]:
    """Ce qu'il faut précharger pour juger l'état de plusieurs cuisines sans N+1.

    Les horaires et les fermetures **à venir** : deux requêtes pour toute une
    liste, là où `kitchen_state` en coûterait deux par cuisine. À employer
    partout où l'on précharge des horaires pour un verdict.
    """
    depuis = at if at is not None else timezone.now()
    return (
        "opening_hours",
        models.Prefetch(
            "closures",
            queryset=KitchenClosure.objects.filter(ends_at__gt=depuis).order_by("starts_at"),
        ),
    )


class KitchenClosure(UUIDModel, TimeStampedModel):
    """Fermeture exceptionnelle — un jour férié, des travaux, une coupure de gaz.

    ## Pourquoi pas une plage d'horaires en moins

    Les horaires disent la semaine type ; ils se répètent. Supprimer la plage
    du mardi pour fermer **ce** mardi fermerait aussi tous les suivants, et il
    faudrait se souvenir de la remettre. Une fermeture a un début et une fin
    datés, et **se lève d'elle-même** : la cuisine rouvre à l'heure dite sans
    qu'on y pense — ce qu'aucun interrupteur ne fait.

    ## Pourquoi pas `accepts_orders`

    La pause dit « on ne prend rien pour quelques minutes » et n'a pas de fin
    connue. Une fermeture en a une, que le client doit lire : « fermé
    exceptionnellement, réouverture demain à 11 h ». Les confondre ferait
    annoncer « réessayez dans quelques minutes » pour une fermeture de deux
    jours.

    Les instants sont absolus (UTC en base) : ils se saisissent et s'affichent
    dans le fuseau du pays, comme les horaires se comparent dans ce fuseau.
    """

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="closures")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(
        max_length=200,
        blank=True,
        help_text="Montré au client : « fermeture exceptionnelle (jour férié) ».",
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = "fermeture exceptionnelle"
        verbose_name_plural = "fermetures exceptionnelles"
        ordering = ["starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="kitchen_closure_ends_after_start",
            ),
        ]
        indexes = [
            models.Index(fields=["restaurant", "ends_at"], name="closure_restaurant_end_idx")
        ]

    def __str__(self) -> str:
        return (
            f"{self.restaurant.name} — fermée du {self.starts_at:%d/%m %H:%M} "
            f"au {self.ends_at:%d/%m %H:%M}"
        )


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


class AreaMembership(UUIDModel, TimeStampedModel):
    """Rattachement d'un membre du personnel à un **marché** ou à une ville.

    ## Le palier qui manquait

    Le cloisonnement n'avait que deux étages : le siège, qui voit tout, et le
    rattachement à un établissement, qui ne voit que lui. Rien entre les deux.
    Un directeur pays devait donc être rattaché à chacun de ses établissements,
    un par un — et le jour où l'on en ouvrait un nouveau, il cessait
    silencieusement de le voir. Un responsable de ville avait le même problème,
    en plus fréquent.

    L'alternative retenue ailleurs — faire du directeur pays un
    superutilisateur — donne accès aux autres pays, aux rôles et à
    `django-admin`. Elle transforme un besoin de périmètre en escalade de
    privilèges.

    ## Pourquoi une table à part de `StaffMembership`

    Les deux disent « sur quoi », mais pas avec la même granularité, et surtout
    pas avec la même **durée de validité** : un rattachement d'établissement
    désigne une ligne qui existe, un rattachement de pays désigne un ensemble
    qui grandit. Les fondre dans une table à trois clés étrangères nullables
    ferait porter à `StaffMembership.restaurant` — aujourd'hui non nul, et lu
    partout — une nullité qu'il faudrait vérifier à chaque appel.

    ## Ce que ce rattachement n'accorde pas

    Des permissions. Elles restent portées par les rôles (ADR-005), et un
    directeur pays sans `orders.refund` ne rembourse pas plus qu'avant. Ce
    rattachement dit **sur quoi** s'appliquent celles qu'on a déjà — la même
    séparation que pour `StaffMembership`, pour la même raison.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="area_memberships")

    # Exactement l'un des deux — voir la contrainte. Une ligne portant les deux
    # serait ambiguë (le pays gagne-t-il sur la ville ?) et une ligne n'en
    # portant aucun accorderait un périmètre vide, ce qui est un rattachement
    # qui ne rattache à rien : un défaut de saisie qu'aucun écran ne montrerait.
    country = models.ForeignKey(
        Country, on_delete=models.CASCADE, related_name="staff_areas", null=True, blank=True
    )
    city = models.ForeignKey(
        City, on_delete=models.CASCADE, related_name="staff_areas", null=True, blank=True
    )

    class Meta:
        verbose_name = "rattachement de périmètre"
        verbose_name_plural = "rattachements de périmètre"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(country__isnull=False, city__isnull=True)
                    | models.Q(country__isnull=True, city__isnull=False)
                ),
                name="area_membership_exactly_one_target",
            ),
            models.UniqueConstraint(
                fields=["user", "country"],
                condition=models.Q(country__isnull=False),
                name="one_area_per_user_and_country",
            ),
            models.UniqueConstraint(
                fields=["user", "city"],
                condition=models.Q(city__isnull=False),
                name="one_area_per_user_and_city",
            ),
        ]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"{self.user.full_name} — {self.country or self.city}"


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
