"""Hiérarchie géographique — ADR-006.

    Country → City → DeliveryZone

C'est le socle du multi-pays. L'existant n'en avait aucun : le catalogue était
global et les frais de livraison une constante — et une constante
contradictoire, `5.00` côté commande contre `500.0` côté panier, ce qui
trahissait l'absence de toute règle de tarification.

La hiérarchie est posée entière dès maintenant parce que son coût est
irrécupérable : la rajouter après coup imposerait de migrer commandes,
paiements et historiques. Les fonctionnalités qu'elle rend possibles, elles,
s'ajoutent sans rien casser.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.gis.db import models as gis
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel
from common.money import CURRENCY_EXPONENTS

__all__ = ["City", "Country", "DeliveryZone", "ZoneShape", "validate_timezone"]


def validate_timezone(value: str) -> None:
    """Refuse un fuseau que la bibliothèque standard ne connaît pas.

    Le fuseau du pays sert à décider si un restaurant est ouvert. Une faute de
    frappe (`Africa/Lomé`) ne se verrait donc qu'au moment de rendre une liste
    de restaurants — en 500, à la première requête d'un client. Validé à la
    saisie, le même défaut se voit dans le back-office, là où on peut le
    corriger.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"Fuseau horaire inconnu : {value!r}.") from exc


class Country(UUIDModel, TimeStampedModel):
    """Pays d'opération.

    Porte la devise et le fuseau : ce sont des propriétés du marché, pas du
    restaurant. Deux établissements d'un même pays ne peuvent pas facturer dans
    deux devises différentes.
    """

    iso_code = models.CharField(
        max_length=2, unique=True, help_text="Code ISO 3166-1 alpha-2, par exemple TG."
    )
    name = models.CharField(max_length=100)
    currency = models.CharField(
        max_length=3,
        choices=[(c, c) for c in sorted(CURRENCY_EXPONENTS)],
        help_text="ISO 4217. Figée sur chaque commande au moment de sa création.",
    )
    phone_prefix = models.CharField(max_length=5, help_text="Par exemple +228.")
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    default_language = models.CharField(max_length=5, default="fr")

    # Code alpha-3. Facultatif parce qu'`iso_code` suffit à identifier un pays
    # et porte déjà l'unicité : celui-ci n'existe que pour les intégrations qui
    # ne parlent qu'alpha-3 — certaines passerelles de paiement, la plupart des
    # jeux de données démographiques.
    iso3_code = models.CharField(
        max_length=3, blank=True, help_text="Code ISO 3166-1 alpha-3, par exemple TGO."
    )

    # Symbole d'affichage, distinct du code ISO 4217 porté par `currency`.
    #
    # Les deux sont nécessaires et ne se déduisent pas l'un de l'autre : `XOF`
    # est ce qu'on écrit dans un contrat et ce que `Money` compare, « FCFA » est
    # ce qu'on montre à un client. Le symbole était jusqu'ici écrit en dur dans
    # trois écrans du back-office, avec la même expression recopiée —
    # `zone.currency == 'XOF' ? 'FCFA' : zone.currency`.
    currency_symbol = models.CharField(
        max_length=8, blank=True, help_text="Par exemple FCFA. À défaut, le code ISO est affiché."
    )

    # Centre du marché — sert à cadrer une carte à l'ouverture d'un pays, quand
    # aucune ville n'y est encore déclarée. Sans lui, le back-office ouvre sa
    # carte sur l'océan au large du golfe de Guinée, coordonnées (0, 0).
    centroid = gis.PointField(geography=True, srid=4326, null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "pays"
        verbose_name_plural = "pays"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.iso_code})"


class City(UUIDModel, TimeStampedModel):
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="cities")
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100)
    # Sert à centrer une carte et à trier des résultats par proximité, pas à
    # décider d'une livrabilité — c'est le rôle de la zone.
    centroid = gis.PointField(geography=True, srid=4326)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "ville"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["country", "slug"], name="city_slug_unique_per_country")
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.country.iso_code}"


class ZoneShape(models.TextChoices):
    """Comment le contour d'une zone a été saisi.

    **Le contour reste un `MultiPolygon` dans tous les cas** : c'est lui que
    PostGIS interroge, et c'est ce qui permet d'ajouter ce champ sans toucher à
    une seule requête existante. Ce que le type dit, c'est *avec quel outil on a
    dessiné*, donc quel écran rouvrir pour corriger.

    Sans lui, une zone saisie comme un disque de 5 km se rouvrait en éditeur de
    polygone, avec les soixante-quatre points de sa discrétisation : illisible,
    et impossible à ramener à « 5 km » sans tout redessiner.
    """

    CIRCLE = "circle", "Cercle"
    POLYGON = "polygon", "Polygone"
    ADMINISTRATIVE = "administrative", "Zone administrative"


class DeliveryZone(UUIDModel, TimeStampedModel):
    """Périmètre de livraison et barème de frais associé.

    Le contour est un `MultiPolygon` et non un simple `Polygon` : une zone
    réelle est fréquemment discontinue — un fleuve, une voie ferrée ou une
    enclave non desservie la coupent en plusieurs morceaux.

    Le type `geography` (et non `geometry`) fait que PostGIS raisonne sur
    l'ellipsoïde : une distance sort en mètres, sans projection à choisir ni
    erreur qui croît avec la latitude.
    """

    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="zones")

    # Rattachement **facultatif** à un établissement.
    #
    # Une zone sans établissement est municipale : elle vaut pour toutes les
    # cuisines de la ville, et c'est le cas courant — c'est aussi le seul qui
    # existait avant ce champ. Rattachée, elle ne vaut que pour la sienne, et
    # prime sur les municipales qui couvrent le même point : c'est ce qui permet
    # à deux cuisines d'une même ville de facturer différemment sans que l'une
    # hérite du barème de l'autre.
    #
    # La clé est posée en `SET_NULL` et non en `CASCADE` : supprimer un
    # établissement — ce que le produit ne fait pas — ne doit pas emporter un
    # contour que quelqu'un a dessiné, ni les commandes qui s'y réfèrent.
    #
    # Le nom du champ est textuel (`apps.restaurants.Restaurant`) parce que
    # `geography` n'a pas le droit d'importer `restaurants` : la clé étrangère
    # est une dépendance de schéma, que Django résout au chargement, et non une
    # dépendance de code.
    restaurant = models.ForeignKey(
        "restaurants.Restaurant",
        on_delete=models.SET_NULL,
        related_name="own_zones",
        null=True,
        blank=True,
        help_text="Zone propre à un établissement. Vide, la zone vaut pour toute la ville.",
    )

    name = models.CharField(max_length=100)
    boundary = gis.MultiPolygonField(geography=True, srid=4326)

    shape = models.CharField(
        max_length=16,
        choices=ZoneShape.choices,
        default=ZoneShape.POLYGON,
        help_text="Avec quel outil le contour a été saisi — le contour reste un polygone.",
    )

    # Paramètres du disque, conservés **en plus** du contour qu'ils produisent.
    #
    # Le contour seul suffirait à livrer ; il ne suffit pas à *rouvrir* la
    # saisie. Garder le centre et le rayon, c'est pouvoir réafficher « 5 km »
    # dans un champ, et non soixante-quatre sommets qu'on ne saurait ni relire
    # ni ajuster.
    #
    # Nuls pour un polygone ou une zone administrative — une contrainte le
    # garde, plutôt qu'une convention qu'on oublierait.
    center = gis.PointField(geography=True, srid=4326, null=True, blank=True)
    radius_meters = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(100), MaxValueValidator(200_000)],
        help_text="Rayon du disque, en mètres. Renseigné pour les zones circulaires seulement.",
    )

    # Départage explicite, pour ce que la géométrie ne tranche pas : deux zones
    # de surface voisine dont l'une doit l'emporter par décision commerciale.
    # Zéro pour toutes tant que personne n'arbitre, auquel cas la surface décide
    # — voir `apps.geography.resolution`.
    priority = models.SmallIntegerField(
        default=0,
        help_text="À surface comparable, la priorité la plus haute l'emporte.",
    )

    # Barème. Remplace la constante incohérente de l'existant et sert dès le
    # premier restaurant.
    base_fee = MoneyField()
    fee_per_km = MoneyField()
    free_delivery_threshold = MoneyField(
        null=True,
    )
    min_order_amount = MoneyField(null=True)
    max_distance_km = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=15,
        validators=[MinValueValidator(0), MaxValueValidator(500)],
        help_text="Au-delà, la zone refuse la course même si le point est dans le contour.",
    )

    estimated_delivery_minutes = models.PositiveSmallIntegerField(default=30)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "zone de livraison"
        verbose_name_plural = "zones de livraison"
        ordering = ["city", "name"]
        constraints = [
            models.UniqueConstraint(fields=["city", "name"], name="zone_name_unique_per_city"),
            # Un disque sans centre ni rayon serait irrouvrable en édition, et un
            # polygone qui en porterait ferait croire à un cercle qu'on pourrait
            # redimensionner. La contrainte tient l'invariant que le sérialiseur
            # vérifie déjà : elle le tient aussi pour `django-admin`, pour une
            # commande de peuplement et pour une correction en `shell`.
            models.CheckConstraint(
                condition=(
                    models.Q(shape="circle", center__isnull=False, radius_meters__isnull=False)
                    | (
                        ~models.Q(shape="circle")
                        & models.Q(center__isnull=True, radius_meters__isnull=True)
                    )
                ),
                name="zone_circle_carries_center_and_radius",
            ),
        ]
        indexes = [
            # Index GiST sur le contour : sans lui, déterminer la zone d'un
            # point balaie toute la table à chaque passage de commande.
            gis.Index(fields=["boundary"]),
            models.Index(fields=["city", "is_active"]),
            # Les zones propres à un établissement sont interrogées à chaque
            # devis : sans cet index, la clause qui les distingue des zones
            # municipales balaie la table.
            models.Index(fields=["restaurant", "is_active"], name="zone_restaurant_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} — {self.city.name}"
