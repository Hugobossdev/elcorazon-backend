"""Une commande annulée n'a plus de course à prendre.

Constaté en local le 2026-09-24, pendant le test de bout en bout du parcours
client : une commande annulée depuis le back-office gardait sa course
« proposée », et le livreur à qui elle l'était pouvait encore l'**accepter**
(200). Deux trous, indépendants :

* `AssignmentService.accept` verrouillait la commande sans jamais lire son
  statut — la garde « commande annulée » ne vivait que dans `transition_to`,
  qui sert aux étapes suivantes ;
* l'annulation d'une commande ne refermait pas la course encore ouverte, qui
  restait dans la liste du livreur.
"""

from __future__ import annotations

import pytest

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from common.exceptions import BusinessRuleViolation

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def test_on_n_accepte_pas_la_course_d_une_commande_annulee(
    order: Order, courier: CourierProfile
) -> None:
    course = Assignment.objects.create(order=order, courier=courier)
    # Annulée sans passer par le service : c'est l'acceptation seule qu'on
    # éprouve ici, pas la fermeture automatique.
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED)

    with pytest.raises(BusinessRuleViolation, match="annulée"):
        AssignmentService.accept(assignment=course, courier=courier)

    course.refresh_from_db()
    assert course.status == DeliveryStatus.OFFERED


def test_annuler_la_commande_referme_sa_course_ouverte(
    order: Order, courier: CourierProfile
) -> None:
    course = Assignment.objects.create(order=order, courier=courier)

    OrderService.cancel_by_staff(order=order, actor=courier.user, reason="Rupture en cuisine")

    course.refresh_from_db()
    assert course.status == DeliveryStatus.CANCELLED
    assert course.decline_reason


def test_la_fermeture_ne_compte_pas_contre_le_livreur(
    order: Order, courier: CourierProfile
) -> None:
    """Le livreur n'y est pour rien : son compteur d'annulations, qui sert à
    juger sa fiabilité, ne doit pas bouger."""
    Assignment.objects.create(order=order, courier=courier)
    avant = courier.deliveries_cancelled

    OrderService.cancel_by_staff(order=order, actor=courier.user, reason="Client injoignable")

    courier.refresh_from_db()
    assert courier.deliveries_cancelled == avant


def test_une_course_deja_close_n_est_pas_touchee(order: Order, courier: CourierProfile) -> None:
    course = Assignment.objects.create(
        order=order, courier=courier, status=DeliveryStatus.DECLINED, decline_reason="Trop loin"
    )

    OrderService.cancel_by_staff(order=order, actor=courier.user, reason="Rupture en cuisine")

    course.refresh_from_db()
    assert course.status == DeliveryStatus.DECLINED
    assert course.decline_reason == "Trop loin"
