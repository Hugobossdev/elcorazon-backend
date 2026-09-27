"""Limitation de débit — couverture, quotas et identification de l'appelant.

Trois choses se vérifient ici, de nature différente :

* **la couverture** — aucune route n'est sans quota. C'est le défaut corrigé :
  la limitation n'était déclarée que sur neuf vues sur cent trente-six, et les
  plus coûteuses — passage de commande, initiation de paiement — n'en avaient
  aucune ;
* **le quota lui-même** — qu'il compte, et qu'il coupe ;
* **l'identification** — qu'un appelant ne puisse pas s'en fabriquer une neuve
  en variant un en-tête. C'était vrai jusqu'ici, et cela rendait le limiteur
  par IP décoratif.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.core.cache import cache
from django.urls import reverse
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework import status
from rest_framework.settings import api_settings
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import User
from apps.accounts.throttling import AuthIdentifierThrottle, AuthIPThrottle
from apps.catalog.models import MenuItem
from common.throttling import (
    CartWriteThrottle,
    FailClosedOnCacheOutage,
    FailOpenOnCacheOutage,
    OrderCreationThrottle,
    PaymentInitiationThrottle,
    ResilientAnonRateThrottle,
    ResilientUserRateThrottle,
    ReviewWriteThrottle,
    RewardRedemptionThrottle,
    TrackingPingThrottle,
)

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture(autouse=True)
def _compteurs_propres() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def quota_serre() -> Any:
    """Réactive la limitation, désactivée par les réglages de test.

    Comme pour l'authentification : DRF lit `THROTTLE_RATES` dans le corps de
    la classe, une seule fois à l'import, donc `override_settings` ne
    l'atteint plus. On pilote l'attribut de classe directement.
    """
    classes = (
        CartWriteThrottle,
        OrderCreationThrottle,
        PaymentInitiationThrottle,
        ReviewWriteThrottle,
        TrackingPingThrottle,
    )
    quotas = {classe.scope: "2/min" for classe in classes}
    anciens = [(c, c.THROTTLE_RATES, getattr(c, "rate", None)) for c in classes]

    for classe in classes:
        classe.THROTTLE_RATES = quotas
        classe.rate = None

    yield

    for classe, rates, rate in anciens:
        classe.THROTTLE_RATES = rates
        classe.rate = rate


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


class TestCouverture:
    """Le défaut corrigé : la limitation n'était pas le défaut."""

    def test_un_limiteur_s_applique_partout_par_defaut(self) -> None:
        classes = {classe.__name__ for classe in api_settings.DEFAULT_THROTTLE_CLASSES}

        assert classes == {"ResilientAnonRateThrottle", "ResilientUserRateThrottle"}

    def test_le_socle_par_defaut_ne_tombe_pas_avec_le_cache(self) -> None:
        """Les classes de DRF laissent remonter l'erreur de connexion en 500
        depuis `check_throttles`, donc avant la vue : un cache injoignable
        rabattait toute l'API publique, catalogue compris."""
        for classe in api_settings.DEFAULT_THROTTLE_CLASSES:
            assert issubclass(classe, FailOpenOnCacheOutage), classe.__name__

    def test_chaque_quota_nomme_a_un_taux(self) -> None:
        """Un `scope` sans taux ne protège rien : DRF lève
        `ImproperlyConfigured` à la première requête, et seulement sur la route
        concernée — c'est-à-dire en production, sur une route peu passante."""
        from django.conf import settings

        taux = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        for classe in (
            CartWriteThrottle,
            OrderCreationThrottle,
            PaymentInitiationThrottle,
            ReviewWriteThrottle,
            TrackingPingThrottle,
        ):
            assert classe.scope in taux, classe.__name__

    def test_les_ecritures_couteuses_ont_leur_propre_quota(self) -> None:
        """Le socle `user` à 120/min conviendrait à la lecture et laisserait
        passer douze commandes par minute et par compte."""
        from apps.carts.views import CartViewSet
        from apps.orders.views import OrderViewSet
        from apps.payments.views import InitiatePaymentView
        from apps.tracking.views import PingView

        assert CartWriteThrottle in CartViewSet.throttle_classes
        assert PaymentInitiationThrottle in InitiatePaymentView.throttle_classes
        assert TrackingPingThrottle in PingView.throttle_classes

        viewset = OrderViewSet()
        viewset.action = "create"
        assert isinstance(viewset.get_throttles()[0], OrderCreationThrottle)

    def test_la_lecture_de_commandes_garde_le_socle(self) -> None:
        """Consulter son historique est bon marché : lui appliquer le quota de
        création reviendrait à choisir entre gêner la lecture et laisser
        l'écriture ouverte."""
        from apps.orders.views import OrderViewSet

        viewset = OrderViewSet()
        viewset.action = "list"

        assert not any(isinstance(t, OrderCreationThrottle) for t in viewset.get_throttles())


class TestQuotasEffectifs:
    def test_le_panier_est_borne_en_ecriture(
        self, as_customer: APIClient, restaurant: object, menu_item: MenuItem, quota_serre: Any
    ) -> None:
        url = reverse("v1:carts:cart-add-line", args=[restaurant.slug])  # type: ignore[attr-defined]
        corps = {"menu_item": str(menu_item.pk)}

        codes = [as_customer.post(url, corps, format="json").status_code for _ in range(3)]

        assert codes[-1] == status.HTTP_429_TOO_MANY_REQUESTS

    def test_le_depot_d_avis_est_borne(
        self, as_customer: APIClient, menu_item: MenuItem, quota_serre: Any
    ) -> None:
        """S5 limite déjà à un avis par article : le quota arrête le
        remplissage automatisé sur plusieurs articles."""
        url = reverse("v1:catalog:review-list")
        corps = {"menu_item": str(menu_item.pk), "rating": 5}

        codes = [as_customer.post(url, corps, format="json").status_code for _ in range(3)]

        assert codes[-1] == status.HTTP_429_TOO_MANY_REQUESTS

    def test_la_lecture_d_avis_n_est_pas_bornee_par_l_ecriture(
        self, as_customer: APIClient, menu_item: MenuItem, quota_serre: Any
    ) -> None:
        """Les avis se lisent librement : appliquer le quota d'écriture à la
        lecture rendrait une page de menu inaffichable."""
        url = reverse("v1:catalog:review-list")

        codes = [as_customer.get(url).status_code for _ in range(4)]

        assert set(codes) == {status.HTTP_200_OK}


class TestReponseDeRefus:
    def test_un_429_dit_combien_de_temps_attendre(
        self, as_customer: APIClient, menu_item: MenuItem, quota_serre: Any
    ) -> None:
        """Sans `Retry-After`, la seule stratégie qui reste au client est de
        réessayer tout de suite — c'est-à-dire d'aggraver ce que la limitation
        cherche à contenir. Le gestionnaire RFC 9457 reconstruit la réponse et
        perdait l'en-tête."""
        url = reverse("v1:catalog:review-list")
        corps = {"menu_item": str(menu_item.pk), "rating": 5}
        for _ in range(3):
            reponse = as_customer.post(url, corps, format="json")

        assert reponse.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Retry-After" in reponse.headers
        assert int(reponse.headers["Retry-After"]) > 0

    def test_le_refus_garde_la_forme_des_autres_erreurs(
        self, as_customer: APIClient, menu_item: MenuItem, quota_serre: Any
    ) -> None:
        """RFC 9457 comme partout ailleurs : le client lit `code`, jamais
        `detail`."""
        url = reverse("v1:catalog:review-list")
        corps = {"menu_item": str(menu_item.pk), "rating": 5}
        for _ in range(3):
            reponse = as_customer.post(url, corps, format="json")

        assert reponse["Content-Type"].startswith("application/problem+json")
        assert reponse.data["code"] == "throttled"


class TestIdentificationDeLAppelant:
    """Le limiteur par IP était contournable en variant un en-tête.

    Nginx **ajoute** à `X-Forwarded-For` au lieu de le remplacer. Sans
    `NUM_PROXIES`, DRF prend la chaîne entière : un client qui envoie son
    propre en-tête obtient une identité neuve à chaque requête, et le compteur
    ne compte plus rien.
    """

    def test_le_nombre_de_proxys_est_declare(self) -> None:
        assert api_settings.NUM_PROXIES is not None, (
            "Sans ce réglage, l'identification par IP se falsifie par en-tête."
        )

    def test_un_en_tete_forge_ne_change_pas_l_identite(self, rf: Any) -> None:
        """Ce que Nginx a inscrit lui-même l'emporte sur ce que le client a
        prétendu."""
        throttle = SimpleRateThrottle.__new__(SimpleRateThrottle)

        requete = rf.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.1", REMOTE_ADDR="10.0.0.1")
        autre = rf.get("/", HTTP_X_FORWARDED_FOR="9.9.9.9, 10.0.0.1", REMOTE_ADDR="10.0.0.1")

        assert throttle.get_ident(requete) == throttle.get_ident(autre) == "10.0.0.1"

    def test_sans_proxy_declare_l_identite_serait_falsifiable(self, rf: Any, settings: Any) -> None:
        """La démonstration du défaut, laissée en test : elle explique pourquoi
        le réglage précédent n'est pas facultatif."""
        api_settings.reload()
        try:
            settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": None}
            api_settings.reload()

            throttle = SimpleRateThrottle.__new__(SimpleRateThrottle)
            requete = rf.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.1", REMOTE_ADDR="10.0.0.1")
            autre = rf.get("/", HTTP_X_FORWARDED_FOR="9.9.9.9, 10.0.0.1", REMOTE_ADDR="10.0.0.1")

            assert throttle.get_ident(requete) != throttle.get_ident(autre)
        finally:
            api_settings.reload()


class _CacheInjoignable:
    """Le cache tel qu'il se comporte quand Redis ne répond plus.

    `django_redis` relaie l'erreur brute de `redis` (`raise e.__cause__`) et pas
    seulement son enveloppe `ConnectionInterrupted` : c'est cette forme-là qui
    remontait jusqu'à DRF, donc celle qu'il faut imiter ici.
    """

    message = "Error 111 connecting to cache:6379. Connection refused."

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError(self.message)

    def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError(self.message)


@pytest.fixture
def cache_injoignable(monkeypatch: Any) -> None:
    """Coupe le cache pour tous les limiteurs, sans toucher au reste.

    `SimpleRateThrottle.cache` est un attribut de classe résolu par héritage :
    le remplacer là couvre les quatre familles de limiteurs d'un coup.
    """
    monkeypatch.setattr(SimpleRateThrottle, "cache", _CacheInjoignable())


@pytest.fixture
def quotas_opposables() -> Any:
    """Rend les quotas actifs — les réglages de test les neutralisent tous.

    Sans taux, `allow_request` sort sur `rate is None` avant même d'avoir
    touché au cache : un test de panne passerait alors sans rien démontrer.
    """
    classes = (
        ResilientAnonRateThrottle,
        ResilientUserRateThrottle,
        AuthIPThrottle,
        AuthIdentifierThrottle,
    )
    quotas = {classe.scope: "60/min" for classe in classes}
    anciens = [(c, c.THROTTLE_RATES, getattr(c, "rate", None)) for c in classes]

    for classe in classes:
        classe.THROTTLE_RATES = quotas
        classe.rate = None

    yield

    for classe, rates, rate in anciens:
        classe.THROTTLE_RATES = rates
        classe.rate = rate


class TestPanneDeCache:
    """Ce qu'un cache injoignable doit faire — et ne plus faire.

    Panne réelle, en production, le 19/08/2026 : `REDIS_URL` portait le nom de
    service docker-compose `redis`, qui ne résout pas chez l'hébergeur. Tout ce
    qui franchissait la permission atteignait `check_throttles`, y lisait le
    cache, et repartait en 500 — carte et catalogue compris. Les routes
    protégées, elles, mouraient en 401 avant d'y arriver : c'est cette
    frontière 401/500 qui a désigné le cache plutôt que la base.
    """

    def test_le_catalogue_reste_lisible(
        self, menu_item: MenuItem, quotas_opposables: Any, cache_injoignable: None
    ) -> None:
        """Le défaut corrigé : la carte tombait avec le compteur."""
        reponse = APIClient().get(reverse("v1:catalog:item-list"))

        assert reponse.status_code == status.HTTP_200_OK

    def test_l_authentification_refuse_en_503(
        self, customer: User, quotas_opposables: Any, cache_injoignable: None
    ) -> None:
        """Refuser, et non laisser passer : ici le compteur *est* la protection
        contre la force brute, et une panne de cache, un attaquant peut la
        provoquer plutôt que l'attendre."""
        reponse = APIClient().post(
            reverse("v1:accounts:login"),
            {"email": customer.email, "password": "peu importe"},
            format="json",
        )

        assert reponse.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    def test_le_refus_garde_la_forme_des_autres_erreurs(
        self, customer: User, quotas_opposables: Any, cache_injoignable: None
    ) -> None:
        """RFC 9457 et un `code` stable. Le 503 dit ce que le 500 taisait :
        l'indisponibilité est passagère, et réessayer est la bonne réponse."""
        reponse = APIClient().post(
            reverse("v1:accounts:login"),
            {"email": customer.email, "password": "peu importe"},
            format="json",
        )

        assert reponse["Content-Type"].startswith("application/problem+json")
        assert reponse.data["code"] == "quota_unavailable"

    def test_la_requete_laissee_passer_est_journalisee(
        self,
        menu_item: MenuItem,
        quotas_opposables: Any,
        cache_injoignable: None,
        caplog: Any,
    ) -> None:
        """Un quota qui s'efface en silence est un quota qu'on croit encore
        appliqué. La panne doit rester lisible même quand elle ne casse plus
        rien."""
        with caplog.at_level(logging.WARNING, logger="common.throttling"):
            APIClient().get(reverse("v1:catalog:item-list"))

        assert any("cache injoignable" in message for message in caplog.messages)


class TestPolitiqueParClasse:
    """Chaque limiteur déclare ce qu'il fait sans cache — aucun ne s'abstient.

    Le partage ne suit pas le coût de l'opération mais ce qui reste debout sans
    le compteur : un catalogue sans quota reste un catalogue lisible, un
    `/auth/login` sans quota est une porte ouverte.
    """

    def test_les_quotas_qui_sont_la_seule_protection_ferment(self) -> None:
        for classe in (
            AuthIPThrottle,
            AuthIdentifierThrottle,
            OrderCreationThrottle,
            PaymentInitiationThrottle,
            RewardRedemptionThrottle,
        ):
            assert issubclass(classe, FailClosedOnCacheOutage), classe.__name__

    def test_les_quotas_de_precaution_s_effacent(self) -> None:
        for classe in (
            ResilientAnonRateThrottle,
            ResilientUserRateThrottle,
            CartWriteThrottle,
            ReviewWriteThrottle,
            TrackingPingThrottle,
        ):
            assert issubclass(classe, FailOpenOnCacheOutage), classe.__name__

    def test_le_webhook_du_prestataire_ne_se_ferme_pas(self) -> None:
        """Ce qui garde cette route est la signature du prestataire, pas le
        compteur. La fermer perdrait des confirmations de paiement — donc des
        commandes payées mais jamais marquées telles — pour protéger une porte
        déjà fermée à clé."""
        from apps.payments.views import WebhookView

        assert issubclass(WebhookView.throttle_classes[0], FailOpenOnCacheOutage)
