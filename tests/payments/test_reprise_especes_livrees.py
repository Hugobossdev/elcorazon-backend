"""Reprise de l'historique : les commandes espèces livrées avant l'encaissement.

Jusqu'au 2026-09-24, une commande espèces livrée gardait `amount_paid = null`
(voir `test_encaissement_especes.py`). La commande `encaisser_especes_livrees`
rattrape ces commandes-là, par le même chemin que l'encaissement courant.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentStatus, Transaction
from apps.payments.services import PaymentService
from apps.restaurants.models import Restaurant
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LIVREE_LE = timezone.now() - dt.timedelta(days=12)


def livree_avant_la_regle(
    restaurant: Restaurant, customer: User, reference: str, **extra: object
) -> Order:
    """Une commande livrée comme elles l'étaient avant : sans encaissement.

    Écrite directement, sans passer par `transition_to` — c'est ce qui
    reproduit l'état laissé par l'ancien code, où rien ne réagissait à la
    livraison.
    """
    defaults: dict[str, object] = {"payment_method": PaymentMethod.CASH}
    defaults.update(extra)
    order = build_order(restaurant, customer, reference=reference, **defaults)
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED, delivered_at=LIVREE_LE)
    order.refresh_from_db()
    return order


def reprendre(*options: str) -> str:
    sortie = StringIO()
    call_command("encaisser_especes_livrees", *options, stdout=sortie)
    return sortie.getvalue()


def test_sans_appliquer_rien_n_est_ecrit(restaurant: Restaurant, customer: User) -> None:
    order = livree_avant_la_regle(restaurant, customer, "EC000301")

    rapport = reprendre()

    assert "1 commande" in rapport
    order.refresh_from_db()
    assert order.amount_paid is None
    assert not Transaction.objects.filter(order=order).exists()


def test_appliquer_encaisse_a_la_date_de_livraison(restaurant: Restaurant, customer: User) -> None:
    """Datée du jour de la reprise, l'espèce tomberait dans les chiffres
    d'aujourd'hui — qui filtrent sur `created_at` — au lieu du jour où le
    livreur l'a reçue."""
    order = livree_avant_la_regle(restaurant, customer, "EC000302")

    reprendre("--appliquer")

    order.refresh_from_db()
    assert order.amount_paid == order.total
    especes = Transaction.objects.get(order=order)
    assert especes.status == PaymentStatus.COMPLETED
    assert especes.completed_at == LIVREE_LE
    assert especes.created_at == LIVREE_LE


def test_la_demande_ouverte_est_soldee_et_garde_sa_date(
    restaurant: Restaurant, customer: User
) -> None:
    order = build_order(
        restaurant, customer, reference="EC000303", payment_method=PaymentMethod.CASH
    )
    ouverte, _ = PaymentService.initiate(order=order, payer=customer)
    # Ouverte à l'écran de règlement, donc **avant** la livraison.
    ouverte_le = LIVREE_LE - dt.timedelta(minutes=40)
    Transaction.objects.filter(pk=ouverte.pk).update(created_at=ouverte_le)
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED, delivered_at=LIVREE_LE)

    reprendre("--appliquer")

    assert Transaction.objects.filter(order=order).count() == 1
    ouverte.refresh_from_db()
    assert ouverte.status == PaymentStatus.COMPLETED
    assert ouverte.completed_at == LIVREE_LE
    assert ouverte.created_at == ouverte_le


def test_une_seconde_reprise_ne_fait_rien(restaurant: Restaurant, customer: User) -> None:
    livree_avant_la_regle(restaurant, customer, "EC000304")
    reprendre("--appliquer")

    rapport = reprendre("--appliquer")

    assert "0 commande" in rapport
    assert Transaction.objects.count() == 1


def test_seules_les_especes_livrees_sont_reprises(restaurant: Restaurant, customer: User) -> None:
    livree_avant_la_regle(
        restaurant, customer, "EC000305", payment_method=PaymentMethod.MOBILE_MONEY
    )
    build_order(restaurant, customer, reference="EC000306", payment_method=PaymentMethod.CASH)

    reprendre("--appliquer")

    assert not Transaction.objects.exists()
