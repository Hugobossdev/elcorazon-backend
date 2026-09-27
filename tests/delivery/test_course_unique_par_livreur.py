"""L6 — un livreur ne porte qu'une course engagée à la fois.

Cette suite reprend, en l'inversant, la preuve qui a établi le défaut : avant
correction, un livreur déjà en route restait proposé par `available_for`,
pouvait se voir offrir une seconde course et l'accepter. Il en tenait deux.

Ce qui rendait ce défaut coûteux n'est pas la base mais le client : `Dely`
n'émet ses relevés de position que pour **une** course (`activeCourse`) et son
écran de navigation ne guide que vers celle-là. Le client de la seconde commande
suivait donc un livreur immobile jusqu'à ce qu'on sonne à sa porte.

Trois barrières, testées séparément parce qu'elles échouent à des moments
différents et qu'aucune ne remplace les autres :

* `available_for` — le back-office ne le propose plus ;
* `offer` / `accept` — un refus métier, lisible, plutôt qu'une erreur de base ;
* `one_engaged_assignment_per_courier` — le dernier rempart, quand le service
  est contourné.

`offered` reste hors du champ, et c'est le point qu'il faut protéger d'une
correction trop zélée : plusieurs propositions n'occupent personne, et un livreur
qui laisse traîner une offre bloquerait sinon sa propre file.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService, CourierService
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def deux_commandes(restaurant: Restaurant, customer, order: Order) -> tuple[Order, Order]:
    """Deux commandes **prêtes** du même établissement, à confier à un livreur.

    La première est la fixture partagée ; la seconde porte une référence
    distincte, `reference` étant unique.

    Elles sont menées jusqu'à `ready`, et non plus jusqu'à `confirmed` : une
    course ne se propose que lorsque le repas peut réellement être retiré
    (`OFFERABLE_FROM`). Ce qui se joue ici — un livreur ne porte qu'une course
    — est indépendant de cette règle, mais la fixture doit la respecter pour
    atteindre le geste qu'elle veut éprouver.
    """
    seconde = build_order(restaurant, customer, reference="EC900002")
    for commande in (order, seconde):
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=commande, target=etape, actor=None)
    return order, seconde


def porte_une_course(courier: CourierProfile, commande: Order) -> Assignment:
    """Amène le livreur à l'état « engagé » sur cette commande."""
    course = AssignmentService.offer(order=commande, courier=courier)
    return AssignmentService.accept(assignment=course, courier=courier)


class TestLeBackOfficeNeLeProposePlus:
    def test_un_livreur_engage_sort_des_disponibles(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """C'est la première moitié du défaut : le superviseur voyait « Disponible »
        en face de quelqu'un qui roulait déjà."""
        premiere, seconde = deux_commandes
        assert courier in list(CourierService.available_for(seconde))

        porte_une_course(courier, premiere)

        assert courier not in list(CourierService.available_for(seconde))

    def test_une_simple_proposition_ne_le_retire_pas(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """Une offre en attente n'occupe personne.

        Sans cette nuance, un livreur qui ne répond pas à une proposition se
        rendrait lui-même indisponible pour toutes les suivantes.
        """
        premiere, seconde = deux_commandes
        AssignmentService.offer(order=premiere, courier=courier)

        assert courier in list(CourierService.available_for(seconde))

    def test_une_course_livree_le_rend_de_nouveau_disponible(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """L'exclusion dure le temps de la course, pas au-delà."""
        premiere, seconde = deux_commandes
        course = porte_une_course(courier, premiere)
        for etape in (
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.ON_THE_WAY,
            DeliveryStatus.DELIVERED,
        ):
            course = AssignmentService.transition_to(assignment=course, target=etape, actor=None)

        assert courier in list(CourierService.available_for(seconde))


class TestLeRefusEstMetier:
    def test_proposer_une_seconde_course_est_refuse(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        premiere, seconde = deux_commandes
        engagee = porte_une_course(courier, premiere)

        with pytest.raises(BusinessRuleViolation) as refus:
            AssignmentService.offer(order=seconde, courier=courier)

        # Le motif doit désigner la course qui occupe : sans elle, le superviseur
        # ne sait pas quoi attendre ni qui rappeler.
        assert refus.value.extra["assignment_id"] == str(engagee.pk)

    def test_accepter_une_seconde_proposition_est_refuse(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """Le cas réel : deux offres reçues, deux fois le doigt sur l'écran.

        La seconde offre est posée **avant** l'acceptation de la première, ce que
        rien n'interdit — c'est bien l'acceptation qui doit refuser.
        """
        premiere, seconde = deux_commandes
        offre_a = AssignmentService.offer(order=premiere, courier=courier)
        offre_b = AssignmentService.offer(order=seconde, courier=courier)

        AssignmentService.accept(assignment=offre_a, courier=courier)

        with pytest.raises(BusinessRuleViolation):
            AssignmentService.accept(assignment=offre_b, courier=courier)

    def test_la_seconde_offre_reste_refusable(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """Refuser doit rester possible : sinon l'offre resterait en suspens et la
        commande sans porteur, personne ne pouvant la reproposer."""
        premiere, seconde = deux_commandes
        offre_a = AssignmentService.offer(order=premiere, courier=courier)
        offre_b = AssignmentService.offer(order=seconde, courier=courier)
        AssignmentService.accept(assignment=offre_a, courier=courier)

        refusee = AssignmentService.decline(
            assignment=offre_b, courier=courier, reason="Déjà en course"
        )

        assert refusee.status == DeliveryStatus.DECLINED


class TestLaBaseTientEncore:
    """Le service contourné — script d'exploitation, correctif à chaud."""

    def test_deux_courses_engagees_sont_impossibles(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        premiere, seconde = deux_commandes
        porte_une_course(courier, premiere)

        with pytest.raises(IntegrityError), transaction.atomic():
            Assignment.objects.create(
                order=seconde, courier=courier, status=DeliveryStatus.ACCEPTED
            )

    def test_deux_propositions_restent_possibles(
        self, courier: CourierProfile, deux_commandes: tuple[Order, Order]
    ) -> None:
        """La contrainte ne doit pas déborder sur `offered`."""
        premiere, seconde = deux_commandes
        AssignmentService.offer(order=premiere, courier=courier)
        AssignmentService.offer(order=seconde, courier=courier)

        assert (
            Assignment.objects.filter(courier=courier, status=DeliveryStatus.OFFERED).count() == 2
        )
