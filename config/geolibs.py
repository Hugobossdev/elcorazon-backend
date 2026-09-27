"""Localisation de GDAL et GEOS pour les postes de développement Windows.

GeoDjango s'appuie sur deux bibliothèques **système**, `gdal` et `geos_c`.  Les
images Docker et la CI les installent par le gestionnaire de paquets ; un poste
Windows nu ne les a pas, et il n'existe pas d'installateur léger : la voie
habituelle est OSGeo4W, plusieurs centaines de mégaoctets, avec droits
administrateur.

Sans elles, l'import du moindre modèle échoue — donc `manage.py check`,
`makemigrations` et la suite de tests entière, y compris les tests qui ne
touchent ni à la base ni au géospatial.  Un développeur Windows se retrouve
incapable d'exécuter quoi que ce soit, ce qui est un coût quotidien.

Ce module récupère les deux DLL depuis la roue `pyogrio`, qui les embarque déjà
pour ses propres besoins et s'installe par un simple `pip install`.  Aucun
téléchargement hors PyPI, aucun droit administrateur, rien hors de l'environnement
virtuel.

**Ce module n'est appelé que par `dev.py` et `test.py`.**  En production, GDAL
et GEOS proviennent du système et `settings.GDAL_LIBRARY_PATH` reste absent :
le comportement de Django y est celui, standard, d'une installation normale.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

__all__ = ["discover"]


def discover() -> dict[str, str]:
    """Chemins de `gdal` et `geos_c`, ou dictionnaire vide.

    Le retour alimente directement les réglages :

        globals().update(discover())

    Un dictionnaire vide signifie « laisser Django chercher tout seul », ce qui
    est le comportement correct dès que les bibliothèques sont installées
    normalement — sous Linux notamment, où ce module ne trouvera rien et n'a
    rien à faire.
    """
    if not sys.platform.startswith("win"):
        return {}

    try:
        import pyogrio
    except ImportError:
        # Dépendance de confort, déclarée dans l'extra `dev`.  Son absence n'est
        # pas une erreur : sur un poste où GDAL est installé par ailleurs, tout
        # fonctionne sans elle.
        return {}

    libs = Path(pyogrio.__file__).parent.parent / "pyogrio.libs"
    if not libs.is_dir():
        return {}

    found: dict[str, str] = {}
    for setting, pattern in (
        ("GDAL_LIBRARY_PATH", "gdal-*.dll"),
        ("GEOS_LIBRARY_PATH", "geos_c-*.dll"),
    ):
        matches = sorted(glob.glob(str(libs / pattern)))
        if matches:
            found[setting] = matches[0]

    if len(found) < 2:
        # Une seule des deux ne sert à rien : Django échouerait sur l'autre, avec
        # un message moins clair que celui d'origine.  Mieux vaut ne rien poser.
        return {}

    # `gdal.dll` charge une vingtaine de dépendances voisines (proj, sqlite,
    # libcurl…).  Sous Windows, `ctypes.CDLL` avec un chemin absolu ne les
    # cherche pas dans le dossier du fichier ; sans cette déclaration, le
    # chargement échoue sur une dépendance manquante et non sur GDAL lui-même.
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(libs))

    return found
