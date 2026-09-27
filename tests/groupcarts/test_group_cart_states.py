"""Cycle de vie du panier collaboratif — ADR-010.

Sans base de données : la machine à états est un objet pur, ce qui permet
d'exercer **toutes** les combinaisons plutôt que les trois qu'on aurait écrites à
la main. C'est dans la majorité non écrite que vivaient C3 et C4.
"""

from __future__ import annotations

import pytest

from apps.groupcarts.states import GROUP_CART_MACHINE, GroupCartStatus
from common.state_machine import IllegalTransition

pytestmark = pytest.mark.architecture


class TestGrapheDuPanierCollaboratif:
    def test_toutes_les_transitions_non_declarees_sont_refusees(self) -> None:
        """Douze couples, dont aucun n'aurait été écrit à la main."""
        interdits = GROUP_CART_MACHINE.illegal_pairs()

        assert interdits, "La machine n'aurait aucun couple interdit : elle ne protège rien."
        for source, target in interdits:
            with pytest.raises(IllegalTransition):
                GROUP_CART_MACHINE.validate(source, target)

    def test_un_panier_confirme_ne_se_rouvre_pas(self) -> None:
        """La confirmation a créé une commande, décompté le stock et engagé un
        code promotionnel. Rouvrir le panier laisserait ajouter des plats à une
        commande déjà partie en cuisine — et facturée."""
        assert GROUP_CART_MACHINE.is_terminal(GroupCartStatus.CONFIRMED)

    def test_un_panier_clos_ne_se_rouvre_pas(self) -> None:
        """`locked → open` est le retour arrière tentant, et c'est celui qui
        rendrait la confirmation non déterministe : l'hôte relit un total,
        quelqu'un ajoute un plat, l'hôte paie autre chose que ce qu'il a lu."""
        assert GroupCartStatus.OPEN not in GROUP_CART_MACHINE.targets_from(GroupCartStatus.LOCKED)

    def test_l_echeance_reste_possible_apres_la_cloture(self) -> None:
        """Un hôte qui clôt puis ne confirme jamais ne doit pas laisser le panier
        en attente indéfiniment."""
        assert GROUP_CART_MACHINE.can(GroupCartStatus.LOCKED, GroupCartStatus.EXPIRED)

    def test_l_echeance_ne_frappe_plus_un_panier_confirme(self) -> None:
        """Sinon la tâche planifiée afficherait « échéance dépassée » sur un repas
        déjà en cuisine."""
        assert not GROUP_CART_MACHINE.can(GroupCartStatus.CONFIRMED, GroupCartStatus.EXPIRED)

    def test_la_confirmation_n_est_atteignable_que_par_la_cloture(self) -> None:
        """`open → confirmed` n'existe pas comme arête : la clôture est le moment
        où le total cesse de bouger, et la sauter reviendrait à valoriser un
        panier qui accepte encore des lignes."""
        assert not GROUP_CART_MACHINE.can(GroupCartStatus.OPEN, GroupCartStatus.CONFIRMED)
        assert GROUP_CART_MACHINE.can(GroupCartStatus.OPEN, GroupCartStatus.LOCKED)
        assert GROUP_CART_MACHINE.can(GroupCartStatus.LOCKED, GroupCartStatus.CONFIRMED)

    def test_les_etats_du_modele_et_de_la_machine_coincident(self) -> None:
        """Divergence exacte de C4 : un statut écrit par le code mais absent de
        l'énumération, donc refusé par la contrainte `CHECK` en production."""
        assert GROUP_CART_MACHINE.states == frozenset(GroupCartStatus.values)
