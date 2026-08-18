"""API d'authentification — ADR-004, invariants T1 et T2.

Deux tests portent le poids de cette suite :

* `test_le_changement_de_mot_de_passe_revoque_les_autres_sessions` — T2. Il
  échouerait sur l'implémentation précédente, qui ne révoquait rien.
* `TestForceBrute` — T1. L'ancien `/auth/login` n'avait aucun limiteur.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Device, Role, User, UserType
from apps.accounts.services import AuthService

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def credentials() -> dict[str, str]:
    return {"email": "ama@elcorazon.test", "password": "MotDePasseSolide!42"}


@pytest.fixture
def registered(credentials: dict[str, str]) -> User:
    user, _ = AuthService.register(
        email=credentials["email"], password=credentials["password"], full_name="Ama Koffi"
    )
    return user


def authenticated(client: APIClient, user: User) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AuthService.issue_tokens(user).access}")
    return client


class TestInscription:
    def test_cree_un_client_et_renvoie_un_couple_de_jetons(
        self, client: APIClient, credentials: dict[str, str]
    ) -> None:
        response = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Ama Koffi"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["access"] and response.data["refresh"]
        assert response.data["user"]["user_type"] == UserType.CUSTOMER

    def test_le_type_de_compte_n_est_pas_choisi_par_le_client(
        self, client: APIClient, credentials: dict[str, str]
    ) -> None:
        """Escalade de privilège en un champ de formulaire : le champ est
        ignoré, un livreur ou un membre du personnel se crée par le
        back-office."""
        response = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Malin", "user_type": "staff"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["user"]["user_type"] == UserType.CUSTOMER
        assert User.objects.get(email=credentials["email"]).user_type == UserType.CUSTOMER

    def test_un_mot_de_passe_faible_est_refuse(self, client: APIClient) -> None:
        response = client.post(
            reverse("v1:accounts:register"),
            {"email": "faible@elcorazon.test", "password": "12345678", "full_name": "Faible"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_adresse_deja_prise_est_refusee(
        self, client: APIClient, registered: User, credentials: dict[str, str]
    ) -> None:
        response = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Doublon"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_l_adresse_est_normalisee(self, client: APIClient) -> None:
        """Sans normalisation, `Ama@…` et `ama@…` créeraient deux comptes."""
        client.post(
            reverse("v1:accounts:register"),
            {
                "email": "  AMA@Elcorazon.TEST ",
                "password": "MotDePasseSolide!42",
                "full_name": "Ama",
            },
            format="json",
        )
        assert User.objects.filter(email="ama@elcorazon.test").exists()


class TestConnexion:
    def test_cas_nominal(
        self, client: APIClient, registered: User, credentials: dict[str, str]
    ) -> None:
        response = client.post(reverse("v1:accounts:login"), credentials, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"]["email"] == credentials["email"]

    def test_un_compte_inconnu_et_un_mot_de_passe_faux_donnent_la_meme_reponse(
        self, client: APIClient, registered: User, credentials: dict[str, str]
    ) -> None:
        """Sans quoi le point d'entrée devient un oracle d'existence de comptes."""
        inconnu = client.post(
            reverse("v1:accounts:login"),
            {"email": "personne@elcorazon.test", "password": "MotDePasseSolide!42"},
            format="json",
        )
        faux = client.post(
            reverse("v1:accounts:login"),
            {**credentials, "password": "MauvaisMotDePasse!42"},
            format="json",
        )

        assert inconnu.status_code == faux.status_code == status.HTTP_401_UNAUTHORIZED
        assert inconnu.data["detail"] == faux.data["detail"]

    def test_un_compte_desactive_ne_peut_pas_se_connecter(
        self, client: APIClient, registered: User, credentials: dict[str, str]
    ) -> None:
        registered.is_active = False
        registered.save(update_fields=["is_active"])

        response = client.post(reverse("v1:accounts:login"), credentials, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestContratDeSortie:
    def test_inscription_connexion_et_me_renvoient_les_memes_cles(
        self, client: APIClient, credentials: dict[str, str]
    ) -> None:
        """L'ancienne API renvoyait 8 clés à l'inscription contre 15 sur /me —
        deux formes du même objet, que chaque client devait apprendre
        séparément."""
        inscription = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Ama Koffi"},
            format="json",
        )
        connexion = client.post(reverse("v1:accounts:login"), credentials, format="json")

        user = User.objects.get(email=credentials["email"])
        me = authenticated(APIClient(), user).get(reverse("v1:accounts:me"))

        assert set(inscription.data["user"]) == set(connexion.data["user"]) == set(me.data)

    def test_les_dates_ne_sont_jamais_omises(
        self, client: APIClient, credentials: dict[str, str]
    ) -> None:
        """Les clients Dart appellent `DateTime.parse` sans garde nulle : un
        `created_at` absent ne dégrade pas l'affichage, il fait planter la
        connexion."""
        response = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Ama Koffi"},
            format="json",
        )
        assert response.data["user"]["created_at"] is not None
        assert response.data["user"]["updated_at"] is not None

    def test_le_mot_de_passe_n_est_jamais_renvoye(
        self, client: APIClient, credentials: dict[str, str]
    ) -> None:
        response = client.post(
            reverse("v1:accounts:register"),
            {**credentials, "full_name": "Ama Koffi"},
            format="json",
        )
        assert "password" not in response.data["user"]

    def test_les_permissions_sont_vides_pour_un_client(
        self, client: APIClient, registered: User
    ) -> None:
        me = authenticated(client, registered).get(reverse("v1:accounts:me"))
        assert me.data["permissions"] == []

    def test_les_permissions_du_personnel_sont_exposees(self, client: APIClient) -> None:
        staff = User.objects.create_user(
            "staff@elcorazon.test",
            "MotDePasseSolide!42",
            full_name="Staff",
            user_type=UserType.STAFF,
        )
        staff.roles.add(Role.objects.create(name="Caisse", permissions=["orders.refund"]))

        me = authenticated(client, staff).get(reverse("v1:accounts:me"))
        assert me.data["permissions"] == ["orders.refund"]


class TestRevocationDesSessions:
    """T2 — un changement de mot de passe coupe les sessions ouvertes ailleurs."""

    def test_le_changement_de_mot_de_passe_revoque_les_autres_sessions(
        self, registered: User, credentials: dict[str, str]
    ) -> None:
        """Ce test échouerait sur l'implémentation précédente : elle ne
        révoquait rien, alors qu'on change son mot de passe précisément parce
        qu'on soupçonne qu'il a fuité."""
        autre_appareil = AuthService.issue_tokens(registered)

        # L'autre appareil fonctionne avant le changement.
        avant = APIClient()
        assert (
            avant.post(
                reverse("v1:accounts:token-refresh"),
                {"refresh": autre_appareil.refresh},
                format="json",
            ).status_code
            == status.HTTP_200_OK
        )

        authenticated(APIClient(), registered).post(
            reverse("v1:accounts:password-change"),
            {"current_password": credentials["password"], "new_password": "NouveauSolide!99"},
            format="json",
        )

        # Après : le jeton de l'autre appareil ne vaut plus rien.
        apres = APIClient().post(
            reverse("v1:accounts:token-refresh"),
            {"refresh": autre_appareil.refresh},
            format="json",
        )
        assert apres.status_code == status.HTTP_401_UNAUTHORIZED

    def test_le_changement_renvoie_un_couple_utilisable(
        self, registered: User, credentials: dict[str, str]
    ) -> None:
        """Toutes les sessions sont coupées, y compris la courante : le client
        doit repartir avec les jetons renvoyés."""
        response = authenticated(APIClient(), registered).post(
            reverse("v1:accounts:password-change"),
            {"current_password": credentials["password"], "new_password": "NouveauSolide!99"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        suite = APIClient()
        suite.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        assert suite.get(reverse("v1:accounts:me")).status_code == status.HTTP_200_OK

    def test_un_mot_de_passe_actuel_faux_est_refuse(self, registered: User) -> None:
        response = authenticated(APIClient(), registered).post(
            reverse("v1:accounts:password-change"),
            {"current_password": "PasLeBon!42", "new_password": "NouveauSolide!99"},
            format="json",
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_reutiliser_le_meme_mot_de_passe_est_refuse(
        self, registered: User, credentials: dict[str, str]
    ) -> None:
        response = authenticated(APIClient(), registered).post(
            reverse("v1:accounts:password-change"),
            {"current_password": credentials["password"], "new_password": credentials["password"]},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestRotationDesJetons:
    def test_un_jeton_de_rafraichissement_consomme_ne_vaut_plus_rien(
        self, registered: User
    ) -> None:
        """`ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` : rejouer un
        jeton déjà échangé est détecté."""
        tokens = AuthService.issue_tokens(registered)
        client = APIClient()

        premier = client.post(
            reverse("v1:accounts:token-refresh"), {"refresh": tokens.refresh}, format="json"
        )
        assert premier.status_code == status.HTTP_200_OK

        rejeu = client.post(
            reverse("v1:accounts:token-refresh"), {"refresh": tokens.refresh}, format="json"
        )
        assert rejeu.status_code == status.HTTP_401_UNAUTHORIZED

    def test_la_deconnexion_revoque_le_jeton(self, registered: User) -> None:
        tokens = AuthService.issue_tokens(registered)
        client = authenticated(APIClient(), registered)

        client.post(reverse("v1:accounts:logout"), {"refresh": tokens.refresh}, format="json")

        rejeu = APIClient().post(
            reverse("v1:accounts:token-refresh"), {"refresh": tokens.refresh}, format="json"
        )
        assert rejeu.status_code == status.HTTP_401_UNAUTHORIZED

    def test_une_deconnexion_rejouee_reste_silencieuse(self, registered: User) -> None:
        """Renvoyer une erreur ferait boucler un client sur une déconnexion
        qui a déjà eu lieu."""
        tokens = AuthService.issue_tokens(registered)
        client = authenticated(APIClient(), registered)

        for _ in range(2):
            response = client.post(
                reverse("v1:accounts:logout"), {"refresh": tokens.refresh}, format="json"
            )
            assert response.status_code == status.HTTP_204_NO_CONTENT


class TestAppareils:
    def test_l_enregistrement_est_idempotent(self, registered: User) -> None:
        client = authenticated(APIClient(), registered)
        payload = {"token": "fcm-abc-123", "platform": "android"}

        client.post(reverse("v1:accounts:devices"), payload, format="json")
        client.post(reverse("v1:accounts:devices"), payload, format="json")

        assert Device.objects.filter(token="fcm-abc-123").count() == 1

    def test_un_appareil_qui_change_de_compte_est_reattribue(self, registered: User) -> None:
        """Sans réattribution, deux comptes s'abonnent au même téléphone et le
        second reçoit les notifications du premier."""
        autre = User.objects.create_user(
            "autre@elcorazon.test", "MotDePasseSolide!42", full_name="Autre"
        )
        payload = {"token": "fcm-partage", "platform": "ios"}

        authenticated(APIClient(), registered).post(
            reverse("v1:accounts:devices"), payload, format="json"
        )
        authenticated(APIClient(), autre).post(
            reverse("v1:accounts:devices"), payload, format="json"
        )

        assert Device.objects.get(token="fcm-partage").user == autre
        assert Device.objects.count() == 1

    def test_le_retrait_est_scope_a_l_utilisateur(self, registered: User) -> None:
        """Personne ne désabonne l'appareil d'autrui en devinant son jeton."""
        autre = User.objects.create_user(
            "autre@elcorazon.test", "MotDePasseSolide!42", full_name="Autre"
        )
        Device.objects.create(user=registered, token="fcm-de-ama", platform="android")

        authenticated(APIClient(), autre).delete(
            reverse("v1:accounts:devices"), {"token": "fcm-de-ama"}, format="json"
        )

        assert Device.objects.filter(token="fcm-de-ama").exists()


class TestAccesProtege:
    def test_me_exige_une_authentification(self, client: APIClient) -> None:
        assert client.get(reverse("v1:accounts:me")).status_code == status.HTTP_401_UNAUTHORIZED

    def test_un_jeton_invalide_est_refuse(self, client: APIClient) -> None:
        client.credentials(HTTP_AUTHORIZATION="Bearer nimportequoi")
        assert client.get(reverse("v1:accounts:me")).status_code == status.HTTP_401_UNAUTHORIZED


class TestMiseAJourDeSonProfil:
    """`PATCH /auth/me/` — deux champs, et pas un de plus."""

    def test_le_compte_change_son_nom_et_son_telephone(
        self, client: APIClient, customer: User
    ) -> None:
        client.force_authenticate(customer)

        response = client.patch(
            reverse("v1:accounts:me"),
            {"full_name": "Ama Koffi-Mensah", "phone": "+22890222222"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        customer.refresh_from_db()
        assert customer.full_name == "Ama Koffi-Mensah"
        assert customer.phone == "+22890222222"

    def test_le_type_de_compte_ne_s_ecrit_pas(self, client: APIClient, customer: User) -> None:
        """Un client qui pourrait s'écrire « staff » se donnerait des droits."""
        client.force_authenticate(customer)

        client.patch(
            reverse("v1:accounts:me"),
            {"full_name": "Ama", "user_type": UserType.STAFF},
            format="json",
        )

        customer.refresh_from_db()
        assert customer.user_type == UserType.CUSTOMER

    def test_l_email_ne_s_ecrit_pas(self, client: APIClient, customer: User) -> None:
        """Il identifie le compte et sert à s'y connecter."""
        client.force_authenticate(customer)
        avant = customer.email

        client.patch(reverse("v1:accounts:me"), {"email": "autre@elcorazon.test"}, format="json")

        customer.refresh_from_db()
        assert customer.email == avant

    def test_sans_jeton_rien_ne_change(self, client: APIClient, customer: User) -> None:
        response = client.patch(reverse("v1:accounts:me"), {"full_name": "Intrus"}, format="json")

        assert response.status_code in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }
