"""Planning de la flotte — `/delivery/shifts/`.

Le test le plus important de ce fichier est
`test_le_planning_ne_conditionne_pas_l_eligibilite` : le planning est
**indicatif**. Un livreur en ligne et validé prend des courses, créneau ou pas.
L'inverse ferait refuser du travail à quelqu'un de présent, avec un message
qu'aucun écran ne sait expliquer, et laisserait la commande sans porteur.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import CourierProfile, CourierShift
from apps.delivery.states import VerificationStatus
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LISTE = "v1:delivery:shift-list"
DETAIL = "v1:delivery:shift-detail"


@pytest.fixture
def as_planificateur(restaurant: Restaurant) -> APIClient:
    membre = User.objects.create_user(
        "planning@elcorazon.test", "motdepasse", full_name="Planning", user_type=UserType.STAFF
    )
    membre.roles.add(
        Role.objects.create(name="Planning", permissions=["couriers.read", "couriers.write"])
    )
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


def creneau(courier: CourierProfile, jour: int = 2, debut: str = "09:00") -> dict[str, object]:
    return {
        "courier": str(courier.pk),
        "day_of_week": jour,
        "start_time": debut,
        "end_time": "17:00",
    }


class TestPlanification:
    def test_l_exploitation_pose_un_creneau(
        self, as_planificateur: APIClient, courier: CourierProfile
    ) -> None:
        response = as_planificateur.post(reverse(LISTE), creneau(courier), format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["courier_name"] == courier.user.full_name

    def test_un_creneau_a_l_envers_est_refuse_lisiblement(
        self, as_planificateur: APIClient, courier: CourierProfile
    ) -> None:
        """Un créneau qui passe minuit s'écrit en deux lignes, sur deux jours."""
        response = as_planificateur.post(
            reverse(LISTE),
            {**creneau(courier), "start_time": "22:00", "end_time": "06:00"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "end_time" in response.data["errors"]

    def test_deux_creneaux_identiques_sont_refuses(
        self, as_planificateur: APIClient, courier: CourierProfile
    ) -> None:
        """Même jour, même heure de début : c'est une double saisie, et les deux
        lignes s'afficheraient l'une sur l'autre."""
        as_planificateur.post(reverse(LISTE), creneau(courier), format="json")
        response = as_planificateur.post(reverse(LISTE), creneau(courier), format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_absence_se_marque_au_lieu_de_s_effacer(
        self, as_planificateur: APIClient, courier: CourierProfile
    ) -> None:
        """Elle se lit alors dans le planning, au lieu d'en disparaître."""
        cree = as_planificateur.post(reverse(LISTE), creneau(courier), format="json")

        response = as_planificateur.patch(
            reverse(DETAIL, kwargs={"pk": cree.data["id"]}),
            {"is_available": False},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_available"] is False

    def test_un_creneau_saisi_par_erreur_se_supprime(
        self, as_planificateur: APIClient, courier: CourierProfile
    ) -> None:
        """Contrairement aux autres back-offices : un créneau n'est pas une
        pièce comptable, rien n'y renvoie."""
        cree = as_planificateur.post(reverse(LISTE), creneau(courier), format="json")

        response = as_planificateur.delete(reverse(DETAIL, kwargs={"pk": cree.data["id"]}))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not CourierShift.objects.exists()


class TestIndicatifEtNonOpposable:
    def test_le_planning_ne_conditionne_pas_l_eligibilite(self, courier: CourierProfile) -> None:
        """Le cœur du choix : L1 ne compte que trois termes — en ligne, validé,
        actif. Un livreur sans le moindre créneau prend des courses."""
        courier.is_online = True
        courier.verification_status = VerificationStatus.APPROVED
        courier.save(update_fields=["is_online", "verification_status"])

        assert not CourierShift.objects.filter(courier=courier).exists()
        assert courier.can_accept_orders

    def test_un_creneau_marque_indisponible_ne_bloque_pas_non_plus(
        self, courier: CourierProfile
    ) -> None:
        """La bascule « en ligne » est la déclaration du livreur, qui sait mieux
        que le planning s'il roule à cet instant."""
        CourierShift.objects.create(
            courier=courier,
            day_of_week=1,
            start_time=dt.time(9),
            end_time=dt.time(17),
            is_available=False,
        )
        courier.is_online = True
        courier.verification_status = VerificationStatus.APPROVED
        courier.save(update_fields=["is_online", "verification_status"])

        assert courier.can_accept_orders


class TestCloisonnement:
    def test_on_ne_planifie_pas_le_livreur_d_une_autre_enseigne(
        self, as_planificateur: APIClient, restaurant: Restaurant
    ) -> None:
        autre = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000002",
        )
        etranger_user = User.objects.create_user(
            "kara@elcorazon.test", "motdepasse", full_name="Kossi", user_type=UserType.COURIER
        )
        etranger = CourierProfile.objects.create(
            user=etranger_user, restaurant=autre, vehicle_type="motorcycle"
        )

        response = as_planificateur.post(reverse(LISTE), creneau(etranger), format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not CourierShift.objects.exists()

    def test_sans_droit_sur_la_flotte_rien_ne_s_ecrit(
        self, customer: User, courier: CourierProfile
    ) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        response = client.post(reverse(LISTE), creneau(courier), format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
