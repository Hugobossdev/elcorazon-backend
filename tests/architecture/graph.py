"""Lecture du graphe d'imports réel — outillage des tests d'architecture.

Les règles d'architecture ne valent que si quelque chose les applique. L'ADR-002
annonce un graphe de dépendances « vérifié en CI » ; jusqu'ici il ne l'était
pas, et rien n'aurait signalé la première arête interdite — sinon une relecture
attentive, c'est-à-dire une vigilance, c'est-à-dire ce que ce projet cherche
partout ailleurs à remplacer par une contrainte.

L'analyse est **statique** et porte sur le texte des modules : elle voit un
import même s'il est placé dans une fonction, et ne dépend pas de l'ordre de
chargement des applications. Un import dynamique par `import_string` lui
échappe — c'est la limite connue, et c'est aussi pourquoi les chemins
d'`import_string` du projet pointent tous vers des ports, jamais vers du métier.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

__all__ = ["APPS_ROOT", "Edge", "app_of", "imported_apps", "iter_app_modules"]

APPS_ROOT = Path(__file__).resolve().parents[2] / "apps"


@dataclass(frozen=True, slots=True)
class Edge:
    """Une dépendance constatée, avec le fichier qui la porte."""

    source: str
    target: str
    module: Path

    def __str__(self) -> str:
        return f"{self.source} → {self.target} ({self.module.name})"


def iter_app_modules() -> list[Path]:
    """Tous les modules Python des applications, migrations exclues.

    Les migrations sont écartées : elles importent librement d'autres apps par
    dépendance de schéma, ce qui est le fonctionnement normal de Django et non
    une dépendance de code.
    """
    return [
        path
        for path in sorted(APPS_ROOT.rglob("*.py"))
        if "migrations" not in path.parts and "__pycache__" not in path.parts
    ]


def app_of(module: Path) -> str:
    """Nom de l'application à laquelle ce module appartient."""
    return module.relative_to(APPS_ROOT).parts[0]


def imported_apps(module: Path) -> set[str]:
    """Applications importées par ce module, quelle que soit la forme.

    `import apps.orders.models` et `from apps.orders.models import Order` sont
    la même dépendance ; les distinguer laisserait passer la moitié des cas.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found |= _app_in(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found |= _app_in(alias.name)

    return found - {app_of(module)}


def _app_in(dotted: str) -> set[str]:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "apps":
        return {parts[1]}
    return set()
