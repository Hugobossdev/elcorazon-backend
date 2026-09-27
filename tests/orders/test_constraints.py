"""Contraintes de base sur les commandes — ADR-010.

Ces tests ne vérifient pas le code applicatif : ils vérifient que **PostgreSQL**
refuse les données invalides. C'est la dernière ligne de défense, celle qui tient
encore quand un script d'exploitation, un correctif à chaud ou une régression
future contourne les services.

Le contournement est explicite dans chaque test — `QuerySet.update()`, qui
n'appelle ni `save()`, ni `full_clean()`, ni le moindre code métier. Si la base
laisse passer, l'invariant n'est pas réellement défendu.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.orders.models import IdempotencyKey, Order, OrderLine
from apps.orders.states import ORDER_MACHINE, OrderStatus
from common.money import Money
from tests.fixtures import XOF, build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


class TestEnumerationDesStatuts:
    """C4 — le code et le schéma ne peuvent pas diverger."""

    def test_la_contrainte_couvre_exactement_la_machine(self, order: Order) -> None:
        """Chaque statut déclaré doit être acceptable en base."""
        for status in ORDER_MACHINE.states:
            Order.objects.filter(pk=order.pk).update(status=status)
            order.refresh_from_db()
            assert order.status == status

    def test_un_statut_hors_enumeration_est_rejete(self, order: Order) -> None:
        """L'ancien code écrivait `accepted`, absent de l'énumération. Il
        n'aurait pas franchi cette contrainte."""
        with pytest.raises(IntegrityError, match="order_status_in_enum"), transaction.atomic():
            Order.objects.filter(pk=order.pk).update(status="accepted")

    def test_une_chaine_vide_est_rejetee(self, order: Order) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Order.objects.filter(pk=order.pk).update(status="")


class TestMontants:
    def test_un_montant_negatif_est_rejete(self, order: Order) -> None:
        with pytest.raises(IntegrityError, match="not_negative"), transaction.atomic():
            Order.objects.filter(pk=order.pk).update(subtotal_minor=-1)

    def test_une_remise_superieure_au_du_est_rejetee(self, order: Order) -> None:
        """Sans cette borne, le total devient négatif : la « commande »
        rapporterait de l'argent au client."""
        with pytest.raises(IntegrityError, match="discount_within_bounds"), transaction.atomic():
            # sous-total 3 500 + frais 500 = 4 000 dus
            Order.objects.filter(pk=order.pk).update(discount_minor=4_001)

    def test_une_remise_egale_au_du_est_acceptee(self, order: Order) -> None:
        """Le franco intégral est un cas métier légitime, pas une anomalie."""
        Order.objects.filter(pk=order.pk).update(discount_minor=4_000)
        order.refresh_from_db()
        assert order.discount == Money(4_000, XOF)

    def test_les_montants_portent_leur_devise(self, order: Order) -> None:
        order.refresh_from_db()
        assert order.total == Money(4_000, XOF)
        assert order.total.currency == "XOF"


class TestLignes:
    def test_une_quantite_nulle_est_rejetee(self, order: Order, menu_item) -> None:
        line = OrderLine.objects.create(
            order=order,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=1,
            line_total=Money(3_500, XOF),
        )
        with pytest.raises(IntegrityError, match="quantity_positive"), transaction.atomic():
            OrderLine.objects.filter(pk=line.pk).update(quantity=0)

    def test_la_ligne_est_un_instantane(self, order: Order, menu_item) -> None:
        """C1/C2 — changer le prix au catalogue ne réécrit pas l'histoire."""
        line = OrderLine.objects.create(
            order=order,
            menu_item=menu_item,
            item_name=menu_item.name,
            unit_price=menu_item.price,
            quantity=1,
            line_total=menu_item.price,
        )

        menu_item.price = Money(9_999, XOF)
        menu_item.name = "Burger Corazón — nouvelle recette"
        menu_item.save()

        line.refresh_from_db()
        assert line.unit_price == Money(3_500, XOF)
        assert line.item_name == "Burger Corazón"

    def test_un_article_retire_du_catalogue_laisse_la_commande_lisible(
        self, order: Order, menu_item
    ) -> None:
        """Suppression logique : l'historique reste cohérent."""
        line = OrderLine.objects.create(
            order=order,
            menu_item=menu_item,
            item_name=menu_item.name,
            unit_price=menu_item.price,
            quantity=2,
            line_total=Money(7_000, XOF),
        )

        menu_item.delete()  # logique

        line.refresh_from_db()
        assert line.item_name == "Burger Corazón"
        assert line.menu_item.is_deleted


class TestIdempotence:
    """ADR-009 — un client mobile qui perd le réseau retente."""

    def test_la_meme_cle_ne_passe_pas_deux_fois(self, order: Order, customer) -> None:
        payload = {
            "key": "abc-123",
            "user": customer,
            "endpoint": "POST /api/v1/orders/",
            "order": order,
            "response_status": 201,
            "response_body": {"id": str(order.pk)},
        }
        IdempotencyKey.objects.create(**payload)

        with pytest.raises(IntegrityError, match="idempotency_key"), transaction.atomic():
            IdempotencyKey.objects.create(**payload)

    def test_la_cle_est_portee_a_l_utilisateur(self, order: Order, customer, courier_user) -> None:
        """Deux clients peuvent tirer la même clé sans se gêner — et personne
        ne peut lire la réponse d'autrui en devinant sa clé."""
        base = {
            "key": "collision",
            "endpoint": "POST /api/v1/orders/",
            "response_status": 201,
            "response_body": {},
        }
        IdempotencyKey.objects.create(user=customer, order=order, **base)
        IdempotencyKey.objects.create(user=courier_user, **base)

        assert IdempotencyKey.objects.filter(key="collision").count() == 2


class TestReference:
    def test_la_reference_est_unique(self, restaurant, customer) -> None:
        build_order(restaurant, customer, reference="EC000042")
        with pytest.raises(IntegrityError), transaction.atomic():
            build_order(restaurant, customer, reference="EC000042")


class TestProprietesDerivees:
    @pytest.mark.parametrize(
        ("status", "annulable"),
        [
            (OrderStatus.PENDING, True),
            (OrderStatus.READY, True),
            (OrderStatus.PICKED_UP, False),  # le repas est parti
            (OrderStatus.ON_THE_WAY, False),
            (OrderStatus.DELIVERED, False),
            (OrderStatus.CANCELLED, False),
        ],
    )
    def test_annulabilite(self, order: Order, status: str, annulable: bool) -> None:
        Order.objects.filter(pk=order.pk).update(status=status)
        order.refresh_from_db()
        assert order.is_cancellable is annulable

    def test_terminalite(self, order: Order) -> None:
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED)
        order.refresh_from_db()
        assert order.is_terminal
