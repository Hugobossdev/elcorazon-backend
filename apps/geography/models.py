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

__all__ = ["City", "Country", "DeliveryZone", "validate_timezone"]


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
    name = models.CharField(max_length=100)
    boundary = gis.MultiPolygonField(geography=True, srid=4326)

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
            models.UniqueConstraint(fields=["city", "name"], name="zone_name_unique_per_city")
        ]
        indexes = [
            # Index GiST sur le contour : sans lui, déterminer la zone d'un
            # point balaie toute la table à chaque passage de commande.
            gis.Index(fields=["boundary"]),
            models.Index(fields=["city", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} — {self.city.name}"
