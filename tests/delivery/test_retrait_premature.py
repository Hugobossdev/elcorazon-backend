"""Une course n'avance pas sans sa commande.

## Le défaut que cette suite ferme

`allowed_transitions` est calculé sur la seule machine de la **course**. Le
bouton « J'ai récupéré la commande » s'affichait donc dès l'acceptation, quelle
que soit l'avancée de la cuisine. Le serveur acceptait le geste, puis la
projection constatait que `preparing → picked_up` n'était pas jouable et
**retournait en silence**.

La course poursuivait alors sa vie — récupérée, en route, livrée, livreur
crédité — pendant que la commande restait « en préparation ». Le client ne
voyait jamais sa livraison, ses points de fidélité n'étaient pas crédités (ils
le sont à la livraison de la *commande*), et le personnel pouvait encore
annuler un repas déjà remis.

## Les deux barrières, testées séparément

* la **proposition** ne part que d'une commande prête (`OFFERABLE_FROM`) ;
* la **transition** refuse toute étape que la commande ne peut pas suivre
  (`AssignmentService._exiger_une_commande_qui_suit`).

La seconde ne devient inutile que si la première est parfaite, et c'est
précisément ce qu'on ne veut pas supposer : une reprise de données, un ancien
enregistrement ou un futur assouplissement de la proposition remettraient le
défaut en place. Elles ne se remplacent pas.
"""

from __future__ import annotations

import pytest

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def commande(restaurant: Restaurant, customer, statut: str, reference: str) -> Order:
    """Une commande de cet établissement, posée directement dans l'état voulu."""
    return build_order(
        restaurant,
        customer,
        reference=reference,
        status=statut,
        delivery_zone=restaurant.zone,
        delivery_zone_name=restaurant.zone.name,
        city=restaurant.zone.city,
        country=restaurant.zone.city.country,
    )


def course_acceptee(commande_prete: Order, courier: CourierProfile) -> Assignment:
    offre = AssignmentService.offer(order=commande_prete, courier=courier)
    return AssignmentService.accept(assignment=offre, courier=courier)


class TestLaPropositionAttendLaCuisine:
    """Une course ne se propose que lorsque le repas peut être retiré."""

    @pytest.mark.parametrize(
        "statut",
        [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING],
    )
    def test_une_commande_en_cuisine_ne_se_propose_pas(
        self, restaurant: Restaurant, customer, courier: CourierProfile, statut: str
    ) -> None:
        en_cuisine = commande(restaurant, customer, statut, f"EC4000{statut[:2]}")

        with pytest.raises(BusinessRuleViolation) as refus:
            AssignmentService.offer(order=en_cuisine, courier=courier)

        assert "prêt à être retiré" in str(refus.value)
        assert not Assignment.objects.filter(order=en_cuisine).exists()

    def test_une_commande_prete_se_propose(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        prete = commande(restaurant, customer, OrderStatus.READY, "EC400010")

        course = AssignmentService.offer(order=prete, courier=courier)

        assert course.status == DeliveryStatus.OFFERED


class TestLeRetraitAttendLaCuisine:
    """La garde de transition, éprouvée sur une course déjà acceptée.

    La commande est ramenée en préparation **après** l'acceptation : c'est le
    seul moyen de reproduire l'état que l'ancienne règle de proposition
    laissait exister, et c'est aussi ce qu'une reprise de données produirait.
    """

    def test_recuperer_un_repas_encore_en_cuisine_est_refuse(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, "EC400020"), courier
        )
        Order.objects.filter(pk=course.order_id).update(status=OrderStatus.PREPARING)
        course.refresh_from_db()

        with pytest.raises(BusinessRuleViolation) as refus:
            AssignmentService.transition_to(assignment=course, target=DeliveryStatus.PICKED_UP)

        assert "pas encore déclaré cette commande prête" in str(refus.value)
        # Le motif est **exploitable** : l'application sait quoi dire, et sur
        # quoi porte l'attente.
        assert refus.value.extra["order_status"] == OrderStatus.PREPARING
        assert refus.value.extra["expected_order_status"] == OrderStatus.PICKED_UP

    def test_la_course_ne_bouge_pas_sur_un_refus(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        # Rien ne doit rester écrit : ni l'étape, ni l'horodatage de retrait.
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, "EC400021"), courier
        )
        Order.objects.filter(pk=course.order_id).update(status=OrderStatus.PREPARING)
        course.refresh_from_db()

        with pytest.raises(BusinessRuleViolation):
            AssignmentService.transition_to(assignment=course, target=DeliveryStatus.PICKED_UP)

        course.refresh_from_db()
        assert course.status == DeliveryStatus.ACCEPTED
        assert course.picked_up_at is None

    def test_une_commande_prete_se_recupere(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, "EC400022"), courier
        )

        retiree = AssignmentService.transition_to(
            assignment=course, target=DeliveryStatus.PICKED_UP
        )

        retiree.order.refresh_from_db()
        assert retiree.status == DeliveryStatus.PICKED_UP
        assert retiree.order.status == OrderStatus.PICKED_UP


class TestLaLivraisonSuitLaCommande:
    """Les deux dernières étapes, sur une commande désynchronisée de force.

    En service normal, la garde du retrait suffit : chaque étape projette, donc
    les deux restent d'accord. Ces tests forcent l'écart en base — ce qu'une
    reprise de données ou une écriture hors service produirait — pour vérifier
    que la course refuse de finir seule, plutôt que de se terminer sur une
    commande restée en arrière.
    """

    def course_en_route(
        self, restaurant: Restaurant, customer, courier: CourierProfile, reference: str
    ) -> Assignment:
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, reference), courier
        )
        course = AssignmentService.transition_to(assignment=course, target=DeliveryStatus.PICKED_UP)
        return AssignmentService.transition_to(assignment=course, target=DeliveryStatus.ON_THE_WAY)

    def test_livrer_une_commande_qui_n_est_pas_partie_est_refuse(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        course = self.course_en_route(restaurant, customer, courier, "EC400030")
        Order.objects.filter(pk=course.order_id).update(status=OrderStatus.READY)
        course.refresh_from_db()

        with pytest.raises(BusinessRuleViolation) as refus:
            AssignmentService.transition_to(assignment=course, target=DeliveryStatus.DELIVERED)

        assert "partie en livraison" in str(refus.value)
        course.refresh_from_db()
        assert course.status == DeliveryStatus.ON_THE_WAY
        assert course.delivered_at is None

    def test_le_livreur_n_est_pas_credite_sur_un_refus(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        # C'est l'autre moitié du défaut : la course se terminait, et le
        # livreur était payé, pour une commande que le client voyait encore en
        # préparation.
        course = self.course_en_route(restaurant, customer, courier, "EC400031")
        Order.objects.filter(pk=course.order_id).update(status=OrderStatus.READY)
        course.refresh_from_db()
        livrees = courier.deliveries_completed

        with pytest.raises(BusinessRuleViolation):
            AssignmentService.transition_to(assignment=course, target=DeliveryStatus.DELIVERED)

        courier.refresh_from_db()
        assert courier.deliveries_completed == livrees

    def test_le_parcours_complet_laisse_les_deux_d_accord(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        """L'invariant de sortie : course livrée ⇔ commande livrée."""
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, "EC400032"), courier
        )

        for etape in (
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.ON_THE_WAY,
            DeliveryStatus.DELIVERED,
        ):
            course = AssignmentService.transition_to(assignment=course, target=etape)

        course.order.refresh_from_db()
        courier.refresh_from_db()
        assert course.status == DeliveryStatus.DELIVERED
        assert course.order.status == OrderStatus.DELIVERED
        assert course.order.delivered_at is not None
        assert courier.deliveries_completed == 1


class TestCeQueLaGardeNeCassePas:
    def test_une_commande_menee_a_la_main_ne_bloque_pas_la_course(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        """Le personnel a fait avancer la commande ; la course doit suivre.

        Sans cette tolérance, une commande passée en `picked_up` depuis le
        back-office rendrait la course du livreur définitivement bloquée : sa
        propre étape ne serait plus jouable, et il n'aurait aucun moyen de
        terminer une livraison qu'il est pourtant en train de faire.
        """
        course = course_acceptee(
            commande(restaurant, customer, OrderStatus.READY, "EC400040"), courier
        )
        OrderService.transition_to(order=course.order, target=OrderStatus.PICKED_UP, actor=None)

        retiree = AssignmentService.transition_to(
            assignment=course, target=DeliveryStatus.PICKED_UP
        )

        retiree.order.refresh_from_db()
        assert retiree.status == DeliveryStatus.PICKED_UP
        assert retiree.order.status == OrderStatus.PICKED_UP

    def test_refuser_une_proposition_reste_possible(
        self, restaurant: Restaurant, customer, courier: CourierProfile
    ) -> None:
        """Décliner et annuler ne projettent rien : la garde ne les regarde pas."""
        prete = commande(restaurant, customer, OrderStatus.READY, "EC400041")
        offre = AssignmentService.offer(order=prete, courier=courier)

        refusee = AssignmentService.decline(assignment=offre, courier=courier, reason="Trop loin")

        assert refusee.status == DeliveryStatus.DECLINED
