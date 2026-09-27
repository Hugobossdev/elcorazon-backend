"""Livreur compatible → proposition → acceptation, sans superviseur au passe.

Ce que cette suite verrouille :

* **le périmètre de zone.** Un livreur affecté à « Cocody » ne reçoit pas une
  course pour « Yopougon », même partie de sa cuisine — ni par la liste des
  disponibles, ni par une proposition manuelle ;
* **l'affectation automatique.** Une commande prête est proposée au meilleur
  livreur compatible ; un refus, une proposition sans réponse, un livreur qui se
  met en ligne relancent la recherche ; une cuisine en affectation manuelle
  n'est pas touchée ;
* **ce que le livreur voit.** Le montant à encaisser en espèces, les consignes,
  la zone, le contenu du sac — et le numéro du client seulement quand il porte
  la course.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.dispatch import EXPIRED_OFFER_REASON, DispatchService
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.services import AssignmentService, CourierService
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.geography.models import City, DeliveryZone
from apps.orders.models import Order, OrderLine, PaymentMethod
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.payments.services import report_settled_total
from apps.restaurants.models import Restaurant, StaffMembership
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.serializers import MoneyField
from tests.fixtures import LOME, build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


# ================================================================ outillage


def zone_de(city: City, nom: str, centre: Point, **kw: Any) -> DeliveryZone:
    x, y = centre.x, centre.y
    d = 0.02
    return DeliveryZone.objects.create(
        city=city,
        name=nom,
        boundary=MultiPolygon(
            Polygon(
                ((x - d, y - d), (x + d, y - d), (x + d, y + d), (x - d, y + d), (x - d, y - d))
            ),
            srid=4326,
        ),
        base_fee=Money(500, XOF),
        fee_per_km=Money(0, XOF),
        **kw,
    )


def livreur(
    restaurant: Restaurant, email: str, position: Point | None = None, **kw: Any
) -> CourierProfile:
    user = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.COURIER
    )
    return CourierProfile.objects.create(
        user=user,
        restaurant=restaurant,
        vehicle_type=VehicleType.MOTORCYCLE,
        verification_status=kw.pop("verification_status", VerificationStatus.APPROVED),
        is_online=kw.pop("is_online", True),
        last_location=position,
        **kw,
    )


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def cocody(city: City) -> DeliveryZone:
    return zone_de(city, "Cocody", Point(1.20, 6.12, srid=4326))


@pytest.fixture
def yopougon(city: City) -> DeliveryZone:
    return zone_de(city, "Yopougon", Point(1.27, 6.18, srid=4326))


def commande_prete(
    restaurant: Restaurant, customer: User, zone: DeliveryZone | None, reference: str, **kw: Any
) -> Order:
    return build_order(
        restaurant,
        customer,
        reference=reference,
        status=OrderStatus.READY,
        delivery_zone=zone,
        delivery_zone_name=zone.name if zone else "",
        city=restaurant.zone.city,
        country=restaurant.zone.city.country,
        **kw,
    )


# =================================================== le périmètre de zone


class TestPerimetreDeZone:
    def test_un_livreur_sans_zone_roule_partout_ou_sa_cuisine_livre(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile, cocody: DeliveryZone
    ) -> None:
        commande = commande_prete(restaurant, customer, cocody, "EC100001")

        assert courier in CourierService.available_for(commande)

    def test_un_livreur_de_cocody_ne_voit_pas_une_course_de_yopougon(
        self,
        restaurant: Restaurant,
        customer: User,
        courier: CourierProfile,
        cocody: DeliveryZone,
        yopougon: DeliveryZone,
    ) -> None:
        CourierService.set_service_zones(courier=courier, zones=[cocody])
        pour_cocody = commande_prete(restaurant, customer, cocody, "EC100002")
        pour_yopougon = commande_prete(restaurant, customer, yopougon, "EC100003")

        assert courier in CourierService.available_for(pour_cocody)
        assert courier not in CourierService.available_for(pour_yopougon)

    def test_la_proposition_manuelle_hors_zone_est_refusee(
        self,
        restaurant: Restaurant,
        customer: User,
        courier: CourierProfile,
        cocody: DeliveryZone,
        yopougon: DeliveryZone,
    ) -> None:
        CourierService.set_service_zones(courier=courier, zones=[cocody])
        commande = commande_prete(restaurant, customer, yopougon, "EC100004")

        with pytest.raises(BusinessRuleViolation, match="Yopougon"):
            AssignmentService.offer(order=commande, courier=courier)
        assert not Assignment.objects.exists()

    def test_une_commande_sans_zone_connue_ne_part_pas_chez_un_livreur_restreint(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile, cocody: DeliveryZone
    ) -> None:
        CourierService.set_service_zones(courier=courier, zones=[cocody])
        ancienne = commande_prete(restaurant, customer, None, "EC100005")

        assert courier not in CourierService.available_for(ancienne)

    def test_une_zone_d_une_autre_ville_est_refusee_sans_rien_ecrire(
        self, restaurant: Restaurant, courier: CourierProfile, cocody: DeliveryZone, city: City
    ) -> None:
        kara = City.objects.create(
            country=city.country, name="Kara", slug="kara", centroid=Point(1.19, 9.55, srid=4326)
        )
        zone_kara = zone_de(kara, "Kara centre", Point(1.19, 9.55, srid=4326))

        with pytest.raises(BusinessRuleViolation, match="Kara centre"):
            CourierService.set_service_zones(courier=courier, zones=[cocody, zone_kara])
        assert courier.service_zones.count() == 0

    def test_la_zone_propre_d_une_autre_cuisine_est_refusee(
        self, restaurant: Restaurant, courier: CourierProfile, city: City, zone: DeliveryZone
    ) -> None:
        autre = Restaurant.objects.create(
            name="Autre",
            slug="autre-cuisine",
            zone=zone,
            address="x",
            location=LOME,
            phone="+22890000031",
        )
        sienne = zone_de(city, "Zone d'Autre", Point(1.22, 6.14, srid=4326), restaurant=autre)

        with pytest.raises(BusinessRuleViolation):
            CourierService.set_service_zones(courier=courier, zones=[sienne])

    def test_la_route_du_back_office_affecte_et_leve_la_restriction(
        self, restaurant: Restaurant, courier: CourierProfile, cocody: DeliveryZone
    ) -> None:
        membre = User.objects.create_user(
            "flotte@elcorazon.test", "motdepasse", full_name="Flotte", user_type=UserType.STAFF
        )
        membre.roles.add(
            Role.objects.create(name="Flotte", permissions=["couriers.read", "couriers.write"])
        )
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
        client = connecte(membre)
        url = reverse("v1:delivery:courier-zones", args=[courier.pk])

        affecte = client.post(url, {"zones": [str(cocody.pk)]}, format="json")
        assert affecte.status_code == status.HTTP_200_OK, affecte.data
        assert affecte.data["service_zones"] == [{"id": str(cocody.pk), "name": "Cocody"}]

        leve = client.post(url, {"zones": []}, format="json")
        assert leve.status_code == status.HTTP_200_OK
        assert leve.data["service_zones"] == []

    def test_sans_couriers_write_la_route_est_refusee(
        self, restaurant: Restaurant, courier: CourierProfile, cocody: DeliveryZone
    ) -> None:
        lecteur = User.objects.create_user(
            "lecteur.flotte@elcorazon.test", "motdepasse", full_name="L", user_type=UserType.STAFF
        )
        lecteur.roles.add(Role.objects.create(name="Lecture flotte", permissions=["couriers.read"]))
        StaffMembership.objects.create(user=lecteur, restaurant=restaurant)

        reponse = connecte(lecteur).post(
            reverse("v1:delivery:courier-zones", args=[courier.pk]),
            {"zones": [str(cocody.pk)]},
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN
        assert courier.service_zones.count() == 0


# ================================================ l'affectation automatique


class TestAffectationAutomatique:
    def test_une_commande_prete_est_proposee_au_plus_proche(
        self,
        restaurant: Restaurant,
        customer: User,
        cocody: DeliveryZone,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        loin = livreur(restaurant, "loin@elcorazon.test", Point(1.40, 6.30, srid=4326))
        pres = livreur(restaurant, "pres@elcorazon.test", Point(1.2260, 6.1320, srid=4326))
        commande = build_order(
            restaurant,
            customer,
            reference="EC200001",
            status=OrderStatus.PREPARING,
            delivery_zone=cocody,
            city=restaurant.zone.city,
            country=restaurant.zone.city.country,
        )

        with django_capture_on_commit_callbacks(execute=True):
            OrderService.transition_to(order=commande, target=OrderStatus.READY)

        course = Assignment.objects.get(order=commande)
        assert course.courier == pres
        assert course.status == DeliveryStatus.OFFERED
        assert loin.assignments.count() == 0

    def test_le_livreur_hors_zone_est_saute(
        self,
        restaurant: Restaurant,
        customer: User,
        cocody: DeliveryZone,
        yopougon: DeliveryZone,
    ) -> None:
        pres_mais_ailleurs = livreur(restaurant, "ailleurs@elcorazon.test", LOME)
        CourierService.set_service_zones(courier=pres_mais_ailleurs, zones=[yopougon])
        compatible = livreur(restaurant, "compatible@elcorazon.test", Point(1.30, 6.20, srid=4326))
        commande = commande_prete(restaurant, customer, cocody, "EC200002")

        course = DispatchService.dispatch(commande.pk)

        assert course is not None
        assert course.courier == compatible

    def test_un_refus_propose_au_suivant(
        self,
        restaurant: Restaurant,
        customer: User,
        cocody: DeliveryZone,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        premier = livreur(restaurant, "premier@elcorazon.test", LOME)
        second = livreur(restaurant, "second@elcorazon.test", Point(1.30, 6.20, srid=4326))
        commande = commande_prete(restaurant, customer, cocody, "EC200003")
        course = DispatchService.dispatch(commande.pk)
        assert course is not None and course.courier == premier

        with django_capture_on_commit_callbacks(execute=True):
            AssignmentService.decline(assignment=course, courier=premier, reason="crevaison")

        relance = Assignment.objects.get(order=commande, status=DeliveryStatus.OFFERED)
        assert relance.courier == second

    def test_seul_en_ligne_celui_qui_a_refuse_la_recoit_de_nouveau(
        self, restaurant: Restaurant, customer: User, cocody: DeliveryZone
    ) -> None:
        """L'exclure laisserait la commande sans personne, indéfiniment."""
        seul = livreur(restaurant, "seul@elcorazon.test", LOME)
        commande = commande_prete(restaurant, customer, cocody, "EC200004")
        course = DispatchService.dispatch(commande.pk)
        assert course is not None
        AssignmentService.decline(assignment=course, courier=seul)

        relance = DispatchService.dispatch(commande.pk)

        assert relance is not None and relance.courier == seul

    @override_settings(DELIVERY_OFFER_TTL_SECONDS=90)
    def test_une_proposition_sans_reponse_passe_au_suivant(
        self, restaurant: Restaurant, customer: User, cocody: DeliveryZone
    ) -> None:
        dormeur = livreur(restaurant, "dormeur@elcorazon.test", LOME)
        eveille = livreur(restaurant, "eveille@elcorazon.test", Point(1.30, 6.20, srid=4326))
        commande = commande_prete(restaurant, customer, cocody, "EC200005")
        course = DispatchService.dispatch(commande.pk)
        assert course is not None and course.courier == dormeur
        Assignment.objects.filter(pk=course.pk).update(
            offered_at=timezone.now() - dt.timedelta(seconds=120)
        )

        closes = DispatchService.expire_stale_offers()

        course.refresh_from_db()
        assert closes == 1
        assert course.status == DeliveryStatus.DECLINED
        assert course.decline_reason == EXPIRED_OFFER_REASON
        assert (
            Assignment.objects.get(order=commande, status=DeliveryStatus.OFFERED).courier == eveille
        )

    @override_settings(DELIVERY_OFFER_TTL_SECONDS=90)
    def test_une_proposition_recente_n_expire_pas(
        self, restaurant: Restaurant, customer: User, cocody: DeliveryZone
    ) -> None:
        livreur(restaurant, "recent@elcorazon.test", LOME)
        commande = commande_prete(restaurant, customer, cocody, "EC200006")
        course = DispatchService.dispatch(commande.pk)

        assert DispatchService.expire_stale_offers() == 0
        assert course is not None
        course.refresh_from_db()
        assert course.status == DeliveryStatus.OFFERED

    def test_une_acceptation_concurrente_n_est_pas_expiree(
        self, restaurant: Restaurant, customer: User, cocody: DeliveryZone
    ) -> None:
        rapide = livreur(restaurant, "rapide@elcorazon.test", LOME)
        commande = commande_prete(restaurant, customer, cocody, "EC200007")
        course = DispatchService.dispatch(commande.pk)
        assert course is not None
        AssignmentService.accept(assignment=course, courier=rapide)

        assert DispatchService.expire(course.pk) is False
        course.refresh_from_db()
        assert course.status == DeliveryStatus.ACCEPTED

    def test_un_livreur_qui_se_met_en_ligne_recoit_la_commande_en_attente(
        self,
        restaurant: Restaurant,
        customer: User,
        cocody: DeliveryZone,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        commande = commande_prete(restaurant, customer, cocody, "EC200008")
        assert DispatchService.dispatch(commande.pk) is None  # personne en ligne
        retardataire = livreur(restaurant, "retard@elcorazon.test", LOME, is_online=False)

        with django_capture_on_commit_callbacks(execute=True):
            CourierService.set_online(courier=retardataire, is_online=True)

        assert Assignment.objects.get(order=commande).courier == retardataire

    def test_une_cuisine_en_affectation_manuelle_n_est_pas_touchee(
        self,
        restaurant: Restaurant,
        customer: User,
        courier: CourierProfile,
        cocody: DeliveryZone,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        Restaurant.objects.filter(pk=restaurant.pk).update(auto_dispatch_couriers=False)
        commande = build_order(
            restaurant,
            customer,
            reference="EC200009",
            status=OrderStatus.PREPARING,
            delivery_zone=cocody,
        )

        with django_capture_on_commit_callbacks(execute=True):
            OrderService.transition_to(order=commande, target=OrderStatus.READY)

        assert not Assignment.objects.exists()
        assert DispatchService.dispatch_waiting() == 0

    def test_une_commande_deja_couverte_n_est_pas_reproposee(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile, cocody: DeliveryZone
    ) -> None:
        commande = commande_prete(restaurant, customer, cocody, "EC200010")
        AssignmentService.offer(order=commande, courier=courier)
        livreur(restaurant, "autre@elcorazon.test", LOME)

        assert DispatchService.dispatch(commande.pk) is None
        assert Assignment.objects.filter(order=commande).count() == 1

    def test_la_tache_planifiee_rattrape_les_commandes_pretes(
        self, restaurant: Restaurant, customer: User, cocody: DeliveryZone
    ) -> None:
        from apps.delivery.tasks import expire_stale_offers

        commande = commande_prete(restaurant, customer, cocody, "EC200011")
        livreur(restaurant, "tache@elcorazon.test", LOME)

        resultat = expire_stale_offers()

        assert resultat == {"expired": 0, "offered": 1}
        assert Assignment.objects.filter(order=commande, status=DeliveryStatus.OFFERED).exists()


# ============================================== ce que le livreur voit


class TestCeQueLeLivreurVoit:
    def course(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile, **kw: Any
    ) -> Assignment:
        commande = commande_prete(
            restaurant,
            customer,
            restaurant.zone,
            "EC300001",
            delivery_instructions="Portail bleu, sonnez deux fois",
            **kw,
        )
        return AssignmentService.offer(order=commande, courier=courier)

    def test_le_montant_a_encaisser_les_consignes_et_la_zone(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile, menu_item: Any
    ) -> None:
        course = self.course(restaurant, customer, courier, payment_method=PaymentMethod.CASH)
        OrderLine.objects.create(
            order=course.order,
            menu_item=menu_item,
            item_name="Burger Corazón",
            unit_price=Money(3_500, XOF),
            quantity=2,
            line_total=Money(7_000, XOF),
            options=[{"group": "Cuisson", "option": "À point"}],
        )

        donnees = (
            connecte(courier.user)
            .get(reverse("v1:delivery:assignment-detail", args=[course.pk]))
            .data
        )

        assert donnees["payment_method"] == "cash"
        assert donnees["amount_to_collect"] == {"amount": "4000", "currency": XOF}
        assert donnees["delivery_instructions"] == "Portail bleu, sonnez deux fois"
        assert donnees["delivery_zone_name"] == "Centre"
        assert donnees["city_name"] == "Lomé"
        # `item_image` fait partie du contrat : la fiche d'une course montre une
        # vignette par article, et le livreur vérifie son sac à l'œil.
        assert donnees["items"] == [
            {
                "name": "Burger Corazón",
                "item_image": "",
                "quantity": 2,
                "options": ["À point"],
                "notes": "",
            }
        ]
        # Le statut de la **commande**, que l'étape de la course ne dit pas :
        # c'est lui qui autorise l'application du livreur à proposer « J'ai
        # récupéré la commande ».
        assert donnees["order_status"] == OrderStatus.READY

    def encaisse(self, order: Order, montant: Money, reference: str) -> None:
        """Enregistre un encaissement, et le reporte comme le fait le webhook."""
        Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference=reference,
            amount=montant,
            status=PaymentStatus.COMPLETED,
        )
        report_settled_total(order)

    def a_encaisser(self, course: Assignment, courier: CourierProfile) -> Any:
        return (
            connecte(courier.user)
            .get(reverse("v1:delivery:assignment-detail", args=[course.pk]))
            .data["amount_to_collect"]
        )

    def test_rien_a_encaisser_quand_c_est_deja_paye(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        course = self.course(
            restaurant, customer, courier, payment_method=PaymentMethod.MOBILE_MONEY
        )
        self.encaisse(course.order, course.order.total, "PD-REGLEE-001")

        assert self.a_encaisser(course, courier) is None

    def test_un_paiement_en_ligne_qui_n_a_pas_abouti_reste_a_encaisser(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        """Le défaut que la règle précédente produisait, dans son cas exact.

        Le moyen annoncé est « mobile money », et rien n'a été encaissé : le
        prestataire a refusé, ou le client a quitté l'écran de paiement, et
        quelqu'un a confirmé la commande à la main depuis le back-office. Le
        livreur doit réclamer le total à la porte — l'ancienne règle lui
        affichait « déjà réglée », et il repartait sans son argent.
        """
        course = self.course(
            restaurant, customer, courier, payment_method=PaymentMethod.MOBILE_MONEY
        )

        assert self.a_encaisser(course, courier) == MoneyField().to_representation(
            course.order.total
        )

    def test_un_paiement_partiel_ne_laisse_a_encaisser_que_le_reste(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        """Le cas du paiement partagé dont toutes les parts ne sont pas réglées."""
        course = self.course(
            restaurant, customer, courier, payment_method=PaymentMethod.MOBILE_MONEY
        )
        moitie = Money(course.order.total.amount_minor // 2, course.order.total.currency)
        self.encaisse(course.order, moitie, "PD-MOITIE-001")

        reste = course.order.total - moitie
        assert self.a_encaisser(course, courier) == MoneyField().to_representation(reste)

    def test_des_especes_deja_encaissees_ne_sont_pas_reclamees_deux_fois(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        """Le moyen annoncé ne décide plus, dans un sens comme dans l'autre.

        Une commande en espèces dont le règlement a finalement été enregistré —
        le client a payé au comptoir, ou par un autre canal — n'a plus rien à
        réclamer à la porte. L'ancienne règle, qui lisait « espèces donc à
        encaisser », la facturait une seconde fois.
        """
        course = self.course(restaurant, customer, courier, payment_method=PaymentMethod.CASH)
        self.encaisse(course.order, course.order.total, "PD-COMPTOIR-001")

        assert self.a_encaisser(course, courier) is None

    def test_le_numero_du_client_n_arrive_qu_a_l_acceptation(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        course = self.course(restaurant, customer, courier)
        client = connecte(courier.user)
        url = reverse("v1:delivery:assignment-detail", args=[course.pk])

        assert client.get(url).data["recipient_phone"] == ""

        AssignmentService.accept(assignment=course, courier=courier)
        assert client.get(url).data["recipient_phone"] == "+22890111111"

    def test_le_personnel_lit_toujours_le_numero(
        self, restaurant: Restaurant, customer: User, courier: CourierProfile
    ) -> None:
        course = self.course(restaurant, customer, courier)
        superviseur = User.objects.create_user(
            "sup.course@elcorazon.test", "motdepasse", full_name="S", user_type=UserType.STAFF
        )
        superviseur.roles.add(
            Role.objects.create(name="Lecture commandes", permissions=["orders.read"])
        )
        StaffMembership.objects.create(user=superviseur, restaurant=restaurant)

        reponse = connecte(superviseur).get(
            reverse("v1:delivery:managed-assignment-detail", args=[course.pk])
        )

        assert reponse.status_code == status.HTTP_200_OK, reponse.data
        assert reponse.data["recipient_phone"] == "+22890111111"
