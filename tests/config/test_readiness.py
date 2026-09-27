"""Sonde de disponibilité — `/ready/`.

## Pourquoi cette sonde existe, et pourquoi elle est séparée de `/health/`

Le déploiement n'en avait qu'une : `/health/`, qui répond « le processus est
vivant » sans toucher à quoi que ce soit. C'est le bon choix pour Render, qui
redémarre le conteneur quand elle échoue — la lier à PostgreSQL ferait
redémarrer en boucle des conteneurs sains le jour d'une indisponibilité de
base, et le redémarrage ne réparant rien, la panne durerait plus longtemps.

Mais rien ne répondait à l'autre question : « les dépendances répondent-elles ? »
Le seul moyen de le savoir était d'appeler une route métier et de lire un 500,
c'est-à-dire de découvrir la panne par une requête d'utilisateur.

Ces tests portent la distinction. Ils vérifient surtout ce que la sonde
**refuse de faire** : mettre le cache et la base sur le même plan, et laisser
fuir un détail de connexion dans la réponse.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestSondeDeDisponibilite:
    def test_toutes_dependances_debout_repond_200(self) -> None:
        reponse = Client().get(reverse("ready"))

        assert reponse.status_code == 200
        corps = reponse.json()
        assert corps["status"] == "ready"
        assert corps["dependencies"]["database"] == "ok"

    def test_la_base_indisponible_repond_503(self) -> None:
        """503 et non 500 : une dépendance absente n'est pas une erreur de
        l'application, et les orchestrateurs distinguent les deux."""
        with mock.patch(
            "config.urls.connection.cursor", side_effect=RuntimeError("connexion refusée")
        ):
            reponse = Client().get(reverse("ready"))

        assert reponse.status_code == 503
        corps = reponse.json()
        assert corps["status"] == "not-ready"
        assert corps["dependencies"]["database"] == "indisponible"

    def test_le_cache_absent_ne_sort_pas_le_serveur_du_service(self) -> None:
        """Le cache est signalé, pas bloquant.

        Redis en offre gratuite est volatil et plafonné ; l'application dégrade
        proprement sans lui — les quotas laissent passer plutôt que de refuser
        (`common.throttling`) — alors qu'elle ne peut rien faire sans base. Les
        traiter pareil ferait sortir du service un serveur encore utile.
        """
        with mock.patch("config.urls.cache.set", side_effect=RuntimeError("redis absent")):
            reponse = Client().get(reverse("ready"))

        assert reponse.status_code == 200
        corps = reponse.json()
        assert corps["status"] == "ready"
        assert corps["dependencies"]["cache"] == "indisponible"

    def test_aucun_detail_de_connexion_ne_fuit_dans_la_reponse(self) -> None:
        """Le message d'une erreur de base porte souvent un hôte, un port,
        parfois un nom d'utilisateur. Il va au journal, jamais au client — la
        sonde est ouverte sans authentification."""
        secret = "postgres://elcorazon:motdepasse@interne:5432/elcorazon"

        with mock.patch("config.urls.connection.cursor", side_effect=RuntimeError(secret)):
            reponse = Client().get(reverse("ready"))

        assert "motdepasse" not in reponse.content.decode()
        assert "interne" not in reponse.content.decode()

    def test_le_schema_a_jour_est_rapporte(self) -> None:
        corps = Client().get(reverse("ready")).json()

        assert corps["dependencies"]["migrations"] == "ok"

    def test_des_migrations_en_attente_repondent_503(self) -> None:
        """Le code en avance sur le schéma : aucune route métier ne répond.

        Panne du 2026-09-13 — un champ ajouté pendant que le conteneur tournait,
        `migrate` n'étant lancé qu'au démarrage. Chaque requête sur un
        établissement rendait 500, pendant que cette sonde disait « ready » ; le
        client, lui, affichait « aucun restaurant en service ».
        """
        with mock.patch("config.urls._migrations_en_attente", return_value=3):
            reponse = Client().get(reverse("ready"))

        assert reponse.status_code == 503
        corps = reponse.json()
        assert corps["status"] == "not-ready"
        assert corps["dependencies"]["migrations"] == "en attente"
        assert corps["dependencies"]["database"] == "ok"

    def test_la_sonde_de_vivacite_reste_insensible_a_la_base(self) -> None:
        """`/health/` ne doit surtout pas suivre `/ready/`.

        C'est elle que Render interroge : la lier à PostgreSQL transformerait
        une indisponibilité de base en boucle de redémarrage.
        """
        with mock.patch(
            "config.urls.connection.cursor", side_effect=RuntimeError("connexion refusée")
        ):
            reponse = Client().get(reverse("health"))

        assert reponse.status_code == 200
        assert reponse.json()["status"] == "ok"

    def test_les_deux_sondes_sont_annoncees_a_la_racine(self) -> None:
        """La racine sert à trouver les points d'entrée sans lire le code."""
        points = Client().get(reverse("index")).json()["endpoints"]

        assert points["health"] == "/health/"
        assert points["ready"] == "/ready/"
