"""Le livreur apprend l'annulation de sa course sur **sa** file.

Constaté à l'audit de DELY, le 2026-09-25 : l'annulation d'une course était
diffusée sur le canal de la **commande** (`order_group`), que le client écoute
et que le livreur n'écoute pas. Sa file (`ws/couriers/me/`) ne portait que
`delivery.offered` et `delivery.offer_expired`. Le livreur ne l'apprenait que
par la notification poussée, ou au rechargement suivant — et l'écran de
navigation continuait de le guider vers une course qui n'était plus la sienne.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from common.realtime import courier_group, replay

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def annonces(courier: CourierProfile) -> list[tuple[str, dict[str, Any]]]:
    return [(event.type, event.payload) for event in replay(courier_group(courier.pk), since=0)]


def test_le_personnel_retire_la_course(
    order: Order,
    courier: CourierProfile,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    course = Assignment.objects.create(order=order, courier=courier)

    with django_capture_on_commit_callbacks(execute=True):
        AssignmentService.transition_to(
            assignment=course, target=DeliveryStatus.CANCELLED, reason="Réaffectée"
        )

    assert (
        "delivery.cancelled",
        {"assignment": str(course.pk), "order": str(order.pk), "reason": "Réaffectée"},
    ) in annonces(courier)


def test_la_commande_est_annulee(
    order: Order,
    courier: CourierProfile,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    course = Assignment.objects.create(order=order, courier=courier)

    with django_capture_on_commit_callbacks(execute=True):
        OrderService.cancel_by_staff(order=order, actor=courier.user, reason="Rupture")

    assert [type_ for type_, _ in annonces(courier)] == ["delivery.cancelled"]
    _, charge = annonces(courier)[0]
    assert charge["assignment"] == str(course.pk)


def test_rien_n_est_annonce_si_la_transaction_echoue(
    order: Order,
    courier: CourierProfile,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """Annoncé **après** le commit : une annulation rejetée ne doit pas faire
    quitter sa course au livreur."""
    course = Assignment.objects.create(order=order, courier=courier)

    with django_capture_on_commit_callbacks(execute=False) as rappels:
        AssignmentService.transition_to(assignment=course, target=DeliveryStatus.CANCELLED)

    assert annonces(courier) == []
    assert rappels, "la diffusion doit attendre le commit"
