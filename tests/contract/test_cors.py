"""Contrat CORS — ce que le navigateur a le droit d'envoyer.

Le reste de la suite passe par le client de test Django, qui appelle les vues
en direct : aucune requête préalable n'y est jamais émise, donc **aucun de ces
tests ne peut voir un blocage CORS**. Les trois applications Flutter tournent
pourtant en web, sur une origine distincte de l'API.

Le défaut que ce module attrape a exactement cette forme : `Idempotency-Key`
(ADR-009) n'était pas dans `CORS_ALLOW_HEADERS`, la requête préalable le
refusait, et la création de commande échouait depuis le navigateur — sans rien
laisser dans les journaux du serveur, puisque la vraie requête n'était jamais
émise. Toute la suite restait verte.

La règle qui en découle : **un en-tête personnalisé ajouté à une requête client
se déclare ici en même temps que dans le client.**
"""

from __future__ import annotations

import importlib
import re
import types

import pytest
from django.test import Client, override_settings

from apps.orders.views import IDEMPOTENCY_HEADER

pytestmark = pytest.mark.contract

# Origine quelconque : elle représente l'application Flutter Web, servie sur un
# autre port que l'API — c'est cette différence, et elle seule, qui déclenche
# la requête préalable.
ORIGINE = "http://localhost:8080"


def preflight(chemin: str, *, methode: str, entetes: str) -> str:
    """Émet une requête préalable et rend les en-têtes que le serveur autorise.

    `CORS_ALLOW_ALL_ORIGINS` est posé ici plutôt que dans les réglages de test :
    ce module est le seul à en avoir besoin, et le poser globalement rendrait
    permissifs des tests qui n'ont rien à voir avec CORS.
    """
    with override_settings(CORS_ALLOW_ALL_ORIGINS=True):
        response = Client().options(
            chemin,
            HTTP_ORIGIN=ORIGINE,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD=methode,
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=entetes,
        )
    return response.headers.get("access-control-allow-headers", "")


class TestEntetesAutorises:
    def test_la_creation_de_commande_peut_porter_sa_cle_d_idempotence(self) -> None:
        """Le cas réel : `POST /orders/` est la seule route qui pose un en-tête
        à nous, et c'était la seule que le navigateur bloquait."""
        autorises = preflight(
            "/api/v1/orders/", methode="POST", entetes=f"content-type,{IDEMPOTENCY_HEADER}"
        )

        # Comparaison en minuscules : la spécification veut l'en-tête
        # insensible à la casse, et `django-cors-headers` rend la liste telle
        # qu'elle est configurée — le client, lui, écrit `Idempotency-Key`.
        assert IDEMPOTENCY_HEADER.lower() in autorises.lower()

    def test_les_entetes_standards_restent_autorises(self) -> None:
        """Garde contre un remplacement de la liste par la nôtre seule :
        `CORS_ALLOW_HEADERS` *étend* les défauts de `django-cors-headers`, et
        sans `authorization` aucune requête authentifiée ne partirait."""
        autorises = preflight(
            "/api/v1/orders/", methode="POST", entetes="content-type,authorization"
        ).lower()

        assert "authorization" in autorises
        assert "content-type" in autorises


@pytest.fixture
def reglages_de_production(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Charge `config.settings.prod` comme module, sans l'activer.

    Les huit variables posées ici sont le prix à payer pour vérifier la valeur
    **réellement livrée** plutôt qu'une copie recopiée dans le test, qui
    dériverait sans rien casser. Quatre sont exigées par les garde-fous de
    `prod.py`, quatre autres par sa messagerie, et la CI n'en fournit aucune :
    sans elles l'import lève, et ce module échouerait en CI seulement.
    """
    for nom, valeur in {
        "DJANGO_SECRET_KEY": "test-only",
        "JWT_SIGNING_KEY": "test-only",
        "JWT_VERIFYING_KEY": "test-only",
        "POSTGRES_PASSWORD": "test-only",
        "EMAIL_HOST": "localhost",
        "EMAIL_HOST_USER": "test-only",
        "EMAIL_HOST_PASSWORD": "test-only",
        "DEFAULT_FROM_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(nom, valeur)

    import config.settings.prod as prod

    return importlib.reload(prod)


class TestOriginesDeDeveloppement:
    """Garde le repli qui autorise `localhost` en production.

    Il existe parce que le service Render, créé à la main, ignore les `envVars`
    du blueprint : `CORS_ALLOWED_ORIGINS` y est vide, et sans lui aucune réponse
    ne porte `Access-Control-Allow-Origin`.
    """

    @staticmethod
    def accepte(prod: types.ModuleType, origine: str) -> bool:
        """Reproduit `corsheaders.middleware.regex_domain_match`.

        `re.match` et non `re.fullmatch` : la bibliothèque compare en
        **préfixe**. C'est ce qui rend l'ancre `$` du motif indispensable, et
        c'est ce que les cas de refus ci-dessous vérifient.
        """
        return any(re.match(motif, origine) for motif in prod.CORS_ALLOWED_ORIGIN_REGEXES)

    @pytest.mark.parametrize(
        "origine",
        [
            "http://localhost:55450",  # port tiré au hasard par Flutter Web
            "http://localhost:5000",  # port fixe de `.vscode/launch.json`
            "http://127.0.0.1:8080",
            "http://localhost",  # sans port : le 80 implicite
        ],
    )
    def test_une_origine_locale_est_acceptee(
        self, reglages_de_production: types.ModuleType, origine: str
    ) -> None:
        """Un port quelconque doit passer : Flutter Web en tire un nouveau à
        chaque lancement, et une origine se déclare au port près."""
        assert self.accepte(reglages_de_production, origine)

    @pytest.mark.parametrize(
        "origine",
        [
            "https://exemple.invalid",
            "http://localhost.exemple.invalid",  # le `$` seul l'écarte
            "http://127.0.0.1.exemple.invalid",
            "http://localhost:5000.exemple.invalid",
        ],
    )
    def test_une_origine_distante_reste_refusee(
        self, reglages_de_production: types.ModuleType, origine: str
    ) -> None:
        """Le repli ouvre `localhost`, pas l'Internet. Les trois derniers cas
        sont des domaines qui *commencent* par une origine locale : sans l'ancre
        finale, `re.match` les accepterait tous."""
        assert not self.accepte(reglages_de_production, origine)

    def test_le_repli_se_desactive_par_l_environnement(
        self, monkeypatch: pytest.MonkeyPatch, reglages_de_production: types.ModuleType
    ) -> None:
        """Le jour où le back-office a une adresse stable, la déclarer dans
        `CORS_ALLOWED_ORIGINS` est plus étroit — encore faut-il pouvoir fermer
        celle-ci."""
        monkeypatch.setenv("CORS_ALLOW_LOCAL_DEV_ORIGINS", "False")
        prod = importlib.reload(reglages_de_production)

        assert prod.CORS_ALLOW_LOCAL_DEV_ORIGINS is False
        assert not getattr(prod, "CORS_ALLOWED_ORIGIN_REGEXES", [])
