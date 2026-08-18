"""Journal de rattrapage — ADR-008.

Le rattrapage ne vaut que s'il est complet. Un événement perdu dans le journal
produit un trou que le client signale mais qu'aucun rejeu ne comble : il sait
qu'il lui manque quelque chose, et personne ne peut le lui donner.

La première rédaction stockait la fenêtre dans **une** clé, relue puis
réécrite. Deux publications concurrentes sur le même groupe repartaient de la
même liste et l'une écrasait l'autre. Une clé par événement supprime le
problème au lieu de le rendre improbable.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from common.realtime import (
    BACKLOG_SIZE,
    courier_group,
    order_group,
    publish,
    replay,
    restaurant_group,
)


@pytest.fixture(autouse=True)
def cache_propre() -> None:
    """Le cache est partagé par la session : sans purge, les numéros de
    séquence d'un test précédent décalent les suivants."""
    cache.clear()


class TestNumerotation:
    def test_les_numeros_se_suivent(self) -> None:
        groupe = order_group("commande-a")

        numeros = [publish(groupe, "order.status", {"n": i}).seq for i in range(3)]

        assert numeros == [1, 2, 3]

    def test_chaque_groupe_compte_pour_lui_meme(self) -> None:
        """Un compteur global ferait croire à un client qu'il a manqué les
        événements d'une commande qui n'est pas la sienne."""
        publish(order_group("a"), "order.status", {})
        publish(order_group("a"), "order.status", {})

        assert publish(order_group("b"), "order.status", {}).seq == 1


class TestRattrapage:
    def test_le_rejeu_rend_ce_qui_suit_le_numero_demande(self) -> None:
        groupe = order_group("commande-b")
        for index in range(5):
            publish(groupe, "tracking.position", {"index": index})

        manques = replay(groupe, since=2)

        assert [event.seq for event in manques] == [3, 4, 5]
        assert manques[0].payload == {"index": 2}

    def test_un_client_a_jour_ne_recoit_rien(self) -> None:
        groupe = order_group("commande-c")
        publish(groupe, "order.status", {})

        assert replay(groupe, since=1) == []

    def test_un_groupe_muet_ne_rend_rien(self) -> None:
        assert replay(order_group("jamais-utilisee"), since=0) == []

    def test_aucun_evenement_ne_se_perd_dans_la_fenetre(self) -> None:
        """Le défaut corrigé : avec une liste relue-réécrite, une publication
        pouvait en écraser une autre et disparaître du journal."""
        groupe = courier_group("livreur-a")
        attendus = 20
        for index in range(attendus):
            publish(groupe, "delivery.offered", {"index": index})

        manques = replay(groupe, since=0)

        assert [event.seq for event in manques] == list(range(1, attendus + 1))
        assert [event.payload["index"] for event in manques] == list(range(attendus))

    def test_la_fenetre_est_bornee(self) -> None:
        """Au-delà, le client a été absent assez longtemps pour que recharger
        l'état par HTTP soit plus juste que rejouer une heure d'historique."""
        groupe = restaurant_group("etablissement-a")
        for index in range(BACKLOG_SIZE + 10):
            publish(groupe, "order.status", {"index": index})

        manques = replay(groupe, since=0)

        assert len(manques) == BACKLOG_SIZE
        assert manques[-1].seq == BACKLOG_SIZE + 10

    def test_un_retard_hors_fenetre_produit_un_trou_visible(self) -> None:
        """C'est ce trou que le consommateur traduit en `realtime.gap` : le
        client doit savoir qu'il lui manque quelque chose plutôt que de croire
        qu'il est à jour."""
        groupe = restaurant_group("etablissement-b")
        for index in range(BACKLOG_SIZE + 5):
            publish(groupe, "order.status", {"index": index})

        manques = replay(groupe, since=1)

        assert manques[0].seq > 2, "le premier rendu doit sauter au-delà du demandé"


class TestNommageDesGroupes:
    def test_les_noms_portent_toujours_l_identifiant(self) -> None:
        """ADR-008 — aucun groupe ne peut être rejoint sans que la vérification
        d'autorisation ait porté sur cet identifiant."""
        assert order_group("abc") == "order.abc.tracking"
        assert courier_group("def") == "courier.def"
        assert restaurant_group("ghi") == "restaurant.ghi"
