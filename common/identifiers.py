"""Génération d'identifiants.

Voir ADR-007. On utilise UUIDv7 comme clé primaire de tous les modèles : opaque
comme un UUIDv4, mais préfixé de 48 bits d'horodatage, donc *ordonné*.

L'intérêt est physique. Un UUIDv4 tombe à un emplacement aléatoire de l'index
B-tree à chaque insertion, ce qui fragmente les pages et dégrade le taux de
succès du cache à mesure que la table grossit — un vrai coût sur `orders`,
`delivery_locations` et `analytics_events`. Un UUIDv7 s'insère en fin d'index,
comme une séquence, sans en révéler le volume.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7"]


def _uuid7_fallback() -> uuid.UUID:
    """Implémentation locale d'UUIDv7 (RFC 9562, §5.7).

    Disposition sur 128 bits :
        48 bits  horodatage Unix en millisecondes
         4 bits  version (7)
        12 bits  aléatoire — départage les UUID d'une même milliseconde
         2 bits  variante (RFC 4122)
        62 bits  aléatoire
    """
    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")

    value = (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80  # 48 bits d'horodatage
    value |= 0x7 << 76  # version 7
    value |= ((random_bits >> 62) & 0xFFF) << 64  # 12 bits aléatoires
    value |= 0b10 << 62  # variante RFC 4122
    value |= random_bits & 0x3FFF_FFFF_FFFF_FFFF  # 62 bits aléatoires

    return uuid.UUID(int=value)


# `uuid.uuid7` est natif depuis Python 3.14. Les images Docker sont en 3.13
# (ADR-001 : maturité des liaisons GDAL/GEOS), d'où la bascule.
_implementation = getattr(uuid, "uuid7", _uuid7_fallback)


def uuid7() -> uuid.UUID:
    """UUIDv7 — primitive standard si l'interpréteur la fournit, sinon locale.

    L'indirection n'est pas gratuite, et c'est voulu : `uuid7` doit être une
    fonction **de ce module**, au nom stable.

    Django sérialise un `default=` par son chemin d'import.  Avec un simple
    alias `uuid7 = getattr(uuid, "uuid7", _uuid7_fallback)`, ce chemin devenait
    `uuid.uuid7` sous 3.14 et `common.identifiers._uuid7_fallback` sous 3.13 :
    la migration générée dépendait alors de l'interpréteur qui l'avait produite.
    Chaque poste réclamait une migration que l'autre défaisait, et
    `makemigrations --check` échouait en intégration continue dès que sa version
    de Python différait de celle du développeur.

    Ici le chemin sérialisé est toujours `common.identifiers.uuid7`, quelle que
    soit l'implémentation retenue derrière.
    """
    return _implementation()
