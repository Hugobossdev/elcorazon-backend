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

# `base.py` lit `DJANGO_ALLOWED_HOSTS` dans l'environnement, que `render.yaml`
# renseigne depuis l'hôte du service. Ce repli couvre le service créé à la main
# depuis le tableau de bord : celui-ci ignore le blueprint, la variable est donc
# absente et Django répond 400 à toute requête, sonde comprise. Un repli et non
# une valeur en dur : là où la variable existe, elle reste la source unique.
ALLOWED_HOSTS = ALLOWED_HOSTS or [".onrender.com"]  # noqa: F405

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
#
# Le même repli que pour `ALLOWED_HOSTS`, pour la même raison : sans origine de
# confiance, la connexion à `/admin/` échoue dès son POST.
CSRF_TRUSTED_ORIGINS = CSRF_TRUSTED_ORIGINS or ["https://*.onrender.com"]

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
for _required in ("DJANGO_SECRET_KEY", "JWT_SIGNING_KEY", "JWT_VERIFYING_KEY", "POSTGRES_PASSWORD"):
    if not config(_required, default=""):
        raise RuntimeError(
            f"{_required} est absente de l'environnement. "
            "La production ne démarre pas sans configuration complète."
        )
