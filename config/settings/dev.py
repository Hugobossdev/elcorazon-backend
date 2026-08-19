"""Réglages de développement (docker compose up)."""

from __future__ import annotations

import os

from config.geolibs import discover

from .base import *  # noqa: F403
from .base import INSTALLED_APPS, MIDDLEWARE, REST_FRAMEWORK

# --------------------------------------------------------------- garde-fou
#
# Ce module sur un hébergeur, c'est `DEBUG = True` et `ALLOWED_HOSTS = ["*"]`
# ouverts sur Internet : traces d'erreur, réglages et contenu des requêtes
# livrés à qui sait déclencher une exception. Le cas n'est pas théorique — il
# s'est produit, pour deux raisons qui se cumulent : `config/asgi.py` retombe
# sur `config.settings.dev` quand `DJANGO_SETTINGS_MODULE` ne lui parvient pas,
# et un `.env` de développement recopié dans un tableau de bord y porte cette
# même valeur.
#
# `RENDER` est posée par Render dans tous ses services. Le garde-fou est donc
# muet en local, où il n'y a rien à protéger, et refuse de démarrer là-bas en
# nommant le réglage à corriger — un déploiement rouge vaut mieux qu'un
# déploiement vert servant `DEBUG = True` en public.
#
# Lu dans `os.environ` et non par `config()` : c'est l'environnement réel du
# conteneur qui décide, jamais un fichier `.env` embarqué.
if os.environ.get("RENDER"):
    raise RuntimeError(
        "config.settings.dev est chargé sur Render. Renseignez "
        "DJANGO_SETTINGS_MODULE=config.settings.prod dans les variables "
        "d'environnement du service."
    )

# Sur un poste Windows sans OSGeo4W, désigne les DLL GDAL/GEOS embarquées dans
# l'environnement virtuel.  Ne fait rien ailleurs — voir config/geolibs.py.
globals().update(discover())

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Le schéma et l'interface d'exploration ne sont servis qu'ici.
INSTALLED_APPS = [*INSTALLED_APPS]

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    # Interface DRF navigable, pratique pour explorer l'API à la main.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

CORS_ALLOW_ALL_ORIGINS = True  # le back-office Flutter Web tourne sur un autre port

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

MIDDLEWARE = [*MIDDLEWARE]
