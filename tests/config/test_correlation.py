"""Corrélation des journaux — `common.observabilite`.

Les journaux sortaient déjà en JSON structuré, donc interrogeables par champ.
Rien ne reliait pourtant entre elles les lignes d'une **même** requête : sous
quelques requêtes par seconde, les traces d'un incident se mêlent à celles de
tout le monde.

Ces tests portent les trois propriétés qui font qu'un identifiant de corrélation
sert à quelque chose : il apparaît sur toute ligne journalisée, il revient au
client pour qu'il puisse le citer, et il ne fuit pas d'une requête à l'autre.
"""

from __future__ import annotations

import logging

import pytest
from django.test import Client
from django.urls import reverse

from common.observabilite import ENTETE, ID_REQUETE, FiltreDeCorrelation

pytestmark = pytest.mark.django_db


def _enregistrement() -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, "fichier", 1, "message", None, None)


class TestIdentifiantDeRequete:
    def test_la_reponse_porte_un_identifiant(self) -> None:
        """Sans lui, un rapport d'incident se réduit à « ça n'a pas marché »."""
        reponse = Client().get(reverse("health"))

        assert reponse.headers[ENTETE]
        assert len(reponse.headers[ENTETE]) == 32

    def test_un_identifiant_fourni_par_l_appelant_est_repris(self) -> None:
        """La trace traverse la frontière au lieu de recommencer à chaque saut."""
        reponse = Client().get(reverse("health"), headers={"x-request-id": "trace-amont-42"})

        assert reponse.headers[ENTETE] == "trace-amont-42"

    def test_un_identifiant_demesure_est_borne(self) -> None:
        """Il vient du client et finit recopié sur chaque ligne de journal."""
        reponse = Client().get(reverse("health"), headers={"x-request-id": "x" * 500})

        assert len(reponse.headers[ENTETE]) == 64

    def test_deux_requetes_ont_des_identifiants_distincts(self) -> None:
        client = Client()

        premiere = client.get(reverse("health")).headers[ENTETE]
        seconde = client.get(reverse("health")).headers[ENTETE]

        assert premiere != seconde

    def test_le_contexte_ne_fuit_pas_apres_la_requete(self) -> None:
        """Sans la remise à zéro, l'identifiant contaminerait la requête
        suivante servie par le même fil — et deux incidents distincts
        porteraient la même trace."""
        Client().get(reverse("health"))

        assert ID_REQUETE.get() == ""


class TestFiltreDeJournalisation:
    def test_l_identifiant_est_pose_sur_l_enregistrement(self) -> None:
        jeton = ID_REQUETE.set("abc123")
        try:
            record = _enregistrement()
            FiltreDeCorrelation().filter(record)
            assert record.request_id == "abc123"
        finally:
            ID_REQUETE.reset(jeton)

    def test_hors_requete_aucun_champ_n_est_ajoute(self) -> None:
        """Une tâche Celery ou une commande d'administration n'a pas de requête.

        Y poser une chaîne vide ferait apparaître un champ `request_id: ""` sur
        chaque ligne — du bruit qui ressemble à une valeur.
        """
        record = _enregistrement()

        FiltreDeCorrelation().filter(record)

        assert not hasattr(record, "request_id")

    def test_le_filtre_ne_filtre_jamais_rien(self) -> None:
        """Il enrichit. Un `False` ici ferait disparaître des journaux."""
        assert FiltreDeCorrelation().filter(_enregistrement()) is True
