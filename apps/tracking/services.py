"""Écriture et lecture des positions — invariant L3.

**Un relevé appartient à une course, pas à un livreur.** C'est la formulation
structurelle de L3 : il n'existe aucun chemin pour écrire une position sur une
commande qu'on ne dessert pas. L'ancien code n'exigeait pas ce lien, ce qui
permettait de falsifier le suivi d'autrui — de faire croire à un client que son
repas approchait.

L'écriture est **échantillonnée**, la diffusion ne l'est pas. C'est elle qui
fait l'expérience de suivi ; la persistance ne sert qu'au litige et à
l'analyse, où une position toutes les trente secondes suffit largement.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.utils import timezone

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.tracking.models import LocationPing
from common.exceptions import BusinessRuleViolation

__all__ = ["TRACKABLE_STATUSES", "TrackingService"]

#: Étapes pendant lesquelles une position a un sens.
#:
#: Avant l'enlèvement, le livreur n'a rien à bord ; après la livraison, sa
#: position ne regarde plus personne. Suivre un livreur en dehors de sa course
#: n'est pas un service, c'est une filature.
TRACKABLE_STATUSES = frozenset(
    {DeliveryStatus.ACCEPTED, DeliveryStatus.PICKED_UP, DeliveryStatus.ON_THE_WAY}
)


class TrackingService:
    @staticmethod
    def record(
        *,
        assignment: Assignment,
        courier: CourierProfile,
        point: Point,
        recorded_at: dt.datetime,
        accuracy_m: float | None = None,
        speed_mps: float | None = None,
        heading_deg: float | None = None,
    ) -> LocationPing | None:
        """Enregistre une position, ou l'ignore si l'échantillonnage la rejette.

        Renvoie `None` quand le relevé n'a pas été persisté. Ce n'est pas une
        erreur : la position a bien été reçue, elle sera diffusée, elle n'a
        simplement pas mérité une ligne. Le distinguer d'un refus permet au
        client de savoir que son envoi a porté.
        """
        TrackingService._assert_own_course(assignment, courier)

        # La position du dossier est **toujours** rafraîchie, même quand le
        # relevé n'est pas persisté : c'est elle qui sert à l'affectation, et
        # une position vieille de trente secondes y est parfaitement bonne.
        CourierProfile.objects.filter(pk=courier.pk).update(
            last_location=point, last_location_at=recorded_at
        )

        if not TrackingService._deserves_a_row(assignment, point, recorded_at):
            return None

        return LocationPing.objects.create(
            assignment=assignment,
            point=point,
            recorded_at=recorded_at,
            accuracy_m=accuracy_m,
            speed_mps=speed_mps,
            heading_deg=heading_deg,
        )

    @staticmethod
    def _assert_own_course(assignment: Assignment, courier: CourierProfile) -> None:
        """L3 — deux vérifications, pas une.

        Que la course soit bien la sienne **et** qu'elle soit en cours. La
        seconde compte autant : sans elle, un livreur continuerait d'alimenter
        le suivi d'une course livrée la veille, et le client verrait un point
        se promener après avoir reçu son repas.
        """
        if assignment.courier_id != courier.pk:
            raise BusinessRuleViolation(
                "Cette course n'est pas la vôtre.", assignment_id=str(assignment.pk)
            )
        if assignment.status not in TRACKABLE_STATUSES:
            raise BusinessRuleViolation(
                "Cette course n'est pas en cours ; aucune position n'y est attendue.",
                current_status=assignment.status,
            )

    @staticmethod
    def _deserves_a_row(assignment: Assignment, point: Point, recorded_at: dt.datetime) -> bool:
        """Décide si ce relevé mérite une écriture.

        Deux critères, dont un seul suffit : le temps écoulé, et la distance
        parcourue. Le second est ce qui rend le tracé fidèle malgré
        l'échantillonnage — un livreur arrêté à un feu n'écrit rien, un livreur
        qui avance vite écrit à chaque seuil franchi.
        """
        # La distance est annotée par PostGIS dans la même requête que la
        # lecture du dernier relevé : sur `geography`, elle sort en mètres sur
        # l'ellipsoïde. La calculer en Python depuis les degrés donnerait un
        # seuil qui varie avec la latitude — juste à Lomé, faux à Tanger.
        last = (
            LocationPing.objects.filter(assignment=assignment)
            .annotate(moved=Distance("point", point))
            .order_by("-recorded_at")
            .first()
        )
        if last is None:
            return True

        elapsed = (recorded_at - last.recorded_at).total_seconds()
        if elapsed >= settings.TRACKING_MIN_WRITE_SECONDS:
            return True

        moved_m: float = last.moved.m
        return moved_m >= settings.TRACKING_MIN_WRITE_METERS

    @staticmethod
    def latest_for(assignment: Assignment) -> LocationPing | None:
        return LocationPing.objects.filter(assignment=assignment).order_by("-recorded_at").first()

    @staticmethod
    def purge_older_than(days: int) -> int:
        """Purge les relevés anciens.

        Le suivi n'a de valeur qu'en direct ; passé la livraison et le délai de
        réclamation, garder 1,7 million de lignes par jour coûte sans rien
        apporter. Appelée par une tâche planifiée — qui viendra avec la chaîne
        Celery.
        """
        horizon = timezone.now() - dt.timedelta(days=days)
        deleted, _ = LocationPing.objects.filter(received_at__lt=horizon).delete()
        return deleted
