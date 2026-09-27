"""Vérification de compte et mot de passe oublié — codes à usage unique.

Trois tests portent le poids de cette suite, et chacun échouerait sur une
implémentation naïve du même parcours :

* `test_le_code_n_est_pas_stocke_en_clair` — une copie de la base ne rend pas
  les codes en circulation ;
* `test_le_code_se_ferme_apres_trop_d_essais` — six chiffres, c'est un million
  de possibilités : sans compteur, on les parcourt ;
* `TestPasOracle` — aucune des routes ouvertes ne dit si une adresse
  correspond à un compte.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.hashers import check_password, make_password
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, VerificationCode, VerificationPurpose
from apps.accounts.services import (
    AuthService,
    InvalidVerificationCode,
    VerificationService,
)

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

VERIFY = "v1:accounts:verify"
RESEND = "v1:accounts:verify-resend"
RESET = "v1:accounts:password-reset"
RESET_CONFIRM = "v1:accounts:password-reset-confirm"
LOGIN = "v1:accounts:login"

MOT_DE_PASSE = "MotDePasseSolide!42"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def compte() -> User:
    """Compte non vérifié portant un code vivant — l'état où l'on sort de
    l'inscription."""
    user = User.objects.create_user("kodjo@elcorazon.test", MOT_DE_PASSE, full_name="Kodjo Mensah")
    VerificationService.issue(user=user, purpose=VerificationPurpose.ACCOUNT_VERIFICATION)
    return user


def code_connu(user: User, purpose: str, valeur: str = "123456") -> str:
    """Repose une empreinte **connue** sur le dernier code vivant.

    Le code réel n'est lisible nulle part — c'est tout l'objet du mécanisme, et
    le premier test de ce fichier le vérifie. Les tests substituent donc leur
    propre valeur à son empreinte, ce qui exerce exactement le même chemin de
    présentation.
    """
    record = (
        VerificationCode.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    assert record is not None, "Aucun code vivant : le service n'en a pas émis."
    record.code_hash = make_password(valeur)
    record.save(update_fields=["code_hash"])
    return valeur


class TestEmission:
    def test_l_inscription_emet_un_code(self, compte: User) -> None:
        assert (
            VerificationCode.objects.filter(
                user=compte, purpose=VerificationPurpose.ACCOUNT_VERIFICATION
            ).count()
            == 1
        )

    def test_le_code_n_est_pas_stocke_en_clair(self, compte: User) -> None:
        """Une copie de la base ne doit pas rendre les codes en circulation.

        L'empreinte est produite par le jeu de hacheurs des mots de passe : elle
        porte l'algorithme en préfixe, et ne ressemble en rien à six chiffres.

        Ce test exigeait le préfixe `pbkdf2_`. Il vérifiait donc **l'identité du
        hacheur**, qui est un réglage d'environnement — `config/settings/test.py`
        impose MD5 pour la vitesse de la suite — et non la propriété de
        sécurité. Il échouait pour cette seule raison, sur un code parfaitement
        protégé en production, où aucun réglage ne redéfinit `PASSWORD_HASHERS`
        et où le défaut de Django reste PBKDF2.

        Ce qui compte, et qui est vrai sous n'importe quel hacheur : le code émis
        n'est pas récupérable depuis la ligne, et il reste vérifiable.
        """
        record = VerificationCode.objects.get(user=compte)

        # Ni le code lui-même, ni six chiffres sous une autre forme.
        assert not record.code_hash.isdigit()
        assert len(record.code_hash) > 6

        # L'empreinte porte son algorithme en préfixe, quel qu'il soit — c'est
        # ce qui la distingue d'une valeur écrite en clair.
        assert "$" in record.code_hash

        # Et elle reste une empreinte vérifiable : elle valide le code qu'elle
        # protège et rejette tout autre. Sans cette paire, une colonne remplie
        # d'une constante passerait les assertions ci-dessus.
        #
        # La valeur est reposée plutôt que lue : le code réel n'est lisible
        # nulle part, ce que ce test vérifie précisément.
        valeur = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)
        record.refresh_from_db()
        assert record.code_hash != valeur
        assert check_password(valeur, record.code_hash)
        assert not check_password("000000", record.code_hash)

    def test_un_renvoi_immediat_ne_reexpedie_rien(self, compte: User) -> None:
        """Le délai de garde vit dans le service, pas seulement dans le
        limiteur HTTP : un client mobile qui réémet sa requête sur un réseau
        lent ne doit pas expédier trois courriels pour un geste."""
        premier = VerificationCode.objects.get(user=compte)

        challenge = VerificationService.issue(
            user=compte, purpose=VerificationPurpose.ACCOUNT_VERIFICATION
        )

        assert VerificationCode.objects.filter(user=compte).count() == 1
        assert VerificationCode.objects.get(user=compte).pk == premier.pk
        assert challenge.retry_after > 0

    @override_settings(ACCOUNT_VERIFICATION_RESEND_COOLDOWN_SECONDS=0)
    def test_une_nouvelle_emission_invalide_la_precedente(self, compte: User) -> None:
        """Sinon dix demandes laisseraient dix codes valides, et l'espace à
        deviner serait divisé d'autant."""
        ancien = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)

        VerificationService.issue(user=compte, purpose=VerificationPurpose.ACCOUNT_VERIFICATION)

        assert VerificationCode.objects.filter(user=compte, consumed_at__isnull=True).count() == 1
        with pytest.raises(InvalidVerificationCode):
            VerificationService.consume(
                user=compte, purpose=VerificationPurpose.ACCOUNT_VERIFICATION, code=ancien
            )

    def test_le_courriel_part_apres_le_commit(
        self, django_capture_on_commit_callbacks: Any
    ) -> None:
        """L'envoi est posté sur `on_commit` : une panne de messagerie ne peut
        pas annuler la création du compte qui vient d'aboutir."""
        mail.outbox.clear()

        with django_capture_on_commit_callbacks(execute=True):
            AuthService.register(
                email="ama@elcorazon.test", password=MOT_DE_PASSE, full_name="Ama Koffi"
            )

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ama@elcorazon.test"]

    def test_le_code_expedie_est_bien_celui_qui_valide(
        self, client: APIClient, django_capture_on_commit_callbacks: Any
    ) -> None:
        """Le seul test qui parcoure la chaîne entière, courriel compris.

        Les autres reposent une empreinte connue — c'est nécessaire, le code
        n'est lisible nulle part — mais cela laisse une hypothèse non vérifiée :
        que le code réellement expédié soit bien celui dont l'empreinte est
        écrite. Une inversion à cet endroit rendrait tout le reste vert et le
        parcours impraticable.

        Le code est aussi cherché **hors** du corps du message : il ne doit
        apparaître ni dans la représentation de l'enregistrement — elle finit
        dans les journaux d'administration — ni dans son empreinte.
        """
        import re

        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            AuthService.register(
                email="ama@elcorazon.test", password=MOT_DE_PASSE, full_name="Ama Koffi"
            )

        trouve = re.search(r"\b(\d{6})\b", mail.outbox[0].body)
        assert trouve is not None, f"Aucun code à six chiffres dans :\n{mail.outbox[0].body}"
        code = trouve.group(1)

        record = VerificationCode.objects.get(sent_to="ama@elcorazon.test")
        assert code not in str(record)
        assert code not in record.code_hash

        response = client.post(
            reverse(VERIFY), {"email": "ama@elcorazon.test", "code": code}, format="json"
        )
        assert response.status_code == status.HTTP_200_OK


class TestPresentation:
    def test_un_code_valide_verifie_le_compte_et_ouvre_la_session(
        self, client: APIClient, compte: User
    ) -> None:
        code = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)

        response = client.post(
            reverse(VERIFY), {"email": compte.email, "code": code}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["access"] and response.data["refresh"]
        assert response.data["user"]["email_verified_at"] is not None
        compte.refresh_from_db()
        assert compte.email_verified_at is not None

    def test_un_code_ne_sert_qu_une_fois(self, client: APIClient, compte: User) -> None:
        code = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)
        corps = {"email": compte.email, "code": code}

        assert client.post(reverse(VERIFY), corps, format="json").status_code == 200
        rejoue = client.post(reverse(VERIFY), corps, format="json")

        assert rejoue.status_code == status.HTTP_400_BAD_REQUEST
        assert rejoue.data["code"] == "invalid_verification_code"

    def test_un_code_faux_est_refuse(self, client: APIClient, compte: User) -> None:
        code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)

        response = client.post(
            reverse(VERIFY), {"email": compte.email, "code": "999999"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        compte.refresh_from_db()
        assert compte.email_verified_at is None

    def test_un_code_perime_est_refuse(self, client: APIClient, compte: User) -> None:
        code = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)
        VerificationCode.objects.filter(user=compte).update(expires_at=timezone.now())

        response = client.post(
            reverse(VERIFY), {"email": compte.email, "code": code}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_code_de_mauvaise_longueur_ne_consomme_pas_d_essai(
        self, client: APIClient, compte: User
    ) -> None:
        """Le format est une affaire de sérialiseur : quatre chiffres sont
        refusés avant d'atteindre le compteur, sans quoi un doigt qui glisse
        rapprocherait le livreur de la fermeture de son code."""
        code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)

        response = client.post(
            reverse(VERIFY), {"email": compte.email, "code": "1234"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert VerificationCode.objects.get(user=compte).attempts == 0

    @override_settings(ACCOUNT_VERIFICATION_MAX_ATTEMPTS=3)
    def test_le_code_se_ferme_apres_trop_d_essais(self, client: APIClient, compte: User) -> None:
        """T1 appliqué au code : la limitation HTTP se contourne en répartissant
        les requêtes sur plusieurs origines, le compteur d'essais non."""
        code = code_connu(compte, VerificationPurpose.ACCOUNT_VERIFICATION)

        for _ in range(3):
            rate = client.post(
                reverse(VERIFY), {"email": compte.email, "code": "000000"}, format="json"
            )
            assert rate.status_code == status.HTTP_400_BAD_REQUEST

        # Le bon code ne vaut plus rien : le compteur a fermé l'enregistrement.
        final = client.post(reverse(VERIFY), {"email": compte.email, "code": code}, format="json")

        assert final.status_code == status.HTTP_400_BAD_REQUEST
        assert VerificationCode.objects.get(user=compte).consumed_at is not None

    def test_un_code_de_reinitialisation_ne_verifie_pas_le_compte(
        self, client: APIClient, compte: User
    ) -> None:
        """Le motif est vérifié à la présentation. Sans lui, un seul canal
        suffirait à obtenir les deux effets."""
        VerificationCode.objects.filter(user=compte).update(consumed_at=timezone.now())
        VerificationService.issue(user=compte, purpose=VerificationPurpose.PASSWORD_RESET)
        code = code_connu(compte, VerificationPurpose.PASSWORD_RESET)

        response = client.post(
            reverse(VERIFY), {"email": compte.email, "code": code}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestRenvoi:
    def test_le_renvoi_annonce_les_durees_du_serveur(self, client: APIClient, compte: User) -> None:
        """L'écran anime son compte à rebours sur ces valeurs plutôt que sur une
        constante à lui : deux applications qui les devineraient finiraient par
        proposer « Renvoyer » à un moment où le serveur refuse encore."""
        response = client.post(reverse(RESEND), {"email": compte.email}, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.data["retry_after"] > 0
        assert response.data["code_length"] == 6
        assert response.data["expires_at"] is not None

    def test_un_compte_deja_verifie_ne_recoit_pas_de_code(
        self, client: APIClient, compte: User
    ) -> None:
        """Le code de `/auth/verify/` ouvre une session : en émettre un pour un
        compte vérifié ajouterait une connexion sans mot de passe à qui sait
        relever la boîte."""
        compte.email_verified_at = timezone.now()
        compte.save(update_fields=["email_verified_at"])
        VerificationCode.objects.filter(user=compte).update(consumed_at=timezone.now())

        response = client.post(reverse(RESEND), {"email": compte.email}, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert not VerificationCode.objects.filter(user=compte, consumed_at__isnull=True).exists()


class TestMotDePasseOublie:
    def test_le_parcours_repose_le_mot_de_passe_et_ouvre_la_session(
        self, client: APIClient, compte: User
    ) -> None:
        client.post(reverse(RESET), {"email": compte.email}, format="json")
        code = code_connu(compte, VerificationPurpose.PASSWORD_RESET)

        response = client.post(
            reverse(RESET_CONFIRM),
            {"email": compte.email, "code": code, "new_password": "AutreMotDePasse!77"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["access"] and response.data["refresh"]
        compte.refresh_from_db()
        assert compte.check_password("AutreMotDePasse!77")

    def test_l_ancien_mot_de_passe_ne_vaut_plus(self, client: APIClient, compte: User) -> None:
        client.post(reverse(RESET), {"email": compte.email}, format="json")
        code = code_connu(compte, VerificationPurpose.PASSWORD_RESET)
        client.post(
            reverse(RESET_CONFIRM),
            {"email": compte.email, "code": code, "new_password": "AutreMotDePasse!77"},
            format="json",
        )

        response = client.post(
            reverse(LOGIN), {"email": compte.email, "password": MOT_DE_PASSE}, format="json"
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_la_reinitialisation_revoque_les_sessions_ouvertes(
        self, client: APIClient, compte: User
    ) -> None:
        """T2, poussé un cran plus loin : on réinitialise précisément quand on
        ne sait plus qui détient l'ancien mot de passe."""
        ancienne = AuthService.issue_tokens(compte)
        client.post(reverse(RESET), {"email": compte.email}, format="json")
        code = code_connu(compte, VerificationPurpose.PASSWORD_RESET)

        client.post(
            reverse(RESET_CONFIRM),
            {"email": compte.email, "code": code, "new_password": "AutreMotDePasse!77"},
            format="json",
        )

        rejeu = client.post(
            reverse("v1:accounts:token-refresh"), {"refresh": ancienne.refresh}, format="json"
        )
        assert rejeu.status_code == status.HTTP_401_UNAUTHORIZED

    def test_un_mot_de_passe_faible_ne_brule_pas_le_code(
        self, client: APIClient, compte: User
    ) -> None:
        """Le refus vient du validateur, pas du code : celui-ci doit rester
        présentable, sans quoi choisir un mauvais mot de passe obligerait à
        recommencer la demande depuis le début."""
        client.post(reverse(RESET), {"email": compte.email}, format="json")
        code = code_connu(compte, VerificationPurpose.PASSWORD_RESET)

        refus = client.post(
            reverse(RESET_CONFIRM),
            {"email": compte.email, "code": code, "new_password": "12345678"},
            format="json",
        )
        assert refus.status_code == status.HTTP_400_BAD_REQUEST

        reprise = client.post(
            reverse(RESET_CONFIRM),
            {"email": compte.email, "code": code, "new_password": "AutreMotDePasse!77"},
            format="json",
        )
        assert reprise.status_code == status.HTTP_200_OK

    def test_un_compte_desactive_ne_recoit_pas_de_code(
        self, client: APIClient, compte: User
    ) -> None:
        """Lui rouvrir un mot de passe ne lui rendrait aucun accès, mais lui
        adresserait un courriel après une fermeture."""
        compte.is_active = False
        compte.save(update_fields=["is_active"])

        response = client.post(reverse(RESET), {"email": compte.email}, format="json")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert not VerificationCode.objects.filter(
            user=compte, purpose=VerificationPurpose.PASSWORD_RESET
        ).exists()


class TestPasOracle:
    """Aucune route ouverte ne dit si une adresse correspond à un compte.

    C'est la même règle que sur `/auth/login/`, qui rend un message unique quel
    que soit le motif du refus. Une réponse qui différerait ici transformerait
    ces routes en annuaire d'abonnés — et un annuaire d'abonnés se revend.
    """

    def _sans_les_durees(self, corps: dict[str, Any]) -> dict[str, Any]:
        # `expires_at` est un instant : il diffère forcément d'un appel à
        # l'autre. Ce qui doit coïncider, c'est tout le reste.
        return {clef: valeur for clef, valeur in corps.items() if clef != "expires_at"}

    def test_le_renvoi_repond_pareil_pour_une_adresse_inconnue(
        self, client: APIClient, compte: User
    ) -> None:
        connue = client.post(reverse(RESEND), {"email": compte.email}, format="json")
        inconnue = client.post(reverse(RESEND), {"email": "personne@nulle.part"}, format="json")

        assert connue.status_code == inconnue.status_code == status.HTTP_202_ACCEPTED
        assert self._sans_les_durees(dict(connue.data)) == {
            **self._sans_les_durees(dict(inconnue.data)),
            "email": compte.email,
        }

    def test_la_demande_de_reinitialisation_repond_pareil(
        self, client: APIClient, compte: User
    ) -> None:
        connue = client.post(reverse(RESET), {"email": compte.email}, format="json")
        inconnue = client.post(reverse(RESET), {"email": "personne@nulle.part"}, format="json")

        assert connue.status_code == inconnue.status_code == status.HTTP_202_ACCEPTED
        assert connue.data["detail"] == inconnue.data["detail"]
        assert connue.data["retry_after"] == inconnue.data["retry_after"]

    def test_la_presentation_d_un_code_repond_pareil(self, client: APIClient, compte: User) -> None:
        connue = client.post(
            reverse(VERIFY), {"email": compte.email, "code": "000000"}, format="json"
        )
        inconnue = client.post(
            reverse(VERIFY), {"email": "personne@nulle.part", "code": "000000"}, format="json"
        )

        assert connue.status_code == inconnue.status_code == status.HTTP_400_BAD_REQUEST
        assert connue.data["code"] == inconnue.data["code"] == "invalid_verification_code"
        assert connue.data["detail"] == inconnue.data["detail"]
