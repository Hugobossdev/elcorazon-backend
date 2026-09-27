"""Profils clients : adresses et préférences.

Suppression **dure**, contrairement au catalogue : le RGPD impose un droit à
l'effacement, et une adresse ne porte aucune écriture financière. La commande,
elle, en conserve une copie figée — c'est elle qui doit rester lisible, pas
l'adresse d'origine.
"""

from __future__ import annotations

from django.contrib.gis.db import models as gis
from django.contrib.postgres.fields import ArrayField
from django.core.validators import RegexValidator
from django.db import models

from apps.accounts.models import User
from apps.geography.models import City
from common.models import TimeStampedModel, UUIDModel

__all__ = ["Address", "AddressKind", "CustomerPreference"]


class AddressKind(models.TextChoices):
    HOME = "home", "Domicile"
    WORK = "work", "Travail"
    OTHER = "other", "Autre"


class Address(UUIDModel, TimeStampedModel):
    """Adresse de livraison.

    `location` est le point qui compte : à Lomé comme dans beaucoup de villes
    d'Afrique de l'Ouest, l'adressage postal est peu fiable et la livraison se
    fait aux coordonnées, guidée par un point de repère. D'où `landmark`, qui
    n'est pas un ornement mais l'information dont le livreur se sert réellement.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="addresses")

    label = models.CharField(max_length=50, help_text="Nom donné par le client, ex. « Maison ».")
    kind = models.CharField(max_length=8, choices=AddressKind.choices, default=AddressKind.OTHER)

    recipient_name = models.CharField(max_length=150, blank=True)
    recipient_phone = models.CharField(
        max_length=16,
        blank=True,
        validators=[RegexValidator(regex=r"^\+[1-9]\d{7,14}$", message="Format E.164 attendu.")],
        help_text="Si différent du titulaire du compte — livraison à un tiers.",
    )

    line1 = models.CharField(max_length=200)
    line2 = models.CharField(max_length=200, blank=True)
    landmark = models.CharField(
        max_length=200,
        blank=True,
        help_text="Point de repère : « en face de la pharmacie Bel Air ».",
    )
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="addresses")

    location = gis.PointField(geography=True, srid=4326)

    delivery_instructions = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name = "adresse"
        ordering = ["-is_default", "-created_at"]
        constraints = [
            # Index unique partiel : un seul défaut par utilisateur, garanti par
            # la base. Le faire en Python exposerait à une course entre deux
            # requêtes concurrentes marquant chacune son adresse par défaut.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True),
                name="one_default_address_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            gis.Index(fields=["location"]),
        ]

    def __str__(self) -> str:
        return f"{self.label} — {self.line1}"


class CustomerPreference(UUIDModel, TimeStampedModel):
    """Préférences alimentaires et de service.

    Séparées de `User` parce qu'elles ne concernent que les clients et qu'elles
    évoluent à une tout autre fréquence que l'identité — les charger à chaque
    authentification serait du gaspillage.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferences")

    dietary_restrictions = ArrayField(
        models.CharField(max_length=32),
        default=list,
        blank=True,
        help_text="Ex. vegetarian, vegan, halal, gluten_free.",
    )
    allergens = ArrayField(
        models.CharField(max_length=32),
        default=list,
        blank=True,
        help_text="Signalés au restaurant sur chaque commande.",
    )

    # Préférences de notification. Le canal push ne se coupe pas ici : une
    # notification transactionnelle (« votre livreur arrive ») n'est pas du
    # marketing et doit passer quoi qu'il arrive.
    marketing_push_enabled = models.BooleanField(default=True)
    marketing_email_enabled = models.BooleanField(default=True)

    preferred_language = models.CharField(max_length=5, default="fr")

    class Meta:
        verbose_name = "préférences client"
        verbose_name_plural = "préférences clients"

    def __str__(self) -> str:
        return f"Préférences de {self.user.email}"
