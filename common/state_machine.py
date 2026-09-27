"""Machines à états déclaratives.

Voir ADR-010. Quatre entités ont un cycle de vie contraint — commande,
livraison, paiement, dossier livreur — et l'implémentation précédente a produit
sur elles quatre des douze failles prouvées (C3, C4, C5, P1), plus une course
critique (L2).

La cause n'était pas l'inattention : les transitions étaient écrites en `if`
dispersés dans les contrôleurs, donc chaque nouveau point d'entrée devait
re-vérifier l'ensemble des règles.  Il suffisait d'en oublier une.

Ici les transitions sont **déclarées une fois, en donnée**, et la déclaration
est le seul chemin d'écriture.  Ce module est volontairement dépourvu de toute
dépendance à Django : il est testable sans base de données, et c'est ce qui
permet d'exercer exhaustivement les combinaisons d'états.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["IllegalTransition", "StateMachine"]


class IllegalTransition(Exception):
    """Transition non déclarée.

    Porte les états concernés pour que la couche API produise un message
    exploitable sans avoir à ré-analyser la chaîne.
    """

    def __init__(self, source: str, target: str, allowed: Iterable[str]) -> None:
        self.source = source
        self.target = target
        self.allowed = sorted(allowed)
        allowed_text = ", ".join(self.allowed) if self.allowed else "aucune (état terminal)"
        super().__init__(
            f"Transition refusée : {source} → {target}. "
            f"Transitions autorisées depuis {source} : {allowed_text}."
        )


class StateMachine:
    """Graphe de transitions autorisées, immuable et vérifié à la construction.

    >>> sm = StateMachine({"a": {"b"}, "b": {"c"}, "c": set()})
    >>> sm.validate("a", "b")
    >>> sm.is_terminal("c")
    True
    """

    __slots__ = ("_transitions", "name")

    def __init__(
        self,
        transitions: Mapping[str, Iterable[str]],
        *,
        name: str = "state machine",
        require_acyclic: bool = True,
    ) -> None:
        self.name = name
        self._transitions: dict[str, frozenset[str]] = {
            str(source): frozenset(str(t) for t in targets)
            for source, targets in transitions.items()
        }

        self._assert_states_declared()
        if require_acyclic:
            self._assert_acyclic()

    # ------------------------------------------------------ vérifications

    def _assert_states_declared(self) -> None:
        """Tout état cible doit être un état source déclaré.

        C'est la garde contre C4 : l'implémentation précédente projetait sur la
        commande un statut `accepted` qui n'existait pas dans son énumération,
        ce qui aurait violé la contrainte CHECK en production.  Ici, une cible
        non déclarée fait échouer l'import du module — donc le démarrage, donc
        la CI.
        """
        declared = set(self._transitions)
        referenced = {t for targets in self._transitions.values() for t in targets}
        undeclared = referenced - declared
        if undeclared:
            raise ValueError(
                f"{self.name} : états cibles non déclarés comme sources : "
                f"{', '.join(sorted(undeclared))}. "
                "Tout état atteignable doit figurer dans la table des transitions."
            )

    def _assert_acyclic(self) -> None:
        """Le graphe doit être acyclique.

        C'est la garde contre C3 : rejouer `delivered` réincrémentait les
        compteurs du livreur.  Un graphe acyclique rend le retour arrière
        **inexprimable**, donc les compteurs peuvent être incrémentés à la
        transition sans garde supplémentaire.
        """
        unvisited, in_progress, done = 0, 1, 2
        colour = dict.fromkeys(self._transitions, unvisited)

        def visit(node: str, path: list[str]) -> None:
            colour[node] = in_progress
            for nxt in sorted(self._transitions[node]):
                if colour[nxt] == in_progress:
                    cycle = " → ".join([*path, node, nxt])
                    raise ValueError(
                        f"{self.name} : cycle détecté ({cycle}). "
                        "Un cycle autoriserait un retour arrière de statut."
                    )
                if colour[nxt] == unvisited:
                    visit(nxt, [*path, node])
            colour[node] = done

        for state in sorted(self._transitions):
            if colour[state] == unvisited:
                visit(state, [])

    # ------------------------------------------------------ interrogation

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self._transitions)

    def targets_from(self, source: str) -> frozenset[str]:
        try:
            return self._transitions[str(source)]
        except KeyError:
            raise ValueError(f"{self.name} : état inconnu {source!r}.") from None

    def is_terminal(self, state: str) -> bool:
        return not self.targets_from(state)

    def can(self, source: str, target: str) -> bool:
        return str(target) in self.targets_from(source)

    def is_noop(self, source: str, target: str) -> bool:
        """Transition vers l'état courant.

        Réponse directe à P1 : rejouer un webhook de paiement déjà `completed`
        ne doit ni réécrire, ni réémettre d'événement — mais ne doit pas non
        plus être une erreur, sinon le prestataire retente indéfiniment.
        """
        self.targets_from(source)  # valide que l'état existe
        return str(source) == str(target)

    def validate(self, source: str, target: str) -> None:
        """Lève `IllegalTransition` si la transition n'est pas déclarée."""
        if not self.can(source, target):
            raise IllegalTransition(str(source), str(target), self.targets_from(source))

    # ------------------------------------------------------ analyse

    def reachable_from(self, source: str) -> frozenset[str]:
        """Fermeture transitive — utile aux tests et à la documentation."""
        seen: set[str] = set()
        stack = [str(source)]
        while stack:
            current = stack.pop()
            for nxt in self.targets_from(current):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return frozenset(seen)

    def illegal_pairs(self) -> frozenset[tuple[str, str]]:
        """Tous les couples (source, cible) **non** déclarés.

        Alimente le test de propriété : pour la commande, cela fait 56 couples
        qui doivent être refusés.  Aucun n'aurait été écrit à la main — et
        c'est précisément dans cette majorité que vivaient C3 et C4.
        """
        return frozenset(
            (source, target)
            for source in self._transitions
            for target in self._transitions
            if source != target and target not in self._transitions[source]
        )

    def __repr__(self) -> str:
        return f"<StateMachine {self.name!r}: {len(self._transitions)} états>"
