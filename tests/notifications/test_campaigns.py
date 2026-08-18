"""Campagnes de notifications ciblées — ADR-008.

Deux vérifications portent ce module :

* `test_un_double_envoi_ne_part_qu_une_fois` — un envoi de masse ne se rappelle
  pas, et le destinataire qui reçoit deux fois le même message se désabonne ;
* `test_un_refus_de_marketing_est_respecte` — le consentement est décidé par
  `notify` et par lui seul ; le redécider ici produirait deux règles, dont
  l'une finirait par être la mauvaise.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.notifications.models import Audience, Campaign, CampaignStatus, Notification
from apps.notifications.services import recipients_of, send_campaign
from apps.profiles.models import CustomerPreference
from apps.restaurants.models import Restaurant, StaffMembership
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant, *permissions: str) -> User:
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def marketing(restaurant: Restaurant) -> APIClient:
    client = APIClient()
    client.force_authenticate(
        personnel("marketing@elcorazon.test", restaurant, "notifications.send")
    )
    return client


def campagne(**extra: object) -> Campaign:
    valeurs: dict[str, object] = {
        "title": "−20 % ce week-end",
        "body": "Profitez-en jusqu'à dimanche.",
        "audience": Audience.ALL_CUSTOMERS,
    }
    valeurs.update(extra)
    return Campaign.objects.create(**valeurs)


class TestRedaction:
    def test_sans_permission_rien_n_est_lisible(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        assert (
            client.get(reverse("v1:notifications:campaign-list")).status_code
            == status.HTTP_403_FORBIDDEN
        )

    def test_une_campagne_nait_en_brouillon_et_porte_son_auteur(self, marketing: APIClient) -> None:
        """Une trace qu'on peut renseigner soi-même ne trace rien : l'auteur
        vient du jeton."""
        response = marketing.post(
            reverse("v1:notifications:campaign-list"),
            {
                "title": "Nouveau burger",
                "body": "À découvrir dès aujourd'hui.",
                "audience": Audience.ALL_CUSTOMERS,
                "created_by": "peu importe",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == CampaignStatus.DRAFT
        assert response.data["created_by_email"] == "marketing@elcorazon.test"

    def test_une_campagne_envoyee_ne_se_modifie_plus(
        self, marketing: APIClient, customer: User
    ) -> None:
        """L'historique afficherait un texte que personne n'a reçu."""
        envoyee = send_campaign(campagne())

        response = marketing.patch(
            reverse("v1:notifications:campaign-detail", args=[envoyee.pk]),
            {"title": "Réécrit après coup"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        envoyee.refresh_from_db()
        assert envoyee.title == "−20 % ce week-end"


class TestSegments:
    def test_le_segment_par_defaut_est_la_clientele_active(
        self, customer: User, courier_user: User
    ) -> None:
        """Ni les livreurs, ni les comptes bloqués."""
        bloque = User.objects.create_user("bloque@elcorazon.test", "motdepasse", is_active=False)

        cibles = set(recipients_of(campagne()).values_list("email", flat=True))

        assert cibles == {customer.email}
        assert courier_user.email not in cibles
        assert bloque.email not in cibles

    def test_la_reconquete_vise_aussi_ceux_qui_n_ont_jamais_commande(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """La formulation par exclusion embarque les comptes sans commande, et
        c'est la population qu'une campagne de reconquête vise en premier."""
        recent = User.objects.create_user("recent@elcorazon.test", "motdepasse")
        build_order(restaurant, recent)

        cibles = set(
            recipients_of(campagne(audience=Audience.LAPSED_CUSTOMERS)).values_list(
                "email", flat=True
            )
        )

        assert customer.email in cibles
        assert recent.email not in cibles

    def test_le_segment_actif_ne_retient_que_les_commandes_recentes(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        ancienne = build_order(restaurant, customer, reference="EC000042")
        type(ancienne).objects.filter(pk=ancienne.pk).update(
            placed_at=timezone.now() - dt.timedelta(days=90)
        )

        cibles = recipients_of(campagne(audience=Audience.ACTIVE_CUSTOMERS))

        assert cibles.count() == 0

    def test_l_estimation_annonce_un_majorant(self, marketing: APIClient, customer: User) -> None:
        """Elle compte le segment, pas les envois aboutis : le consentement ne
        se vérifie qu'à l'écriture de chaque notification."""
        brouillon = campagne()

        response = marketing.get(reverse("v1:notifications:campaign-audience", args=[brouillon.pk]))

        assert response.data["recipients"] == 1


class TestEnvoi:
    def test_l_envoi_ecrit_une_notification_par_destinataire(
        self, marketing: APIClient, customer: User
    ) -> None:
        brouillon = campagne()

        response = marketing.post(
            reverse("v1:notifications:campaign-send", args=[brouillon.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == CampaignStatus.SENT
        assert response.data["recipient_count"] == 1
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1

    def test_un_double_envoi_ne_part_qu_une_fois(
        self, marketing: APIClient, customer: User
    ) -> None:
        """Un double clic sur « envoyer » arrive régulièrement ; le rejeu est
        absorbé plutôt que refusé, pour ne pas faire croire à un échec."""
        brouillon = campagne()
        url = reverse("v1:notifications:campaign-send", args=[brouillon.pk])

        premier = marketing.post(url, {}, format="json")
        second = marketing.post(url, {}, format="json")

        assert premier.status_code == second.status_code == status.HTTP_200_OK
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1
        assert second.data["sent_at"] == premier.data["sent_at"]

    def test_un_refus_de_marketing_est_respecte(self, marketing: APIClient, customer: User) -> None:
        """`notify` écarte le compte, et le compteur ne l'inclut donc pas :
        annoncer le segment plutôt que les envois donnerait un taux d'ouverture
        flatteur et faux."""
        CustomerPreference.objects.update_or_create(
            user=customer, defaults={"marketing_push_enabled": False}
        )
        brouillon = campagne()

        response = marketing.post(
            reverse("v1:notifications:campaign-send", args=[brouillon.pk]), {}, format="json"
        )

        assert response.data["recipient_count"] == 0
        assert not Notification.objects.filter(user=customer, kind="marketing").exists()
