"""Commandes payables en ligne restées impayées — elles ne vivent pas toujours.

Une commande mobile money ou carte naît `pending` et n'est confirmée que par
l'encaissement. Rien ne la faisait sortir de là si le client abandonnait : elle
restait « en attente » pour toujours, gardait son stock réservé, et le suivi du
client annonçait une commande qui ne serait jamais préparée.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, SplitPayment, Transaction
from apps.payments.tasks import expire_unpaid_orders
from apps.restaurants.models import Restaurant
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

TTL = 30


@pytest.fixture(autouse=True)
def delai(settings: object) -> None:
    settings.PAYMENT_UNPAID_ORDER_TTL_MINUTES = TTL  # type: ignore[attr-defined]


def vieillir(order: Order, minutes: int) -> Order:
    Order.objects.filter(pk=order.pk).update(
        placed_at=timezone.now() - dt.timedelta(minutes=minutes)
    )
    order.refresh_from_db()
    return order


def commande(restaurant: Restaurant, customer: User, reference: str, **extra: object) -> Order:
    return build_order(restaurant, customer, reference=reference, **extra)


def test_une_commande_en_ligne_impayee_au_dela_du_delai_est_annulee(
    restaurant: Restaurant, customer: User
) -> None:
    order = vieillir(commande(restaurant, customer, "EC000101"), TTL + 1)

    assert expire_unpaid_orders() == 1

    order.refresh_from_db()
    assert order.status == OrderStatus.CANCELLED
    assert "Paiement non reçu" in order.cancellation_reason


def test_une_commande_recente_attend_encore(restaurant: Restaurant, customer: User) -> None:
    order = vieillir(commande(restaurant, customer, "EC000102"), TTL - 5)

    assert expire_unpaid_orders() == 0
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING


def test_les_especes_ne_sont_pas_concernees(restaurant: Restaurant, customer: User) -> None:
    """Payées à la remise : l'absence d'encaissement est leur état normal."""
    order = vieillir(
        commande(restaurant, customer, "EC000103", payment_method=PaymentMethod.CASH), TTL * 4
    )

    assert expire_unpaid_orders() == 0
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING


def test_un_paiement_ouvert_recemment_laisse_le_temps_de_finir(
    restaurant: Restaurant, customer: User
) -> None:
    """Le client qui a ouvert son paiement à la vingt-neuvième minute n'est
    pas interrompu à la trentième : le délai court depuis sa dernière tentative."""
    order = vieillir(commande(restaurant, customer, "EC000104"), TTL * 2)
    Transaction.objects.create(
        order=order,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference="PD-EN-COURS-104",
        amount=order.total,
        status=PaymentStatus.PROCESSING,
    )

    assert expire_unpaid_orders() == 0
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING


def test_une_commande_partagee_suit_son_propre_rythme(
    restaurant: Restaurant, customer: User
) -> None:
    """Chaque convive règle sa part à son heure : le partage a son échéance."""
    order = vieillir(commande(restaurant, customer, "EC000105"), TTL * 2)
    SplitPayment.objects.create(order=order, initiated_by=customer, total_amount=order.total)

    assert expire_unpaid_orders() == 0


def test_une_commande_confirmee_n_est_jamais_touchee(
    restaurant: Restaurant, customer: User
) -> None:
    order = vieillir(
        commande(restaurant, customer, "EC000106", status=OrderStatus.CONFIRMED), TTL * 2
    )

    assert expire_unpaid_orders() == 0
    order.refresh_from_db()
    assert order.status == OrderStatus.CONFIRMED
