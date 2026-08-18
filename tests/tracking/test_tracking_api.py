"""API du suivi — invariant L3.

L3 est une faille prouvée : l'ancien code n'exigeait aucun lien entre le relevé
et une course, si bien que n'importe qui pouvait écrire le suivi de n'importe
quelle commande — et faire croire à un client que son repas approchait.

Ici le relevé est rattaché à une **course**, et la course à un livreur. Il
n'existe pas de chemin pour écrire une position sur une commande qu'on ne
dessert pas.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from apps.tracking.models import LocationPing
from apps.tracking.services import TrackingService

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LOME = {"lat": 6.1319, "lon": 1.2255}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_courier(courier: CourierProfile) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(courier.user)
    return separate


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def course(order: Order, courier: CourierProfile) -> Assignment:
    """Course acceptée : l'état où une position a un sens."""
    return Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.ACCEPTED)


def ping(
    client: APIClient,
    assignment: Assignment,
    *,
    moment: dt.datetime | None = None,
    **position: float,
) -> object:
    return client.post(
        reverse("v1:tracking:pings", args=[assignment.pk]),
        {
            "point": {**LOME, **position},
            "recorded_at": (moment or timezone.now()).isoformat(),
        },
        format="json",
    )


class TestDepotDePosition:
    def test_le_livreur_assigne_depose_une_position(
        self, as_courier: APIClient, course: Assignment
    ) -> None:
        response = ping(as_courier, course)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["point"] == {
            "lat": pytest.approx(6.1319),
            "lon": pytest.approx(1.2255),
        }
        assert LocationPing.objects.count() == 1

    def test_la_position_du_dossier_est_rafraichie(
        self, as_courier: APIClient, course: Assignment, courier: CourierProfile
    ) -> None:
        """C'est elle qui sert à l'affectation : elle est mise à jour même
        quand le relevé n'est pas persisté."""
        ping(as_courier, course)
        courier.refresh_from_db()

        assert courier.last_location is not None
        assert courier.last_location_at is not None

    def test_l_horodatage_de_l_appareil_est_conserve(
        self, as_courier: APIClient, course: Assignment
    ) -> None:
        """Un livreur qui traverse une zone sans réseau émet en différé : sans
        cette distinction, une rafale rattrapée dessinerait un trajet
        instantané."""
        emis = timezone.now() - dt.timedelta(minutes=3)

        response = ping(as_courier, course, moment=emis)

        assert response.data["recorded_at"].startswith(emis.isoformat()[:16])
        assert response.data["received_at"] != response.data["recorded_at"]


class TestL3:
    def test_un_livreur_ne_suit_pas_la_course_d_un_autre(
        self, client: APIClient, course: Assignment, restaurant: Restaurant
    ) -> None:
        """Le cœur de la faille : écrire le suivi d'autrui."""
        intrus = CourierProfile.objects.create(
            user=User.objects.create_user(
                "intrus@elcorazon.test",
                "motdepasse",
                full_name="Intrus",
                user_type=UserType.COURIER,
            ),
            restaurant=restaurant,
            vehicle_type=VehicleType.MOTORCYCLE,
            verification_status=VerificationStatus.APPROVED,
            is_online=True,
        )
        client.force_authenticate(intrus.user)

        response = ping(client, course)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert LocationPing.objects.count() == 0

    def test_un_client_ne_depose_pas_de_position(
        self, as_customer: APIClient, course: Assignment
    ) -> None:
        response = ping(as_customer, course)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_visiteur_anonyme_non_plus(self, client: APIClient, course: Assignment) -> None:
        assert ping(client, course).status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.parametrize(
        "etat", [DeliveryStatus.OFFERED, DeliveryStatus.DELIVERED, DeliveryStatus.DECLINED]
    )
    def test_hors_course_aucune_position_n_est_attendue(
        self, as_courier: APIClient, course: Assignment, etat: str
    ) -> None:
        """Suivre un livreur en dehors de sa course n'est pas un service,
        c'est une filature."""
        Assignment.objects.filter(pk=course.pk).update(status=etat)

        response = ping(as_courier, course)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert LocationPing.objects.count() == 0


class TestEchantillonnage:
    def test_un_relevé_trop_proche_et_trop_recent_n_est_pas_persiste(
        self, as_courier: APIClient, course: Assignment
    ) -> None:
        """202 et non 201 : la position a bien été reçue et le dossier
        rafraîchi, elle n'a simplement pas mérité une ligne."""
        moment = timezone.now()
        ping(as_courier, course, moment=moment)

        second = ping(as_courier, course, moment=moment + dt.timedelta(seconds=2))

        assert second.status_code == status.HTTP_202_ACCEPTED
        assert LocationPing.objects.count() == 1

    def test_le_temps_ecoule_declenche_une_ecriture(
        self, as_courier: APIClient, course: Assignment
    ) -> None:
        moment = timezone.now()
        ping(as_courier, course, moment=moment)

        second = ping(as_courier, course, moment=moment + dt.timedelta(seconds=45))

        assert second.status_code == status.HTTP_201_CREATED
        assert LocationPing.objects.count() == 2

    def test_la_distance_parcourue_declenche_une_ecriture(
        self, as_courier: APIClient, course: Assignment
    ) -> None:
        """C'est ce critère qui rend le tracé fidèle malgré
        l'échantillonnage : un livreur arrêté n'écrit rien, un livreur qui
        avance écrit à chaque seuil franchi."""
        moment = timezone.now()
        ping(as_courier, course, moment=moment)

        # ~1,1 km à l'est, bien au-delà du seuil de 100 m.
        second = ping(as_courier, course, moment=moment + dt.timedelta(seconds=3), lon=1.2355)

        assert second.status_code == status.HTTP_201_CREATED
        assert LocationPing.objects.count() == 2


class TestLectureDuSuivi:
    def test_le_client_suit_sa_commande(
        self, as_courier: APIClient, as_customer: APIClient, course: Assignment, order: Order
    ) -> None:
        ping(as_courier, course)

        response = as_customer.get(reverse("v1:tracking:order", args=[order.pk]))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["assignment_status"] == DeliveryStatus.ACCEPTED
        assert response.data["last_position"]["point"]["lat"] == pytest.approx(6.1319)
        assert response.data["courier"]["full_name"] == "Kodjo Mensah"

    def test_le_suivi_ne_livre_pas_le_contact_du_livreur(
        self, as_customer: APIClient, course: Assignment, order: Order
    ) -> None:
        """De quoi le reconnaître à la porte, pas de quoi l'appeler ensuite."""
        response = as_customer.get(reverse("v1:tracking:order", args=[order.pk]))

        assert set(response.data["courier"]) == {
            "id",
            "full_name",
            "avatar",
            "vehicle_type",
            "rating_average",
            "rating_count",
        }

    def test_sans_livreur_le_suivi_est_vide_et_non_absent(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """« Pas encore de livreur » est l'état normal des premières minutes :
        le client doit pouvoir afficher son écran sans traiter une erreur."""
        response = as_customer.get(reverse("v1:tracking:order", args=[order.pk]))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["assignment_status"] == ""
        assert response.data["last_position"] is None

    def test_la_commande_d_autrui_est_introuvable(
        self, client: APIClient, courier_user: User, order: Order
    ) -> None:
        """S3 — le suivi expose l'adresse de livraison ; il exige d'être le
        propriétaire de la commande."""
        client.force_authenticate(courier_user)

        response = client.get(reverse("v1:tracking:order", args=[order.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestPurge:
    def test_les_releves_anciens_sont_purgeables(self, course: Assignment) -> None:
        """Le suivi n'a de valeur qu'en direct : garder 1,7 million de lignes
        par jour coûte sans rien apporter."""
        LocationPing.objects.create(
            assignment=course, point=Point(1.2255, 6.1319, srid=4326), recorded_at=timezone.now()
        )
        LocationPing.objects.update(received_at=timezone.now() - dt.timedelta(days=40))

        supprimes = TrackingService.purge_older_than(days=30)

        assert supprimes == 1
        assert LocationPing.objects.count() == 0

    def test_les_releves_recents_survivent(self, course: Assignment) -> None:
        LocationPing.objects.create(
            assignment=course, point=Point(1.2255, 6.1319, srid=4326), recorded_at=timezone.now()
        )

        assert TrackingService.purge_older_than(days=30) == 0
        assert LocationPing.objects.count() == 1
