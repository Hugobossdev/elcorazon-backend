"""Tests des machines à états — ADR-010.

Ces tests exercent les mécanismes qui ferment C3, C4, C5, P1 et L2. Le plus
important est `test_toute_transition_non_declaree_est_refusee` : il couvre les
46 couples d'états qu'on n'aurait jamais écrits à la main, et c'est précisément
là que vivaient les failles de l'implémentation précédente.
"""

from __future__ import annotations

import pytest

from common.state_machine import IllegalTransition, StateMachine

# Le cycle de vie réel d'une commande (Phase 1 §6.1). Reproduit ici pour que le
# moteur soit exercé sur le graphe qu'il devra porter en production.
ORDER_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"ready", "cancelled"},
    "ready": {"picked_up", "cancelled"},
    "picked_up": {"on_the_way"},
    "on_the_way": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}


@pytest.fixture
def order_machine() -> StateMachine:
    return StateMachine(ORDER_TRANSITIONS, name="commande")


class TestDeclaration:
    """Le graphe est vérifié à la construction — donc à l'import, donc en CI."""

    def test_refuse_un_etat_cible_non_declare(self) -> None:
        """Garde contre C4.

        L'implémentation précédente projetait sur la commande un statut
        `accepted` absent de son énumération, ce qui aurait violé la contrainte
        CHECK en production. Ici, le module ne s'importe pas.
        """
        with pytest.raises(ValueError, match="non déclarés"):
            StateMachine({"a": {"b"}}, name="incomplete")

    def test_refuse_un_cycle(self) -> None:
        """Garde contre C3.

        Rejouer `delivered` réincrémentait les compteurs du livreur. Un graphe
        acyclique rend le retour arrière inexprimable.
        """
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            StateMachine({"a": {"b"}, "b": {"a"}}, name="cyclique")

    def test_refuse_un_cycle_indirect(self) -> None:
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            StateMachine({"a": {"b"}, "b": {"c"}, "c": {"a"}}, name="cyclique")

    def test_accepte_un_graphe_convergent(self) -> None:
        """Deux chemins vers un même état terminal ne forment pas un cycle."""
        machine = StateMachine({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()})
        assert machine.is_terminal("d")

    def test_le_graphe_de_commande_est_valide(self, order_machine: StateMachine) -> None:
        assert len(order_machine.states) == 8


class TestTransitions:
    def test_transition_declaree(self, order_machine: StateMachine) -> None:
        order_machine.validate("pending", "confirmed")  # ne lève pas

    def test_transition_non_declaree(self, order_machine: StateMachine) -> None:
        with pytest.raises(IllegalTransition) as exc:
            order_machine.validate("pending", "delivered")
        assert exc.value.source == "pending"
        assert exc.value.target == "delivered"
        assert exc.value.allowed == ["cancelled", "confirmed"]

    def test_le_message_enumere_les_transitions_possibles(
        self, order_machine: StateMachine
    ) -> None:
        """Le message doit être exploitable par le client sans ré-analyse."""
        with pytest.raises(IllegalTransition, match="cancelled, confirmed"):
            order_machine.validate("pending", "ready")

    def test_toute_transition_non_declaree_est_refusee(self, order_machine: StateMachine) -> None:
        """Test de propriété : les 46 couples non déclarés doivent tous échouer.

        8 états donnent 64 couples, moins 8 diagonales (traitées par
        `is_noop`), moins les 10 transitions déclarées : 46 refus attendus.

        C'est le test qui aurait attrapé C3 et C4. Aucun développeur n'écrit à
        la main la vérification que `delivered → preparing` est refusée ; c'est
        pourtant exactement le cas qui a coûté des livraisons fantômes.
        """
        declared = sum(len(t) for t in ORDER_TRANSITIONS.values())
        states = len(ORDER_TRANSITIONS)
        illegal = order_machine.illegal_pairs()

        assert len(illegal) == states * states - states - declared == 46

        for source, target in illegal:
            with pytest.raises(IllegalTransition):
                order_machine.validate(source, target)

    def test_etat_inconnu(self, order_machine: StateMachine) -> None:
        with pytest.raises(ValueError, match="inconnu"):
            order_machine.validate("inexistant", "confirmed")


class TestMonotonie:
    """C3 — aucun retour arrière n'est exprimable."""

    def test_les_etats_terminaux_ne_mènent_nulle_part(self, order_machine: StateMachine) -> None:
        assert order_machine.is_terminal("delivered")
        assert order_machine.is_terminal("cancelled")
        assert order_machine.targets_from("delivered") == frozenset()

    def test_une_commande_livree_ne_revient_jamais_en_arriere(
        self, order_machine: StateMachine
    ) -> None:
        for state in order_machine.states:
            assert not order_machine.can("delivered", state)

    def test_une_commande_annulee_ne_peut_plus_etre_prise_en_charge(
        self, order_machine: StateMachine
    ) -> None:
        """C5 — une commande annulée n'accepte plus ni paiement ni prise en charge."""
        assert not order_machine.can("cancelled", "picked_up")
        assert not order_machine.can("cancelled", "confirmed")

    def test_l_annulation_n_est_plus_possible_apres_enlevement(
        self, order_machine: StateMachine
    ) -> None:
        """Règle métier : une fois le repas parti, il n'est plus annulable."""
        assert not order_machine.can("picked_up", "cancelled")
        assert not order_machine.can("on_the_way", "cancelled")


class TestIdempotence:
    """P1 — rejouer un webhook ne doit ni réécrire, ni échouer."""

    def test_transition_vers_l_etat_courant(self, order_machine: StateMachine) -> None:
        assert order_machine.is_noop("delivered", "delivered")

    def test_une_transition_reelle_n_est_pas_un_noop(self, order_machine: StateMachine) -> None:
        assert not order_machine.is_noop("pending", "confirmed")

    def test_un_noop_n_est_pas_une_transition_declaree(self, order_machine: StateMachine) -> None:
        """Distinction volontaire : `is_noop` est vrai, `can` est faux.

        L'appelant doit tester `is_noop` d'abord et sortir sans effet. Sans
        cette distinction, un webhook rejoué renverrait une erreur au
        prestataire, qui retenterait indéfiniment.
        """
        assert order_machine.is_noop("delivered", "delivered")
        assert not order_machine.can("delivered", "delivered")


class TestAnalyse:
    def test_atteignabilite(self, order_machine: StateMachine) -> None:
        assert order_machine.reachable_from("on_the_way") == frozenset({"delivered"})
        assert "delivered" in order_machine.reachable_from("pending")

    def test_un_etat_terminal_n_atteint_rien(self, order_machine: StateMachine) -> None:
        assert order_machine.reachable_from("delivered") == frozenset()


class TestAutresMachines:
    """Les trois autres cycles de vie identifiés en Phase 1."""

    def test_livraison(self) -> None:
        machine = StateMachine(
            {
                "assigned": {"accepted", "cancelled"},
                "accepted": {"picked_up", "cancelled"},
                "picked_up": {"on_the_way"},
                "on_the_way": {"delivered"},
                "delivered": set(),
                "cancelled": set(),
            },
            name="livraison",
        )
        # L4 — les compteurs ne peuvent être incrémentés qu'une fois.
        assert machine.is_terminal("delivered")

    def test_paiement(self) -> None:
        machine = StateMachine(
            {
                "pending": {"processing", "failed", "cancelled"},
                "processing": {"completed", "failed"},
                "completed": {"refunded"},
                "refunded": set(),
                "failed": set(),
                "cancelled": set(),
            },
            name="paiement",
        )
        # P1 — un paiement encaissé ne redescend jamais vers `pending`.
        assert not machine.can("completed", "pending")
        assert not machine.can("completed", "processing")

    def test_dossier_livreur(self) -> None:
        """L5 — modifier ses pièces après approbation repasse en `pending`.

        Ce cycle est donc **volontairement cyclique** : `require_acyclic=False`.
        C'est la seule des quatre machines dans ce cas, et la raison est
        métier — un dossier se re-instruit, une commande ne se re-livre pas.
        """
        machine = StateMachine(
            {
                "pending": {"approved", "rejected"},
                "approved": {"pending", "suspended"},
                "rejected": {"pending"},
                "suspended": {"approved"},
            },
            name="dossier livreur",
            require_acyclic=False,
        )
        assert machine.can("approved", "pending")
