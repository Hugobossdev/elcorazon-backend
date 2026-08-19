"""Réglages de production.

Toute valeur sensible vient de l'environnement, sans valeur par défaut : une
variable manquante doit faire échouer le démarrage, pas produire un service qui
tourne avec une configuration dégradée sans que personne ne le sache.
"""

from __future__ import annotations

from decouple import Csv, config

from .base import *  # noqa: F403

DEBUG = False

# --------------------------------------------------------------- transport

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# La sonde de vivacité échappe à la redirection HTTPS. Un orchestrateur
# interroge le conteneur sur son adresse interne, en clair et sans passer par le
# terminateur TLS : `SECURE_SSL_REDIRECT` lui répondrait 301, que Render comme
# Kubernetes comptent comme un échec. Le service serait alors déclaré mort à
# chaque déploiement, et le déploiement annulé — alors que l'application va bien.
#
# Le motif est comparé à `request.path` privé de sa barre oblique de tête.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
# `Csv()` et non `str.split(",")` : sur une variable absente ou vide, le split
# rend `[""]` — une liste d'une origine vide, pas une liste vide. Django refuse
# alors de démarrer (`4_0.E001` : une origine doit porter un schéma), et
# django-cors-headers fait de même sur son propre réglage (`corsheaders.E013`).
# Le déploiement échouait donc au démarrage tant que le back-office n'avait pas
# d'adresse à déclarer. `Csv()` écarte les segments vides et rend `[]`, ce qui
# n'ouvre rien : les deux listes sont des autorisations, pas des filtres.
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

X_FRAME_OPTIONS = "DENY"

# --------------------------------------------------------------- statiques
#
# Sous Docker Compose, Nginx sert `staticfiles/` et Django ne voit jamais ces
# requêtes. Sur un hébergeur où nous ne posons pas notre propre reverse proxy,
# personne ne les sert. WhiteNoise remet ce travail dans le processus.
#
# Inséré juste après SecurityMiddleware, comme sa documentation l'exige : placé
# avant, la redirection HTTPS ne s'appliquerait pas aux fichiers statiques.
MIDDLEWARE = [
    MIDDLEWARE[0],  # noqa: F405
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],  # noqa: F405
]

# `CompressedStaticFilesStorage` et non la variante `Manifest` : celle-ci exige
# que toute référence croisée entre fichiers collectes se resolve, et une seule
# URL cassée dans le CSS d'une dependance fait échouer `collectstatic`, donc le
# déploiement entier. Le hachage des noms viendra quand la chaîne sera stable.
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# --------------------------------------------------------------- CORS

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())

# --------------------------------------------------------------- messagerie

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")

# --------------------------------------------------------------- garde-fous

# Ces réglages n'ont pas de valeur par défaut acceptable en production.  Les
# lire ici, au chargement, transforme un oubli de configuration en échec de
# démarrage immédiat plutôt qu'en incident de sécurité découvert plus tard.
for _required in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD"):
    if not config(_required, default=""):
        raise RuntimeError(
            f"{_required} est absente de l'environnement. "
            "La production ne démarre pas sans configuration complète."
        )

# Les clés JWT se contrôlent sur leur valeur **résolue**, et non sur la variable
# en clair : `base.py` les accepte sous deux formes — un fichier monté
# (`JWT_PRIVATE_KEY_PATH`, la voie du compose et de Kubernetes) ou la variable
# elle-même (`JWT_SIGNING_KEY`, la voie des hébergeurs sans volume). N'exiger
# que la seconde refusait de démarrer un déploiement dont les clés étaient bien
# là, sous l'autre forme — c'était le cas de `docker-compose.prod.yml`, qui ne
# renseigne que les chemins.
for _inline, _path, _key in (
    ("JWT_SIGNING_KEY", "JWT_PRIVATE_KEY_PATH", SIMPLE_JWT["SIGNING_KEY"]),  # noqa: F405
    ("JWT_VERIFYING_KEY", "JWT_PUBLIC_KEY_PATH", SIMPLE_JWT["VERIFYING_KEY"]),  # noqa: F405
):
    if not _key:
        raise RuntimeError(
            f"Aucune clé JWT trouvée : {_inline} est absente de l'environnement "
            f"et {_path} ne désigne aucun fichier lisible. "
            "La production ne démarre pas sans configuration complète."
        )
