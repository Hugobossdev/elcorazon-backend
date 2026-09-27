"""Les espèces remises au livreur sont un encaissement.

Constaté en local le 2026-09-24 : une commande payée en espèces, livrée, gardait
`amount_paid = null`. Rien n'enregistrait l'argent remis à la porte — la
transaction ouverte par `initiate` restait `processing` pour toujours. D'où :

* le back-office lisait « rien d'encaissé, rien à rembourser » sur une commande
  réglée ;
* le client ne pouvait pas savoir sa commande soldée ;
* les chiffres d'encaissement ignoraient toute l'activité en espèces.

La règle : l'espèce est encaissée **à la remise**, c'est-à-dire au passage de
la commande en `delivered`, pour le solde qui restait dû.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.orders.models import Order, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.payments.services import PaymentService
from apps.restaurants.models import Restaurant
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

JUSQU_A_LA_PORTE = (
    OrderStatus.CONFIRMED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.PICKED_UP,
    OrderStatus.ON_THE_WAY,
)


def mener(order: Order, *etapes: str) -> Order:
    for etape in etapes:
        order = OrderService.transition_to(order=order, target=etape)
    order.refresh_from_db()
    return order


def livrer(order: Order) -> Order:
    return mener(order, *JUSQU_A_LA_PORTE, OrderStatus.DELIVERED)


def commande_especes(restaurant: Restaurant, customer: User, reference: str) -> Order:
    return build_order(restaurant, customer, reference=reference, payment_method=PaymentMethod.CASH)


def test_la_livraison_encaisse_les_especes(restaurant: Restaurant, customer: User) -> None:
    order = livrer(commande_especes(restaurant, customer, "EC000201"))

    assert order.amount_paid == order.total
    especes = Transaction.objects.get(order=order)
    assert especes.provider == PaymentProvider.CASH
    assert especes.status == PaymentStatus.COMPLETED
    assert especes.amount == order.total
    assert especes.completed_at is not None


def test_la_demande_ouverte_par_le_client_est_soldee_pas_doublee(
    restaurant: Restaurant, customer: User
) -> None:
    """Le client a ouvert l'écran de règlement : `initiate` a créé une
    transaction espèces `processing`. C'est elle qui se solde."""
    order = commande_especes(restaurant, customer, "EC000202")
    ouverte, _ = PaymentService.initiate(order=order, payer=customer)

    order = livrer(order)

    assert Transaction.objects.filter(order=order).count() == 1
    ouverte.refresh_from_db()
    assert ouverte.status == PaymentStatus.COMPLETED
    assert order.amount_paid == order.total


def test_rien_avant_la_remise(restaurant: Restaurant, customer: User) -> None:
    """En route, le livreur n'a encore rien reçu."""
    order = mener(commande_especes(restaurant, customer, "EC000203"), *JUSQU_A_LA_PORTE)

    assert order.amount_paid is None
    assert not Transaction.objects.filter(order=order, status=PaymentStatus.COMPLETED).exists()


def test_un_paiement_en_ligne_impaye_n_est_pas_invente(
    restaurant: Restaurant, customer: User
) -> None:
    """Une commande mobile money livrée sans encaissement reste impayée : ce
    n'est pas le livreur qui a reçu cet argent, et le supposer le ferait
    disparaître des impayés."""
    order = livrer(
        build_order(
            restaurant, customer, reference="EC000204", payment_method=PaymentMethod.MOBILE_MONEY
        )
    )

    assert order.amount_paid is None
    assert not Transaction.objects.filter(order=order).exists()


def test_rejouer_la_livraison_n_encaisse_pas_deux_fois(
    restaurant: Restaurant, customer: User
) -> None:
    order = livrer(commande_especes(restaurant, customer, "EC000205"))

    # Un rejeu de `delivered` est un no-op de la machine — aucun signal ne
    # repart, et l'encaissé ne bouge pas.
    OrderService.transition_to(order=order, target=OrderStatus.DELIVERED)
    order.refresh_from_db()

    assert Transaction.objects.filter(order=order, status=PaymentStatus.COMPLETED).count() == 1
    assert order.amount_paid == order.total
