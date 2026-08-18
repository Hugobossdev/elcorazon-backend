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

from typing import Any

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.settings import api_settings
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import User
from apps.catalog.models import MenuItem
from common.throttling import (
    CartWriteThrottle,
    OrderCreationThrottle,
    PaymentInitiationThrottle,
    ReviewWriteThrottle,
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

        assert classes == {"AnonRateThrottle", "UserRateThrottle"}

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
