"""Expiration des pièces du dossier livreur.

Le cahier des charges demande le suivi de l'expiration (§4.2.5). Rien ne la
portait : l'ancien modèle Flutter avait un champ « expiration » que rien ne
renseignait. Le test décisif est `test_on_ne_valide_pas_sur_une_piece_expiree` :
valider, c'est affirmer que les pièces sont bonnes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import CourierProfile
from apps.delivery.services import CourierService
from apps.delivery.states import VerificationStatus
from apps.delivery.tasks import remind_document_expiry
from apps.notifications.models import Notification
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

VERIFICATION = "v1:delivery:courier-verification"


def personnel(email: str, restaurant: Restaurant, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def instructeur(restaurant: Restaurant) -> User:
    return personnel("rh@elcorazon.test", restaurant, "couriers.read", "couriers.approve")


@pytest.fixture
def en_attente(courier: CourierProfile) -> CourierProfile:
    courier.verification_status = VerificationStatus.PENDING
    courier.save(update_fields=["verification_status"])
    return courier


def jour(delta: int) -> str:
    return (timezone.localdate() + timedelta(days=delta)).isoformat()


class TestSaisie:
    def test_les_dates_se_relevent_a_la_validation(
        self, instructeur: User, en_attente: CourierProfile
    ) -> None:
        response = connecte(instructeur).post(
            reverse(VERIFICATION, args=[en_attente.pk]),
            {
                "status": VerificationStatus.APPROVED,
                "licence_document_expires_on": jour(400),
                "vehicle_document_expires_on": jour(200),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["licence_document_expires_on"] == jour(400)
        assert response.data["id_document_expires_on"] is None

    def test_on_ne_valide_pas_sur_une_piece_expiree(
        self, instructeur: User, en_attente: CourierProfile
    ) -> None:
        response = connecte(instructeur).post(
            reverse(VERIFICATION, args=[en_attente.pk]),
            {"status": VerificationStatus.APPROVED, "licence_document_expires_on": jour(-1)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "permis de conduire" in response.data["detail"]
        en_attente.refresh_from_db()
        assert en_attente.verification_status == VerificationStatus.PENDING

    def test_une_date_se_complete_sur_un_dossier_deja_valide(
        self, instructeur: User, courier: CourierProfile
    ) -> None:
        """Le statut ne change pas, la date s'écrit quand même : sinon elle ne
        se saisirait jamais après coup."""
        response = connecte(instructeur).post(
            reverse(VERIFICATION, args=[courier.pk]),
            {"status": VerificationStatus.APPROVED, "id_document_expires_on": jour(900)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.id_document_expires_on is not None

    def test_une_nouvelle_piece_efface_la_date_de_l_ancienne(self, courier: CourierProfile) -> None:
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=10)
        courier.save(update_fields=["licence_document_expires_on"])

        CourierService.replace_documents(
            courier=courier,
            licence_document=SimpleUploadedFile("permis.jpg", b"x", content_type="image/jpeg"),
        )

        courier.refresh_from_db()
        assert courier.licence_document_expires_on is None


class TestRappels:
    def test_a_un_mois_le_livreur_et_l_equipe_sont_prevenus(
        self, restaurant: Restaurant, courier: CourierProfile
    ) -> None:
        equipe = personnel("rh@elcorazon.test", restaurant, "couriers.read")
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=30)
        courier.save(update_fields=["licence_document_expires_on"])

        resultat = remind_document_expiry()

        assert resultat == {"reminders": 1}
        assert Notification.objects.filter(
            user=courier.user, title="Pièce bientôt expirée"
        ).exists()
        assert Notification.objects.filter(user=equipe, title="Pièce livreur à renouveler").exists()

    def test_entre_deux_echeances_personne_n_est_derange(self, courier: CourierProfile) -> None:
        """Prévenir chaque jour pendant un mois apprend à ignorer l'avis.

        À douze jours de l'échéance, le seuil courant est J-30 : il part une
        fois — la flotte peut avoir été validée après coup — puis plus rien
        jusqu'à J-7. C'est l'unicité par seuil qui tient la règle, et non le
        hasard du jour où la tâche tourne.
        """
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=12)
        courier.save(update_fields=["licence_document_expires_on"])

        premier = remind_document_expiry()

        assert premier == {"reminders": 1}
        assert remind_document_expiry() == {"reminders": 0}
        assert remind_document_expiry() == {"reminders": 0}

    def test_un_dossier_non_valide_n_est_pas_relance(self, courier: CourierProfile) -> None:
        courier.verification_status = VerificationStatus.REJECTED
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=7)
        courier.save(update_fields=["verification_status", "licence_document_expires_on"])

        assert remind_document_expiry() == {"reminders": 0}


class TestRappelsRejouables:
    """La tâche quotidienne relancée, retardée, ou doublée.

    Deux défauts d'exploitation que la première écriture ne voyait pas : le
    rejeu doublait les avis, et une journée non exécutée perdait son rappel
    pour de bon. `beat` perd son dernier passage à chaque redéploiement, et un
    service qui déploie plusieurs fois par jour ne lançait donc **jamais** sa
    tâche quotidienne.
    """

    def test_rejouer_la_tache_ne_previent_pas_deux_fois(self, courier: CourierProfile) -> None:
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=7)
        courier.save(update_fields=["licence_document_expires_on"])

        premier = remind_document_expiry()
        second = remind_document_expiry()

        assert premier == {"reminders": 1}
        assert second == {"reminders": 0}
        assert Notification.objects.filter(user=courier.user, kind="account").count() == 1

    def test_un_seuil_manque_est_rattrape(self, courier: CourierProfile) -> None:
        """La tâche n'a pas tourné le jour de J-7 : le rappel part avec du
        retard, plutôt que jamais."""
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=5)
        courier.save(update_fields=["licence_document_expires_on"])

        assert remind_document_expiry() == {"reminders": 1}
        avis = Notification.objects.get(user=courier.user, title="Pièce bientôt expirée")
        assert "expire le" in avis.body

    def test_le_seuil_suivant_part_quand_meme(self, courier: CourierProfile) -> None:
        """Rattraper J-7 n'éteint pas J-1 : ce sont deux avis différents."""
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=5)
        courier.save(update_fields=["licence_document_expires_on"])
        remind_document_expiry()

        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=1)
        courier.save(update_fields=["licence_document_expires_on"])

        assert remind_document_expiry() == {"reminders": 1}

    def test_une_piece_deja_expiree_se_dit_au_passe(self, courier: CourierProfile) -> None:
        """Le rappel du jour même peut arriver après coup ; « expire le 17/09 »
        au passé se lirait comme une erreur du système."""
        courier.licence_document_expires_on = timezone.localdate() - timedelta(days=3)
        courier.save(update_fields=["licence_document_expires_on"])

        assert remind_document_expiry() == {"reminders": 1}
        avis = Notification.objects.get(user=courier.user, title="Pièce expirée")
        assert "a expiré le" in avis.body

    def test_une_piece_renouvelee_a_ses_propres_rappels(self, courier: CourierProfile) -> None:
        """L'unicité porte sur l'échéance, pas sur la date d'envoi."""
        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=7)
        courier.save(update_fields=["licence_document_expires_on"])
        remind_document_expiry()

        courier.licence_document_expires_on = timezone.localdate() + timedelta(days=6)
        courier.save(update_fields=["licence_document_expires_on"])

        assert remind_document_expiry() == {"reminders": 1}
