"""Tâche planifiée du suivi.

La table des positions croît d'environ 1,7 million de lignes par jour à deux
cents livreurs actifs. C'est la seule table du projet dont la purge n'est pas
une hygiène mais une condition d'exploitation.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.tracking.models import LocationPing
from apps.tracking.tasks import purge_stale_locations

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def course(order: Order, courier: CourierProfile) -> Assignment:
    return Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.ACCEPTED)


def ping(course: Assignment, *, recu_il_y_a: dt.timedelta) -> LocationPing:
    releve = LocationPing.objects.create(
        assignment=course, point=Point(1.2255, 6.1319, srid=4326), recorded_at=timezone.now()
    )
    LocationPing.objects.filter(pk=releve.pk).update(received_at=timezone.now() - recu_il_y_a)
    return releve


class TestPurgeDesPositions:
    def test_les_releves_anciens_partent(self, course: Assignment) -> None:
        ping(course, recu_il_y_a=dt.timedelta(days=40))

        assert purge_stale_locations(days=30) == 1
        assert LocationPing.objects.count() == 0

    def test_les_releves_recents_restent(self, course: Assignment) -> None:
        ping(course, recu_il_y_a=dt.timedelta(days=2))

        assert purge_stale_locations(days=30) == 0
        assert LocationPing.objects.count() == 1

    def test_la_course_survit_a_la_purge_de_ses_positions(self, course: Assignment) -> None:
        """Le tracé disparaît, la course reste : c'est elle qui porte la
        rémunération du livreur et la preuve de livraison."""
        ping(course, recu_il_y_a=dt.timedelta(days=40))

        purge_stale_locations(days=30)

        assert Assignment.objects.filter(pk=course.pk).exists()

    def test_la_fenetre_par_defaut_vient_des_reglages(
        self, course: Assignment, settings: object
    ) -> None:
        settings.TRACKING_RETENTION_DAYS = 1  # type: ignore[attr-defined]
        ping(course, recu_il_y_a=dt.timedelta(days=3))

        assert purge_stale_locations() == 1
