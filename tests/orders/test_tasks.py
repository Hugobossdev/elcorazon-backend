"""Tâches planifiées des commandes.

Une tâche non testée est une tâche dont on découvre le comportement en
production, à trois heures du matin, quand beat la déclenche pour la première
fois. Celle-ci **supprime** des lignes : c'est la dernière qu'on veut découvrir
ainsi.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.orders.models import IdempotencyKey, Order
from apps.orders.tasks import purge_idempotency_keys

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def cle(order: Order, customer: User) -> IdempotencyKey:
    return IdempotencyKey.objects.create(
        key="cle-de-test",
        user=customer,
        endpoint="POST /api/v1/orders/",
        order=order,
        response_status=201,
        response_body={"id": str(order.pk)},
    )


class TestPurgeDesClesDIdempotence:
    def test_une_cle_perimee_est_supprimee(self, cle: IdempotencyKey) -> None:
        IdempotencyKey.objects.filter(pk=cle.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=100)
        )

        assert purge_idempotency_keys(hours=72) == 1
        assert IdempotencyKey.objects.count() == 0

    def test_une_cle_recente_survit(self, cle: IdempotencyKey) -> None:
        """La fenêtre est large exprès : un téléphone éteint dans une zone sans
        réseau peut retenter longtemps après, et c'est le doublon de commande
        qu'on cherche à empêcher."""
        assert purge_idempotency_keys(hours=72) == 0
        assert IdempotencyKey.objects.count() == 1

    def test_la_commande_survit_a_la_purge_de_sa_cle(
        self, cle: IdempotencyKey, order: Order
    ) -> None:
        """La clé pointe vers la commande ; la supprimer ne doit rien emporter
        d'autre. Une purge technique qui effacerait des écritures comptables
        serait une catastrophe silencieuse."""
        IdempotencyKey.objects.filter(pk=cle.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=100)
        )

        purge_idempotency_keys(hours=72)

        assert Order.objects.filter(pk=order.pk).exists()

    def test_la_fenetre_par_defaut_vient_des_reglages(
        self, cle: IdempotencyKey, settings: object
    ) -> None:
        """La rétention est une politique, pas une constante de code : elle se
        négocie et change sans redéploiement."""
        settings.IDEMPOTENCY_RETENTION_HOURS = 1  # type: ignore[attr-defined]
        IdempotencyKey.objects.filter(pk=cle.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=2)
        )

        assert purge_idempotency_keys() == 1
