"""Graphe de dépendances entre applications — ADR-002.

L'ADR annonce un graphe « vérifié en CI ». Il ne l'était pas : la règle vivait
dans un document et dans l'attention de qui relisait. Ces tests la rendent
exécutable, ce qui change sa nature — une arête interdite ne se discute plus en
revue, elle fait échouer la construction.

Le test le plus utile est `test_le_graphe_est_acyclique` : c'est le cycle, et
non l'arête isolée, qui reconstitue le monolithe enchevêtré sous une
arborescence propre.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.graph import (
    APPS_ROOT,
    Edge,
    app_of,
    imported_apps,
    iter_app_modules,
)

pytestmark = pytest.mark.architecture

#: Dépendances **directes** autorisées, app par app.
#:
#: Transcription du graphe de `02-architecture-generale.md` §7, augmentée de
#: deux règles qui n'y figurent pas comme flèches :
#:
#: * `accounts` est le socle d'identité : tout le monde peut en dépendre, il ne
#:   dépend de personne. L'y faire figurer flèche par flèche n'apprendrait rien.
#: * `notifications` dépend de `orders` et `delivery`, et **c'est le bon sens** :
#:   l'abonné connaît l'émetteur, jamais l'inverse. C'est ce qui permet à un
#:   signal de traverser sans créer de cycle.
ALLOWED: dict[str, set[str]] = {
    "accounts": set(),
    "geography": {"accounts"},
    "restaurants": {"accounts", "geography"},
    "profiles": {"accounts", "geography"},
    "catalog": {"accounts", "restaurants"},
    "carts": {"accounts", "catalog", "restaurants"},
    "promotions": {"accounts", "restaurants"},
    "orders": {
        "accounts",
        "carts",
        "catalog",
        "geography",
        "profiles",
        "promotions",
        "restaurants",
    },
    # Le panier collaboratif se confirme **en** commande : il dépend donc de
    # `orders`, et jamais l'inverse. Il dépend aussi de `carts`, dont il réutilise
    # la valorisation (`price_selection`) plutôt que d'en écrire une seconde — un
    # panier de groupe dont les prix seraient calculés ailleurs finirait par ne
    # plus dire la même chose que le panier personnel.
    "groupcarts": {"accounts", "carts", "catalog", "orders", "profiles", "restaurants"},
    # `delivery` s'y ajoute pour les retraits : les gains d'un livreur sont sur
    # son dossier, et c'est le module qui manie l'argent qui les débite. La
    # flèche va bien dans ce sens — `delivery` ne connaît pas `payments`, sans
    # quoi le cycle serait immédiat.
    "payments": {"accounts", "delivery", "orders", "restaurants"},
    "delivery": {"accounts", "geography", "orders", "restaurants"},
    "tracking": {"accounts", "delivery", "orders", "restaurants"},
    # La signalisation d'appel lit la course pour savoir qui appeler, et la
    # commande pour savoir de quoi il s'agit. Elle ne dépend pas de `tracking` :
    # les deux modules parlent de la même livraison, mais l'un relaie des
    # positions et l'autre fait sonner un téléphone — les coupler ferait
    # dépendre la sonnerie d'un flux de suivi qui peut être coupé.
    "calls": {"accounts", "delivery", "orders"},
    "notifications": {"accounts", "delivery", "orders"},
    # Comme `notifications` : l'abonné connaît l'émetteur, jamais l'inverse
    # (voir `test_orders_ne_connait_aucun_de_ses_abonnes`). `loyalty` réagit à
    # la livraison par signal et frappe ses codes via `promotions`. `payments`
    # s'y ajoute pour les abonnements (P4) : le règlement, initial ou de
    # renouvellement, suit le chemin normal d'un encaissement — jamais un
    # second chemin qui le dupliquerait.
    "loyalty": {"accounts", "orders", "promotions", "restaurants", "payments"},
    # Comme `loyalty` : réagit à la livraison par signal, et lit (sans y
    # écrire) le solde de fidélité pour ses badges.
    "gamification": {"accounts", "orders", "loyalty"},
    # Un partage de commande (S3) désigne une commande existante ; le reste du
    # domaine (groupes, publications, likes) ne dépend de personne d'autre.
    "social": {"accounts", "orders"},
    # Une réclamation ou une demande de retour désigne une commande existante ;
    # aucune écriture dans l'autre sens.
    "support": {"accounts", "orders"},
    # Écoute `orders` par signal, comme `loyalty` et `gamification` ; ses
    # rapports agrègent directement les commandes, leurs lignes et les
    # courses — la source de vérité, plutôt qu'une table dupliquée.
    #
    # `loyalty` et `profiles` s'y ajoutent pour la fiche client, qui croise
    # commandes, points et adresses ; `catalog` pour les ventes par catégorie et
    # l'état de la carte. Ces agrégats ne pouvaient pas vivre dans `accounts`,
    # qui ne dépend de personne : le socle d'identité y serait devenu le module
    # qui connaît tout le reste. La lecture est à sens unique — aucun de ces
    # modules ne connaît `analytics`, ce que garantit l'acyclicité du graphe.
    "analytics": {"accounts", "catalog", "delivery", "loyalty", "orders", "profiles"},
    # Le champ de recherche du back-office traverse quatre domaines et n'écrit
    # nulle part. C'est ce qui rend l'arête acceptable : toutes les flèches
    # entrent, aucune ne sort, et le graphe reste acyclique. L'alternative —
    # une recherche par domaine agrégée dans le client — a été écartée parce
    # que c'est exactement ce que faisait l'implémentation précédente, sans
    # jamais appliquer les permissions ni le cloisonnement.
    "search": {"accounts", "catalog", "delivery", "orders", "restaurants"},
}


def observed_edges() -> list[Edge]:
    return [
        Edge(source=app_of(module), target=target, module=module)
        for module in iter_app_modules()
        for target in sorted(imported_apps(module))
    ]


class TestGrapheDeclare:
    def test_toutes_les_apps_installees_sont_declarees(self) -> None:
        """Une app ajoutée sans ligne dans `ALLOWED` fait échouer ce test.

        C'est voulu : déclarer ses dépendances est le geste qui manque quand on
        crée une app à la hâte, et c'est précisément à ce moment-là que le
        graphe se dégrade.
        """
        sur_disque = {
            path.name
            for path in APPS_ROOT.iterdir()
            if path.is_dir() and (path / "__init__.py").exists()
        }

        assert sur_disque == set(ALLOWED), (
            "Applications non déclarées dans ALLOWED : "
            f"{sorted(sur_disque - set(ALLOWED))} ; "
            f"déclarées mais absentes : {sorted(set(ALLOWED) - sur_disque)}"
        )

    def test_aucune_dependance_hors_du_graphe(self) -> None:
        interdites = [edge for edge in observed_edges() if edge.target not in ALLOWED[edge.source]]

        assert not interdites, "Dépendances non déclarées :\n" + "\n".join(
            f"  {edge}" for edge in interdites
        )

    def test_le_graphe_est_acyclique(self) -> None:
        """C'est le cycle, et non l'arête isolée, qui fait le monolithe.

        Deux apps qui s'importent mutuellement ne peuvent plus être extraites,
        ni testées séparément, ni raisonnées l'une sans l'autre — quel que soit
        le soin apporté à leurs noms de dossiers.
        """
        graphe: dict[str, set[str]] = {app: set() for app in ALLOWED}
        for edge in observed_edges():
            graphe[edge.source].add(edge.target)

        unvisited, in_progress, done = 0, 1, 2
        colour = dict.fromkeys(graphe, unvisited)

        def visit(node: str, path: list[str]) -> None:
            colour[node] = in_progress
            for nxt in sorted(graphe[node]):
                if colour[nxt] == in_progress:
                    raise AssertionError("Cycle : " + " → ".join([*path, node, nxt]))
                if colour[nxt] == unvisited:
                    visit(nxt, [*path, node])
            colour[node] = done

        for app in sorted(graphe):
            if colour[app] == unvisited:
                visit(app, [])


class TestArêtesNommémentInterdites:
    """Les inversions que les ADR citent explicitement.

    Redondantes avec `test_aucune_dependance_hors_du_graphe`, et gardées quand
    même : leur nom dit *pourquoi* elles sont interdites, ce qu'une liste de
    dépendances autorisées ne raconte pas.
    """

    def test_catalog_n_interroge_jamais_les_commandes(self) -> None:
        """C'est cette interdiction qui a produit `VerifiedPurchase` : le
        catalogue ne peut pas demander aux commandes qui a acheté quoi, donc
        `orders` l'en informe (S1)."""
        assert "orders" not in ALLOWED["catalog"]
        assert not [
            edge
            for edge in observed_edges()
            if edge.source == "catalog" and edge.target == "orders"
        ]

    def test_orders_ne_connait_aucun_de_ses_abonnes(self) -> None:
        """`notifications`, et demain `loyalty`, `gamification`, `analytics`,
        réagissent aux commandes par signal. Un appel direct retournerait la
        flèche et, à la quatrième app, referait le monolithe."""
        abonnes = {"notifications", "loyalty", "gamification", "analytics"}

        assert not ALLOWED["orders"] & abonnes
        assert not [
            edge for edge in observed_edges() if edge.source == "orders" and edge.target in abonnes
        ]

    def test_accounts_ne_depend_d_aucune_app(self) -> None:
        """Le socle d'identité doit rester extractible seul : lui faire
        connaître les restaurants ou les commandes le rendrait dépendant de
        tout ce qu'il authentifie."""
        assert ALLOWED["accounts"] == set()
        assert not [edge for edge in observed_edges() if edge.source == "accounts"]


class TestSocleCommun:
    def test_common_ne_depend_que_de_l_identite(self) -> None:
        """`common` est décrit comme « sans dépendance aux apps métier ». Il en
        a une, assumée : les permissions et les consommateurs ont besoin du
        type de compte. La borner ici empêche que la liste s'allonge sans
        décision — le jour où `common` importera `orders`, il ne sera plus un
        socle mais une app de plus.
        """
        common = APPS_ROOT.parent / "common"
        importees: set[str] = set()

        for module in sorted(common.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[0] == "apps":
                        importees.add(parts[1])

        assert importees <= {"accounts"}, (
            f"`common` importe aussi : {sorted(importees - {'accounts'})}"
        )

    def test_aucune_app_ne_contourne_common_pour_les_montants(self) -> None:
        """ADR-007 — un montant se persiste par `MoneyField`, jamais par un
        `DecimalField` nu qui perdrait la devise."""
        coupables: list[Path] = []

        for module in iter_app_modules():
            if module.name != "models.py":
                continue
            source = module.read_text(encoding="utf-8")
            if "DecimalField" in source and "price" in source.lower():
                # `rating_average` est un DecimalField légitime : c'est une
                # note, pas un montant. On ne signale que ce qui touche au prix.
                for line in source.splitlines():
                    if "DecimalField" in line and any(
                        mot in line.lower() for mot in ("price", "amount", "fee", "total")
                    ):
                        coupables.append(module)

        assert not coupables, f"Montants persistés hors de MoneyField : {coupables}"
