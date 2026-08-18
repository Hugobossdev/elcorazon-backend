"""Suivi de position.

La table la plus volumineuse du produit, et de loin : à un relevé toutes les
dix secondes et deux cents livreurs actifs, l'écriture systématique produirait
environ 1,7 million de lignes par jour, pour une valeur analytique faible.

Deux mesures en découlent :

* l'écriture est **échantillonnée** par le service — un relevé sur N, ou au
  franchissement d'un seuil de distance. La *diffusion* temps réel, elle, est
  intégrale : c'est elle qui fait l'expérience de suivi, pas la persistance ;
* les relevés sont **purgés** par une tâche planifiée. Le suivi n'a de valeur
  qu'en direct ; passé la livraison, seul le tracé résumé importe.

**L3** — un relevé est rattaché à une **course**, pas à un livreur. Il est donc
impossible d'en écrire un pour une commande qu'on ne dessert pas : l'ancien code
n'exigeait pas ce lien, ce qui permettait de falsifier le suivi d'autrui.
"""

from __future__ import annotations

from django.contrib.gis.db import models as gis
from django.db import models

from apps.delivery.models import Assignment
from common.models import UUIDModel

__all__ = ["LocationPing"]


class LocationPing(UUIDModel):
    """Position instantanée d'un livreur pendant une course."""

    assignment = models.ForeignKey(
        Assignment, on_delete=models.CASCADE, related_name="location_pings"
    )
    point = gis.PointField(geography=True, srid=4326)

    accuracy_m = models.FloatField(null=True, blank=True)
    speed_mps = models.FloatField(null=True, blank=True)
    heading_deg = models.FloatField(null=True, blank=True)

    # Horodatage **de l'appareil**, distinct de la réception. Un livreur qui
    # traverse une zone sans réseau émet en différé : sans cette distinction,
    # une rafale de relevés rattrapés dessinerait un trajet instantané.
    recorded_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "relevé de position"
        verbose_name_plural = "relevés de position"
        ordering = ["-recorded_at"]
        indexes = [
            # La requête de suivi : les derniers relevés d'une course.
            models.Index(fields=["assignment", "-recorded_at"]),
            # La purge planifiée balaie par date de réception.
            models.Index(fields=["received_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(heading_deg__isnull=True)
                | (models.Q(heading_deg__gte=0) & models.Q(heading_deg__lt=360)),
                name="ping_heading_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(speed_mps__isnull=True) | models.Q(speed_mps__gte=0),
                name="ping_speed_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.assignment.order.reference} @ {self.recorded_at:%H:%M:%S}"
