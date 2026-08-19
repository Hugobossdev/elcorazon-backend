"""Réglages de test.

La base de test est **PostgreSQL/PostGIS**, servi par `docker compose up db`
en local et par un service GitHub Actions en CI. Il n'y a pas de repli SQLite :
le schéma emploie des types propres à PostgreSQL (`ArrayField`, `geography`,
contraintes d'exclusion), et une base de test d'un autre moteur ne pourrait pas
les porter. Un vert obtenu sur un schéma dégradé ne prouverait rien.

    docker compose up -d db redis
    pytest

Les services externes (cache, channel layer, files, stockage objet) sont
remplacés par des équivalents en mémoire : ils n'apportent rien à la
vérification du métier et rendraient la suite lente et instable.
"""

from __future__ import annotations

import os
from typing import cast

# Valeurs par défaut posées avant l'import de `base`, qui lit l'environnement.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("DJANGO_DEBUG", "False")
os.environ.setdefault("GIS_ENABLED", "True")
# `docker compose` expose PostgreSQL sur 5433 pour ne pas heurter une instance
# déjà installée sur le poste. La CI, elle, utilise 5432.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_DB", "elcorazon")
os.environ.setdefault("POSTGRES_USER", "elcorazon")
os.environ.setdefault("POSTGRES_PASSWORD", "elcorazon")

from config.geolibs import discover

from .base import *  # noqa: F403
from .base import BASE_DIR, DATABASES, REST_FRAMEWORK

# Idem `dev.py` : sans cela, aucun test — pas même ceux qui n'ouvrent aucune
# connexion — n'est collectable sur un poste Windows, l'import des modèles
# échouant sur GDAL.
globals().update(discover())

TESTING = True

# --------------------------------------------------------------- connexions
#
# `CONN_MAX_AGE` vaut 60 s en production, et c'est le bon réglage là-bas : une
# connexion PostgreSQL coûte cher à ouvrir, la garder entre deux requêtes évite
# ce coût. En test, le même réglage **casse la destruction de la base**.
#
# La chaîne exacte : les tests WebSocket exécutent leurs requêtes via
# `channels.db.database_sync_to_async`, qui les délègue au greffon de fils
# d'asgiref. Chacun de ces fils ouvre sa propre connexion — elles sont
# thread-local. `database_sync_to_async` appelle bien `close_old_connections()`
# en sortie, mais celui-ci ne ferme *que* les connexions périmées : avec
# `CONN_MAX_AGE = 60`, une connexion de trois secondes est jugée encore bonne et
# reste ouverte. Les fils d'asgiref, eux, survivent à la session pytest.
#
# `destroy_test_db` trouve alors ces sessions résiduelles et échoue sur
# `database "test_elcorazon" is being accessed by other users`, ce qui laisse la
# base de test en place — l'exécution suivante repart d'un schéma déjà migré et
# de données rémanentes, jusqu'à ce que quelqu'un la supprime à la main.
#
# À zéro, `close_old_connections()` ferme systématiquement, les fils d'asgiref
# ne retiennent plus rien, et la suite est réexécutable indéfiniment. La
# persistance des connexions n'apporte de toute façon rien ici : la suite
# tourne contre une base locale.
DATABASES = {**DATABASES, "default": {**DATABASES["default"], "CONN_MAX_AGE": 0}}

# `AllowedHostsOriginValidator` protège les WebSocket : sans hôte autorisé, il
# ferme **toute** connexion, y compris celle d'un test. Le défaut est le bon —
# une configuration oubliée refuse au lieu d'ouvrir — mais la suite doit
# déclarer son hôte comme le ferait un déploiement.
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# --------------------------------------------------------------- sans service externe

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True  # une tâche qui échoue fait échouer le test

# Aucun octet ne sort vers un serveur S3 pendant la suite. Chaque alias
# fonctionnel est substitué par un stockage en mémoire : les modèles gardent
# leurs stockages nommés (`storages["documents"]`, …), et la substitution se
# fait ici, sans que ni les champs ni les migrations n'aient à connaître les
# tests.
_MEMOIRE = {"BACKEND": "django.core.files.storage.InMemoryStorage"}
STORAGES = {
    "default": _MEMOIRE,
    "products": _MEMOIRE,
    "banners": _MEMOIRE,
    "users": _MEMOIRE,
    "documents": _MEMOIRE,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# --------------------------------------------------------------- rapidité

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# La limitation de débit est désactivée par défaut : sinon le 6ᵉ test qui
# s'authentifie reçoit un 429.  Les tests qui vérifient *le limiteur lui-même*
# la réactivent explicitement (T1).
#
# Neutraliser ne veut pas dire vider : sur un scope absent du dictionnaire, DRF
# ne « laisse pas passer », il lève `ImproperlyConfigured` à l'instanciation du
# limiteur — donc à la première requête, et sur toutes les routes concernées.
# C'est bien la clé présente et valant `None` qui désactive (`allow_request`
# rend la main immédiatement quand `rate is None`).
#
# Les scopes sont dérivés de `base` plutôt que réécrits : un scope ajouté
# là-bas est neutralisé ici sans intervention, et ne peut donc pas faire
# tomber la suite entière longtemps après coup.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    # `REST_FRAMEWORK` est un `dict[str, object]` : le cast rend au sous-
    # dictionnaire son type le temps d'en reprendre les clés.
    "DEFAULT_THROTTLE_RATES": dict.fromkeys(
        cast("dict[str, str]", REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]), None
    ),
}

# --------------------------------------------------------------- JWT

# Paire RSA de test, régénérée à chaque session : aucune clé privée en dépôt,
# même de test — c'est la seule façon d'être sûr qu'aucune ne fuite en
# production par copier-coller.
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    _key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    SIMPLE_JWT = {
        **globals()["SIMPLE_JWT"],
        "SIGNING_KEY": _key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        "VERIFYING_KEY": _key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    }
except ImportError:  # pragma: no cover - cryptography absent : tests purs seulement
    pass

LOGGING = {"version": 1, "disable_existing_loggers": False, "root": {"handlers": []}}

MEDIA_ROOT = BASE_DIR / ".pytest-media"

# Identifiants Agora factices : la suite vérifie qu'un jeton est délivré et à
# qui, pas qu'Agora l'accepte — le format est verrouillé à part
# (`tests/common/test_agora.py`). Sans ces valeurs, la fabrication échouerait
# et masquerait les règles d'autorisation qu'on veut réellement tester.
AGORA_APP_ID = "a" * 32
AGORA_APP_CERTIFICATE = "b" * 32
