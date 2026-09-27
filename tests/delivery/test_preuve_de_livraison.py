"""Preuve de livraison — une photo déposée par le livreur, gardée par le serveur.

`Assignment.proof_of_delivery` existait en base, sur un stockage privé, sans
qu'aucun sérialiseur ni aucune vue ne permette de l'écrire : l'application du
livreur ne pouvait rien déposer, et le disait. Constaté à l'audit de DELY le
2026-09-25.

Ce que la route garantit :

* seul le livreur de la course dépose — celle d'un collègue est introuvable ;
* une preuve se dépose une fois le repas parti (`on_the_way`) ou livré, jamais
  avant l'enlèvement ;
* une fois la course livrée et la preuve posée, elle ne se remplace plus :
  c'est elle qu'on relira en cas de litige ;
* la photo n'est jamais rendue par l'API du livreur : on dit **qu'il y en a
  une**, pas où elle est.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.restaurants.models import Restaurant

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def photo(nom: str = "porte.jpg") -> SimpleUploadedFile:
    tampon = BytesIO()
    Image.new("RGB", (8, 8), color=(200, 120, 40)).save(tampon, format="JPEG")
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type="image/jpeg")


def deposer(client: APIClient, course: Assignment, fichier: object) -> object:
    return client.post(
        reverse("v1:delivery:assignment-proof", args=[course.pk]),
        {"photo": fichier},
        format="multipart",
    )


@pytest.fixture
def as_courier(courier: CourierProfile) -> APIClient:
    client = APIClient()
    client.force_authenticate(courier.user)
    return client


@pytest.fixture
def collegue(restaurant: Restaurant) -> CourierProfile:
    return CourierProfile.objects.create(
        user=User.objects.create_user(
            "collegue@elcorazon.test",
            "motdepasse",
            full_name="Yao Adjo",
            user_type=UserType.COURIER,
        ),
        restaurant=restaurant,
        vehicle_type=VehicleType.SCOOTER,
        verification_status=VerificationStatus.APPROVED,
        is_online=True,
    )


def course_en(order: Order, courier: CourierProfile, statut: str) -> Assignment:
    return Assignment.objects.create(order=order, courier=courier, status=statut)


def test_le_livreur_depose_sa_preuve_en_route(
    as_courier: APIClient, order: Order, courier: CourierProfile
) -> None:
    course = course_en(order, courier, DeliveryStatus.ON_THE_WAY)

    reponse = deposer(as_courier, course, photo())

    assert reponse.status_code == status.HTTP_200_OK, reponse.data
    assert reponse.data["has_proof_of_delivery"] is True
    course.refresh_from_db()
    assert course.proof_of_delivery


def test_la_photo_n_est_jamais_rendue(
    as_courier: APIClient, order: Order, courier: CourierProfile
) -> None:
    course = course_en(order, courier, DeliveryStatus.ON_THE_WAY)
    deposer(as_courier, course, photo())

    detail = as_courier.get(reverse("v1:delivery:assignment-detail", args=[course.pk]))

    assert detail.data["has_proof_of_delivery"] is True
    assert "proof_of_delivery" not in detail.data


def test_la_course_d_un_collegue_est_introuvable(
    order: Order, courier: CourierProfile, collegue: CourierProfile
) -> None:
    course = course_en(order, courier, DeliveryStatus.ON_THE_WAY)
    autre = APIClient()
    autre.force_authenticate(collegue.user)

    reponse = deposer(autre, course, photo())

    assert reponse.status_code == status.HTTP_404_NOT_FOUND
    course.refresh_from_db()
    assert not course.proof_of_delivery


@pytest.mark.parametrize(
    "statut", [DeliveryStatus.OFFERED, DeliveryStatus.ACCEPTED, DeliveryStatus.PICKED_UP]
)
def test_pas_de_preuve_avant_le_depart(
    as_courier: APIClient, order: Order, courier: CourierProfile, statut: str
) -> None:
    course = course_en(order, courier, statut)

    reponse = deposer(as_courier, course, photo())

    assert reponse.status_code == status.HTTP_409_CONFLICT


def test_une_course_livree_accepte_une_premiere_preuve(
    as_courier: APIClient, order: Order, courier: CourierProfile
) -> None:
    """La photo prise à la porte peut partir juste après « livré » : le
    réseau n'est pas toujours là au moment où l'on appuie."""
    course = course_en(order, courier, DeliveryStatus.DELIVERED)

    assert deposer(as_courier, course, photo()).status_code == status.HTTP_200_OK


def test_une_preuve_posee_sur_une_course_livree_ne_se_remplace_pas(
    as_courier: APIClient, order: Order, courier: CourierProfile
) -> None:
    course = course_en(order, courier, DeliveryStatus.DELIVERED)
    deposer(as_courier, course, photo("premiere.jpg"))
    course.refresh_from_db()
    premiere = course.proof_of_delivery.name

    reponse = deposer(as_courier, course, photo("seconde.jpg"))

    assert reponse.status_code == status.HTTP_409_CONFLICT
    course.refresh_from_db()
    assert course.proof_of_delivery.name == premiere


def test_un_fichier_qui_n_est_pas_une_image_est_refuse(
    as_courier: APIClient, order: Order, courier: CourierProfile
) -> None:
    course = course_en(order, courier, DeliveryStatus.ON_THE_WAY)
    faux = SimpleUploadedFile("porte.jpg", b"pas une image", content_type="image/jpeg")

    reponse = deposer(as_courier, course, faux)

    assert reponse.status_code == status.HTTP_400_BAD_REQUEST
