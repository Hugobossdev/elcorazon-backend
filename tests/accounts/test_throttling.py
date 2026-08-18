"""Limitation de débit sur l'authentification — T1.

L'implémentation précédente n'avait aucun limiteur sur `/auth/login` : la force
brute était ouverte.

Les réglages de test désactivent la limitation par défaut — sinon le sixième
test qui s'authentifie recevrait un 429. Elle est donc réactivée explicitement
ici, ce qui a l'avantage de rendre visible ce que chaque test mesure.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.throttling import AuthIdentifierThrottle, AuthIPThrottle

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

# Quotas resserrés : mesurer un blocage à 5 tentatives demanderait 6 requêtes
# par test, ce qui n'apporte rien de plus que 3.
QUOTAS = {"auth_ip": "4/min", "auth_identifier": "2/min"}


@pytest.fixture(autouse=True)
def _compteurs_propres():
    """Les compteurs vivent dans le cache, partagé entre les tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def throttled():
    """Réactive la limitation, désactivée par les réglages de test.

    `override_settings` ne suffirait pas : DRF évalue `THROTTLE_RATES` **dans
    le corps de la classe**, donc une seule fois à l'import. Modifier le
    réglage après coup ne l'atteint plus. On pilote donc l'attribut de classe
    directement — moins élégant, mais c'est ce qui marche, et c'est vérifié par
    les tests eux-mêmes.
    """
    classes = (AuthIPThrottle, AuthIdentifierThrottle)
    anciens = [(c, c.THROTTLE_RATES, getattr(c, "rate", None)) for c in classes]

    for classe in classes:
        classe.THROTTLE_RATES = QUOTAS
        classe.rate = None

    yield

    for classe, rates, rate in anciens:
        classe.THROTTLE_RATES = rates
        classe.rate = rate


def tentative(client: APIClient, email: str) -> int:
    return client.post(
        reverse("v1:accounts:login"),
        {"email": email, "password": "MauvaisMotDePasse!42"},
        format="json",
    ).status_code


class TestForceBrute:
    def test_les_tentatives_sur_un_meme_compte_sont_bornees(self, throttled) -> None:
        """Le limiteur par identifiant : deux essais, puis blocage."""
        client = APIClient()

        assert tentative(client, "cible@elcorazon.test") == status.HTTP_401_UNAUTHORIZED
        assert tentative(client, "cible@elcorazon.test") == status.HTTP_401_UNAUTHORIZED
        assert tentative(client, "cible@elcorazon.test") == status.HTTP_429_TOO_MANY_REQUESTS

    def test_changer_d_adresse_ip_ne_contourne_pas_le_limiteur(self, throttled) -> None:
        """Le point décisif.

        Un botnet réparti sur mille adresses passe sous le limiteur par IP sans
        jamais le déclencher, tout en essayant mille mots de passe sur le même
        compte. Seul le comptage par identifiant l'arrête — c'est pour cela
        qu'il existe.
        """
        for ip in ("10.0.0.1", "10.0.0.2"):
            client = APIClient(REMOTE_ADDR=ip)
            assert tentative(client, "cible@elcorazon.test") == status.HTTP_401_UNAUTHORIZED

        troisieme = APIClient(REMOTE_ADDR="10.0.0.3")
        assert tentative(troisieme, "cible@elcorazon.test") == status.HTTP_429_TOO_MANY_REQUESTS

    def test_le_blocage_d_un_compte_n_affecte_pas_les_autres(self, throttled) -> None:
        """Sinon la protection devient une arme : saturer le compteur d'autrui
        suffirait à l'empêcher de se connecter."""
        client = APIClient()
        tentative(client, "cible@elcorazon.test")
        tentative(client, "cible@elcorazon.test")
        assert tentative(client, "cible@elcorazon.test") == status.HTTP_429_TOO_MANY_REQUESTS

        assert tentative(APIClient(), "autre@elcorazon.test") == status.HTTP_401_UNAUTHORIZED

    def test_une_requete_sans_identifiant_ne_consomme_pas_de_quota(self, throttled) -> None:
        """Sinon on saturerait le compteur d'un tiers en envoyant du vide, ce
        qui transformerait la protection en déni de service."""
        client = APIClient()
        for _ in range(3):
            client.post(reverse("v1:accounts:login"), {"password": "x"}, format="json")

        assert tentative(APIClient(), "cible@elcorazon.test") == status.HTTP_401_UNAUTHORIZED


class TestReponse429:
    def test_le_format_est_celui_des_erreurs_metier(self, throttled) -> None:
        """ADR-009 : toutes les erreurs sortent en RFC 9457, y compris celle-ci.
        Un client n'a pas à traiter deux formats."""
        client = APIClient()
        for _ in range(3):
            reponse = client.post(
                reverse("v1:accounts:login"),
                {"email": "cible@elcorazon.test", "password": "MauvaisMotDePasse!42"},
                format="json",
            )

        assert reponse.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert reponse.data["status"] == 429
        assert "code" in reponse.data


class TestSansLimiteur:
    def test_les_reglages_de_test_desactivent_la_limitation(self) -> None:
        """Documente le choix : sans cela, toute suite qui s'authentifie plus
        de cinq fois échouerait pour une raison sans rapport avec son objet."""
        client = APIClient()
        for _ in range(8):
            assert tentative(client, "cible@elcorazon.test") == status.HTTP_401_UNAUTHORIZED
