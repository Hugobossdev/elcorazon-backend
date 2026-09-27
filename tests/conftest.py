"""Configuration commune des tests.

La suite s'exécute contre PostgreSQL/PostGIS réel, dans l'image Docker — les
contraintes `CHECK` et les index GiST ne se vérifient pas autrement.

Le socle (`common/`) fait exception : il est dépourvu de dépendance à Django et
ses tests tournent dans un simple virtualenv, ce qui les rend utilisables comme
boucle de retour rapide.
"""

from __future__ import annotations

import pytest

# Les fixtures du domaine sont partagées par toutes les suites.
from tests.fixtures import *  # noqa: F403


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Signale les tests ignorés faute de services, plutôt que de les taire.

    Un `-m "not postgis"` silencieux donne une suite verte trompeuse. Le
    décompte est affiché en fin de session pour qu'on sache toujours ce qui n'a
    *pas* été vérifié.
    """
    selected = config.getoption("-m", default="")
    if "not postgis" in selected or "not redis" in selected:
        config.stash.setdefault(_SKIPPED_NOTE, []).append(f"filtre actif : -m {selected!r}")


_SKIPPED_NOTE = pytest.StashKey[list[str]]()


def pytest_terminal_summary(config: pytest.Config) -> None:
    notes = config.stash.get(_SKIPPED_NOTE, [])
    if notes:
        print(
            "\n⚠️  Suite partielle — le géospatial et/ou le temps réel n'ont pas "
            "été vérifiés ici. La suite complète tourne en CI (PostGIS + Redis).\n"
            + "\n".join(f"   {n}" for n in notes)
        )
