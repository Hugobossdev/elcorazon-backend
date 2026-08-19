# syntax=docker/dockerfile:1.7
# Image unique pour l'API, les workers et beat (ADR-001).
#
# Une seule image pour trois rôles : c'est ce qui garantit qu'un worker exécute
# exactement le code que l'API a validé. Le rôle est choisi par la commande.

# ---------------------------------------------------------------- base
FROM python:3.13-slim AS base

# `slim` et non `alpine` : GeoDjango s'appuie sur GDAL et GEOS, dont les
# binaires sont compilés contre glibc. Sur musl, il faudrait les recompiler.
#
# `PIP_RETRIES` et `PIP_TIMEOUT` : la construction échouait sur des
# `ReadTimeoutError` de PyPI. Les valeurs par défaut de pip — 5 tentatives mais
# **15 secondes** de délai — sont taillées pour une liaison courte ; sur une
# connexion lente ou saturée, un paquet volumineux (`Pillow`, `psycopg[binary]`,
# les roues GDAL) dépasse ce seuil sans que rien ne soit en panne. Le délai
# passe donc à 60 secondes, et le nombre de tentatives à 8 : ces deux réglages
# ne se remplacent pas, ils traitent deux pannes différentes — un délai plus
# long absorbe la lenteur, des tentatives supplémentaires absorbent la coupure.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=8 \
    PIP_TIMEOUT=60

# `gdal-bin` plutôt qu'un `libgdalNN` explicite : le numéro de SONAME change à
# chaque version de Debian, et l'épingler casse la construction au premier
# changement d'image de base. Le méta-paquet tire la bonne version de libgdal
# et de libproj.
RUN apt-get update && apt-get install --no-install-recommends -y \
        gdal-bin \
        libgeos-c1v5 \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------- dépendances
FROM base AS deps

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# Les dépendances sont installées avant le code : une modification de source
# n'invalide pas ce calque, ce qui rend les reconstructions quasi instantanées.
#
# `pyproject.toml` reste la source de vérité unique des dépendances. Les
# répertoires de paquets sont créés vides le temps de l'installation éditable,
# car setuptools exige leur existence — le code réel est copié au calque
# suivant et les remplace.
# Le cache pip est monté par BuildKit plutôt qu'écrit dans un calque : une
# reconstruction après un `ReadTimeoutError` repart des roues déjà téléchargées
# au lieu de tout retirer de PyPI, et l'image finale n'en porte pas la trace —
# c'est ce que `PIP_NO_CACHE_DIR=1` cherchait à obtenir, sans le prix.
# `PIP_NO_CACHE_DIR=0` le réactive pour ce seul `RUN`, le temps du montage.
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p config common apps \
    && touch config/__init__.py common/__init__.py apps/__init__.py \
    && python -m venv /opt/venv \
    && PIP_NO_CACHE_DIR=0 /opt/venv/bin/pip install --upgrade pip \
    && PIP_NO_CACHE_DIR=0 /opt/venv/bin/pip install -e . "uvicorn[standard]"

# ---------------------------------------------------------------- exécution
FROM base AS runtime

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Utilisateur non privilégié : un conteneur compromis ne doit pas être root.
RUN useradd --create-home --uid 10001 corazon
COPY --chown=corazon:corazon . /app

# `/app` appartient à root : il est créé par le `WORKDIR` de l'étage de base,
# avant même que l'utilisateur existe, et le `--chown` du `COPY` ne s'applique
# qu'aux fichiers copiés, pas au répertoire qui les porte. Or `staticfiles/` est
# dans `.dockerignore` — il n'arrive donc jamais par la copie — et
# `collectstatic` le crée au démarrage, sous l'identité de `corazon` :
# `PermissionError` au premier déploiement où les migrations sont enfin passées.
#
# Seul ce répertoire change de propriétaire, et non `/app` tout entier : le
# processus n'a aucune raison de pouvoir réécrire son propre code.
RUN mkdir -p /app/staticfiles && chown corazon:corazon /app/staticfiles

USER corazon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health/" || exit 1

# Rôle par défaut : l'API. Les autres sont choisis par `command:` dans compose
# ou par l'`args` du déploiement Kubernetes.
#
# Un script plutôt qu'un appel direct à uvicorn : migrations et fichiers
# statiques doivent précéder le service, et cette préparation doit voyager avec
# l'image. Confiée au `dockerCommand` du blueprint Render, elle ne s'appliquait
# qu'aux services créés par ce blueprint — un service créé à la main depuis le
# tableau de bord démarrait sur une base sans tables. Voir `deploy/start-api.sh`.
#
# `sh <script>` et non `./script` : le bit d'exécution ne survit pas toujours à
# un dépôt cloné depuis Windows, où il n'existe pas.
CMD ["sh", "/app/deploy/start-api.sh"]

# ---------------------------------------------------------------- développement
# Étage supplémentaire portant l'outillage de test et de qualité. C'est la cible
# utilisée par docker compose en local.
#
# `runtime` reste la cible livrée : pytest, ruff et mypy n'ont rien à faire dans
# une image de production — ils l'alourdissent et élargissent inutilement sa
# surface d'attaque.
FROM runtime AS dev

USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_NO_CACHE_DIR=0 /opt/venv/bin/pip install -e ".[dev]"
USER corazon

# ---------------------------------------------------------------- cible livrée
# `dev` dérive de `runtime` : il ne peut pas être déclaré avant lui. Or BuildKit
# construit le **dernier** étage quand aucune cible n'est donnée, et le blueprint
# Render n'offre aucun champ pour en choisir une — un build sans `--target` y
# livrerait donc l'image de développement, pytest, ruff et mypy compris.
#
# Cet alias remet la cible de production en dernière position. Il n'ajoute aucun
# calque : CMD, HEALTHCHECK, EXPOSE et l'utilisateur non privilégié viennent tels
# quels de `runtime`. Compose, lui, continue de nommer ses cibles explicitement.
FROM runtime AS production
