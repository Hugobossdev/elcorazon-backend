"""Candidature spontanée de livreur — `POST /delivery/apply/`.

Cette route ouvre une seconde porte vers `CourierService.provision`, à côté de
l'embauche par le back-office. Ce que cette suite vérifie n'est donc pas qu'elle
crée un dossier — c'est le même service, déjà couvert — mais qu'elle ne crée
**rien d'autre** :

* `test_le_dossier_nait_en_attente` et `test_un_candidat_ne_peut_pas_se_valider`
  — L1 tient : un candidat se met dans la file, il ne se valide pas ;
* `test_aucun_jeton_n_est_rendu` — la vérification de l'adresse n'est pas une
  formalité que le client pourrait sauter ;
* `TestParcoursComplet` — bout en bout : candidature, code, session, et le refus
  d'aller plus loin tant que le dossier n'est pas instruit.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType, VerificationCode, VerificationPurpose
from apps.delivery.models import CourierProfile, VehicleType
from apps.delivery.states import VerificationStatus
from apps.restaurants.models import Restaurant, RestaurantStatus

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

APPLY = "v1:delivery:apply"
VERIFY = "v1:accounts:verify"
LOGIN = "v1:accounts:login"
ME = "v1:delivery:me"
ONLINE = "v1:delivery:me-online"

MOT_DE_PASSE = "MotDePasseSolide!42"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def candidature(restaurant: Restaurant) -> dict[str, Any]:
    return {
        "email": "yao@elcorazon.test",
        "password": MOT_DE_PASSE,
        "full_name": "Yao Agbeko",
        "phone": "+22890111222",
        "restaurant": restaurant.slug,
        "vehicle_type": VehicleType.MOTORCYCLE,
        "vehicle_plate": "TG-1234",
    }


def code_connu(email: str, valeur: str = "123456") -> str:
    """Repose une empreinte connue — voir `tests/accounts/test_verification.py`."""
    record = VerificationCode.objects.filter(
        sent_to=email,
        purpose=VerificationPurpose.ACCOUNT_VERIFICATION,
        consumed_at__isnull=True,
    ).first()
    assert record is not None
    record.code_hash = make_password(valeur)
    record.save(update_fields=["code_hash"])
    return valeur


class TestDepot:
    def test_cree_le_compte_et_le_dossier(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        response = client.post(reverse(APPLY), candidature, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        user = User.objects.get(email="yao@elcorazon.test")
        assert user.user_type == UserType.COURIER
        assert CourierProfile.objects.filter(user=user).exists()

    def test_le_dossier_nait_en_attente(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """L1 — la candidature met dans la file, elle ne valide pas."""
        response = client.post(reverse(APPLY), candidature, format="json")

        profil = CourierProfile.objects.get(user__email="yao@elcorazon.test")
        assert profil.verification_status == VerificationStatus.PENDING
        assert profil.can_accept_orders is False
        assert response.data["verification_status"] == VerificationStatus.PENDING

    def test_un_candidat_ne_peut_pas_se_valider(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """Le champ n'existe pas en entrée : il est ignoré, pas honoré."""
        client.post(
            reverse(APPLY),
            {**candidature, "verification_status": VerificationStatus.APPROVED},
            format="json",
        )

        profil = CourierProfile.objects.get(user__email="yao@elcorazon.test")
        assert profil.verification_status == VerificationStatus.PENDING

    def test_un_candidat_ne_peut_pas_se_faire_personnel(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """`user_type` n'est jamais lu d'une requête — la même garde qu'à
        l'inscription client, pour la même raison."""
        client.post(reverse(APPLY), {**candidature, "user_type": "staff"}, format="json")

        assert User.objects.get(email="yao@elcorazon.test").user_type == UserType.COURIER

    def test_aucun_jeton_n_est_rendu(self, client: APIClient, candidature: dict[str, Any]) -> None:
        """Sans quoi l'écran de saisie du code serait une étape que le client
        mobile pourrait sauter : il aurait déjà de quoi appeler l'API."""
        response = client.post(reverse(APPLY), candidature, format="json")

        assert "access" not in response.data
        assert "refresh" not in response.data

    def test_un_code_est_emis(self, client: APIClient, candidature: dict[str, Any]) -> None:
        client.post(reverse(APPLY), candidature, format="json")

        assert VerificationCode.objects.filter(
            sent_to="yao@elcorazon.test",
            purpose=VerificationPurpose.ACCOUNT_VERIFICATION,
        ).exists()

    def test_le_courriel_part_apres_le_commit(
        self,
        client: APIClient,
        candidature: dict[str, Any],
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        mail.outbox.clear()

        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse(APPLY), candidature, format="json")

        assert [message.to for message in mail.outbox] == [["yao@elcorazon.test"]]


class TestRefus:
    def test_une_adresse_deja_prise_est_refusee(
        self, client: APIClient, candidature: dict[str, Any], courier_user: User
    ) -> None:
        response = client.post(
            reverse(APPLY), {**candidature, "email": courier_user.email}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "email" in response.data["errors"]

    def test_un_telephone_local_est_refuse(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """Format E.164 exigé — c'est le seul moyen de joindre le candidat pour
        instruire son dossier, et personne du personnel n'est là pour corriger
        la saisie."""
        response = client.post(reverse(APPLY), {**candidature, "phone": "90111222"}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "phone" in response.data["errors"]

    def test_le_telephone_est_obligatoire(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        sans_telephone = {clef: v for clef, v in candidature.items() if clef != "phone"}

        response = client.post(reverse(APPLY), sans_telephone, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "phone" in response.data["errors"]

    def test_un_mot_de_passe_faible_est_refuse(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        response = client.post(
            reverse(APPLY), {**candidature, "password": "12345678"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not User.objects.filter(email="yao@elcorazon.test").exists()

    def test_un_etablissement_ferme_est_refuse(
        self, client: APIClient, candidature: dict[str, Any], restaurant: Restaurant
    ) -> None:
        """La liste où le candidat choisit est celle de `GET /restaurants/`, qui
        ne rend que les établissements actifs. Accepter les autres permettrait
        de se rattacher, en tapant un slug, à une adresse fermée."""
        # `is_active` découle de `status` : l'écrire seul serait annulé par
        # le prochain `save()`, qui le réaligne sur l'état.
        restaurant.status = RestaurantStatus.INACTIVE
        restaurant.save(update_fields=["status"])

        response = client.post(reverse(APPLY), candidature, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "restaurant" in response.data["errors"]

    def test_rien_n_est_cree_quand_la_candidature_est_refusee(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """Le compte et le dossier tiennent ou tombent ensemble : un `User` de
        type livreur sans `CourierProfile` est quelqu'un qui se connecte à
        l'application et n'y trouve rien."""
        client.post(reverse(APPLY), {**candidature, "vehicle_type": "fusee"}, format="json")

        assert not User.objects.filter(email="yao@elcorazon.test").exists()
        assert not CourierProfile.objects.filter(user__email="yao@elcorazon.test").exists()


class TestParcoursComplet:
    """Candidature → code → session → refus tant que le dossier n'est pas instruit."""

    def test_le_parcours_mene_a_une_session_de_livreur(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        client.post(reverse(APPLY), candidature, format="json")
        code = code_connu("yao@elcorazon.test")

        session = client.post(
            reverse(VERIFY), {"email": "yao@elcorazon.test", "code": code}, format="json"
        )

        assert session.status_code == status.HTTP_200_OK
        assert session.data["user"]["user_type"] == UserType.COURIER
        assert session.data["user"]["email_verified_at"] is not None

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.data['access']}")
        dossier = client.get(reverse(ME))

        assert dossier.status_code == status.HTTP_200_OK
        assert dossier.data["verification_status"] == VerificationStatus.PENDING
        assert dossier.data["can_accept_orders"] is False

    def test_un_candidat_verifie_reste_ineligible_aux_courses(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """L1 — se déclarer en ligne ne rend pas éligible. Le serveur accepte la
        bascule, qui est une déclaration du livreur, et continue de répondre
        `can_accept_orders: false` tant que le dossier n'est pas validé."""
        client.post(reverse(APPLY), candidature, format="json")
        code = code_connu("yao@elcorazon.test")
        session = client.post(
            reverse(VERIFY), {"email": "yao@elcorazon.test", "code": code}, format="json"
        )
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.data['access']}")

        response = client.post(reverse(ONLINE), {"is_online": True}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_online"] is True
        assert response.data["can_accept_orders"] is False

    def test_la_connexion_marche_avant_meme_la_verification(
        self, client: APIClient, candidature: dict[str, Any]
    ) -> None:
        """Le mot de passe reste valable : l'application peut donc reconnaître
        le candidat et le renvoyer vers l'écran du code, plutôt que de lui
        opposer « identifiants invalides » sur des identifiants corrects.

        Ce que la session ne lui donne pas, c'est l'accès aux courses — L1 s'en
        charge, et le dossier est en attente.
        """
        client.post(reverse(APPLY), candidature, format="json")

        response = client.post(
            reverse(LOGIN),
            {"email": "yao@elcorazon.test", "password": MOT_DE_PASSE},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"]["email_verified_at"] is None
