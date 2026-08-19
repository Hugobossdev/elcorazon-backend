"""Réglages communs à tous les environnements.

Aucune valeur secrète ni spécifique à un environnement ici : tout ce qui varie
est lu depuis l'environnement, et `base.py` ne doit jamais être importé
directement — voir `dev.py`, `prod.py`, `test.py`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from corsheaders.defaults import default_headers
from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------- sécurité

SECRET_KEY: str = config("DJANGO_SECRET_KEY")
DEBUG: bool = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS: list[str] = config("DJANGO_ALLOWED_HOSTS", default="", cast=Csv())

# --------------------------------------------------------------- applications

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "channels",
]

# Applications métier — voir ADR-002.  L'ordre suit le graphe de dépendances :
# une app ne dépend que de celles qui la précèdent.
LOCAL_APPS: list[str] = [
    # Chemin critique — construit en premier
    "apps.accounts",
    "apps.geography",
    "apps.restaurants",
    "apps.profiles",
    "apps.catalog",
    "apps.carts",
    "apps.orders",
    # Après `orders` : le panier collaboratif se confirme *en* commande, donc il
    # en dépend — et jamais l'inverse.
    "apps.groupcarts",
    "apps.payments",
    "apps.delivery",
    "apps.tracking",
    "apps.calls",
    "apps.notifications",
    "apps.promotions",
    "apps.loyalty",
    "apps.gamification",
    "apps.social",
    "apps.support",
    "apps.analytics",
    # Lit quatre domaines et n'écrit nulle part — déclarée en dernier,
    # comme le sont les modules qui n'ont aucun dépendant.
    "apps.search",
    #
    # Second temps
    # "apps.inventory",
]

# `common` est déclaré comme application — non pour ses modèles, qui sont tous
# abstraits, mais parce que Django ne découvre les commandes de gestion que
# dans des applications installées. C'est ce qui rend `ensure_storage_buckets`
# appelable.
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + ["common"] + LOCAL_APPS

# --------------------------------------------------------------- géospatial

# GeoDjango exige GDAL et GEOS, qui sont des bibliothèques *système*.  Elles
# sont présentes dans l'image Docker et en CI, absentes d'un poste Windows nu.
# Ce drapeau permet à un développeur sans Docker d'exécuter le sous-ensemble
# non géospatial de la suite (voir Phase 8), sans que la production ait le
# moindre chemin de code différent.
GIS_ENABLED: bool = config("GIS_ENABLED", default=True, cast=bool)

# --- Agora RTC --------------------------------------------------------------
#
# Le certificat signe les jetons d'appel et ne quitte jamais le serveur. Il
# vivait auparavant dans le `.env` des apps Flutter, c'est-à-dire dans un
# binaire distribué : quiconque l'en extrayait pouvait rejoindre n'importe quel
# canal.
AGORA_APP_ID: str = config("AGORA_APP_ID", default="")
AGORA_APP_CERTIFICATE: str = config("AGORA_APP_CERTIFICATE", default="")
# Un appel dure quelques minutes ; une heure couvre celui qui s'éternise sans
# laisser un droit d'accès traîner.
AGORA_TOKEN_TTL_SECONDS: int = config("AGORA_TOKEN_TTL_SECONDS", default=3600, cast=int)

if GIS_ENABLED:
    INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.gis"]

# --------------------------------------------------------------- middleware

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --------------------------------------------------------------- CORS
#
# En-têtes que le navigateur a le droit d'envoyer. La liste par défaut de
# `django-cors-headers` ne connaît que les en-têtes standards ; `Idempotency-Key`
# est le nôtre (ADR-009, `apps.orders.views.IDEMPOTENCY_HEADER`), donc la
# requête préalable le refusait et **la création de commande échouait depuis le
# web** — seulement depuis le web, et seulement sur cette route, la seule qui
# pose un en-tête personnalisé. Le navigateur ne remonte alors qu'une erreur
# réseau nue : la vraie requête n'est jamais émise, rien n'atteint Django, et
# ses journaux restent muets.
#
# Ici et non dans `dev.py` : l'en-tête fait partie du contrat de l'API, il ne
# dépend pas de l'environnement — contrairement aux origines autorisées.
# Les tests, qui passent par le client Django, n'émettent jamais de requête
# préalable : c'est `tests/contract/test_cors.py` qui garde cette liste.
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------- base de données

# Certains hébergeurs ne publient la base que sous la forme d'une URL unique et
# n'exposent pas l'hôte et le port séparément : le blueprint Render, par
# exemple, ne sait injecter qu'une `connectionString`. Quand `DATABASE_URL` est
# présente, elle fait donc autorité ; les variables `POSTGRES_*` restent la voie
# normale en local et sous Docker Compose, où l'hôte est un nom de service.
DATABASE_URL: str = config("DATABASE_URL", default="")


def _database_from_url(url: str) -> dict[str, Any]:
    """Décompose une URL `postgres://` en réglages Django.

    Écrit à la main plutôt qu'en ajoutant `dj-database-url` : c'est une quinzaine
    de lignes contre une dépendance de plus dans l'image, et le seul schéma que
    ce projet ait jamais à lire est PostgreSQL.

    Les identifiants sont déchiffrés (`unquote`) : un mot de passe engendré par
    l'hébergeur contient des caractères réservés, que l'URL transporte
    pourcent-encodés. Sans cette étape, un `+` dans le mot de passe devient un
    espace et l'authentification échoue.
    """
    parsed = urlsplit(url)
    # `sslmode` voyage dans la chaîne de requête chez la plupart des hébergeurs,
    # alors que Django l'attend dans OPTIONS. Recopié tel quel plutôt que forcé
    # à `require` : le réseau interne de Render ne chiffre pas, et l'imposer
    # ferait échouer la connexion privée sans rien protéger de plus.
    query = dict(parse_qsl(parsed.query))
    options: dict[str, str] = {}
    if "sslmode" in query:
        options["sslmode"] = query["sslmode"]
    return {
        "NAME": unquote(parsed.path).lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
        "OPTIONS": options,
    }


def _database_from_env() -> dict[str, Any]:
    """Réglages issus des variables discrètes — le chemin historique."""
    return {
        "NAME": config("POSTGRES_DB", default="elcorazon"),
        "USER": config("POSTGRES_USER", default="elcorazon"),
        "PASSWORD": config("POSTGRES_PASSWORD", default=""),
        "HOST": config("POSTGRES_HOST", default="localhost"),
        "PORT": config("POSTGRES_PORT", default="5432"),
    }


DATABASES = {
    "default": {
        "ENGINE": (
            "django.contrib.gis.db.backends.postgis"
            if GIS_ENABLED
            else "django.db.backends.postgresql"
        ),
        **(_database_from_url(DATABASE_URL) if DATABASE_URL else _database_from_env()),
        "CONN_MAX_AGE": config("POSTGRES_CONN_MAX_AGE", default=60, cast=int),
        "ATOMIC_REQUESTS": False,  # les transactions sont explicites, dans les services
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"  # tables techniques uniquement

AUTH_USER_MODEL = "accounts.User"

# Sans cette liste, `validate_password()` ne valide **rien** : Django n'applique
# aucune politique par défaut, et « 12345678 » passe. Un test l'a attrapé.
AUTH_PASSWORD_VALIDATORS = [
    {
        # Refuse un mot de passe trop proche de l'e-mail ou du nom : c'est la
        # première chose qu'essaie un attaquant qui connaît sa cible.
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ("email", "full_name", "phone")},
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------- cache et files

REDIS_URL: str = config("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TIMEZONE = "UTC"

# --------------------------------------------------------------- API

REST_FRAMEWORK = {
    # ADR-005 : refus par défaut.  Toute route publique le déclare explicitement,
    # ce qui rend la liste des points d'entrée ouverts auditable en une recherche.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "common.exceptions.problem_detail_handler",
    # Limitation appliquée **partout** par défaut. Ne la déclarer que sur
    # quelques vues laissait sans quota les plus coûteuses — création de
    # commande, initiation de paiement — c'est-à-dire celles dont l'abus se
    # paie en argent et en verrous de base.
    #
    # Une vue qui déclare `throttle_classes` remplace ce défaut : c'est ainsi
    # que l'authentification et le webhook gardent leurs quotas propres.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Socle : lecture de catalogue, consultation d'historique.
        "anon": "60/min",
        "user": "120/min",
        # T1 — force brute, par adresse puis par identifiant tenté.
        "auth_ip": "20/min",
        "auth_identifier": "5/min",
        # Le prestataire de paiement, qui peut légitimement grouper ses envois.
        "webhook": "60/min",
        # Opérations coûteuses ou abusables. Les quotas sont volontairement bas :
        # aucun usage humain n'en approche, et ce sont ceux dont l'abus mobilise
        # des verrous, appelle un prestataire ou salit des données.
        "order_create": "10/min",
        "payment_initiate": "10/min",
        "cart_write": "60/min",
        "review_write": "5/min",
        "reward_redeem": "5/min",
        # Le suivi fait exception, et à la hausse : un livreur émet toutes les
        # dix secondes, et rattrape en rafale au retour du réseau après un
        # tunnel. Un quota serré couperait le suivi au moment précis où il
        # redevient utile.
        "tracking_ping": "240/min",
        # Le lien d'une part circule sur une messagerie et s'ouvre sans compte :
        # c'est la seule route non authentifiée qui lise des données de
        # commande. Le quota borne l'essai de jetons au hasard.
        "share_access": "30/min",
    },
    # Nombre de proxys entre le client et l'application.
    #
    # **Réglage de sécurité, pas d'optimisation.** Sans lui, DRF prend la chaîne
    # `X-Forwarded-For` entière pour identifier l'appelant. Or Nginx *ajoute* à
    # cette chaîne au lieu de la remplacer : un client qui envoie son propre
    # en-tête obtient une identité différente à chaque requête, et le limiteur
    # par IP — donc la moitié de T1 — se contourne en variant une chaîne de
    # caractères.
    #
    # Avec la valeur juste, DRF prend l'adresse que le dernier proxy a lui-même
    # inscrite, celle que le client ne peut pas forger.
    "NUM_PROXIES": config("NUM_PROXIES", default=1, cast=int),
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "El Corazón API",
    "DESCRIPTION": "API de la plateforme de commande et de livraison El Corazón.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    # Plusieurs modèles portent un champ `status` ou `kind`, chacun avec son
    # énumération. Sans ces noms, le générateur les baptise `Status5c8Enum` —
    # un identifiant qui change dès qu'un choix est ajouté, donc un client
    # généré qui casse sans raison visible. Les trois `kind` du social et du
    # support sont déclarés sur des `Serializer` nus (pas de `ModelSerializer`
    # dont le générateur tirerait un nom qualifié) : sans ces entrées, ils
    # collisionnent tous sous le même nom générique.
    "ENUM_NAME_OVERRIDES": {
        "OrderStatusEnum": "apps.orders.states.OrderStatus.choices",
        "PaymentStatusEnum": "apps.payments.models.PaymentStatus.choices",
        "PaymentMethodEnum": "apps.orders.models.PaymentMethod.choices",
        "PaymentProviderEnum": "apps.payments.models.PaymentProvider.choices",
        "UserTypeEnum": "apps.accounts.models.UserType.choices",
        "CallKindEnum": "apps.calls.models.CallKind.choices",
        "CallStatusEnum": "apps.calls.states.CallStatus.choices",
        "GroupKindEnum": "apps.social.models.GroupKind.choices",
        "PostKindEnum": "apps.social.models.PostKind.choices",
        "ComplaintKindEnum": "apps.support.models.ComplaintKind.choices",
        "DiscountKindEnum": "apps.promotions.models.DiscountKind.choices",
        "SubscriptionStatusEnum": "apps.loyalty.models.SubscriptionStatus.choices",
        "RewardKindEnum": "apps.loyalty.models.RewardKind.choices",
        "AchievementConditionEnum": "apps.gamification.models.AchievementCondition.choices",
        "ChallengeKindEnum": "apps.gamification.models.ChallengeKind.choices",
    },
}

# --------------------------------------------------------------- JWT (ADR-004)


def _read_key(var: str) -> str:
    r"""Lit une clé PEM depuis l'environnement.

    Une seule voie, la variable. Le fichier monté — `JWT_PRIVATE_KEY_PATH`,
    `JWT_PUBLIC_KEY_PATH` — a été retiré : aucun des hébergements visés ne monte
    de volume sur `/run/secrets/`, et ce chemin hérité d'un `.env` de
    développement a fait échouer deux déploiements d'affilée en désignant un
    fichier qui n'existait pas. Une seule voie, c'est une seule chose à vérifier
    le jour où une clé manque.

    Les deux écritures d'un PEM multiligne sont acceptées, et c'est ce qui rend
    la variable suffisante :

      — le texte tel quel, sur plusieurs lignes, que le tableau de bord de Render
        et un `Secret` Kubernetes transportent sans dommage ;
      — la même clé repliée sur une ligne, sauts de ligne échappés en `\n` —
        seule forme qu'un `env_file` de Docker Compose sait porter.

    Une clé absente rend la chaîne vide plutôt que de lever : c'est `prod.py` qui
    tranche, parce que lui seul sait que l'absence y est fatale — en test la paire
    est régénérée, et `dev.py` n'a pas à refuser de démarrer pour autant.
    """
    return str(config(var, default="")).replace("\\n", "\n")


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "RS256",
    "SIGNING_KEY": _read_key("JWT_SIGNING_KEY"),
    "VERIFYING_KEY": _read_key("JWT_VERIFYING_KEY"),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "sub",
    "TOKEN_TYPE_CLAIM": "typ",
}

# --------------------------------------------------------------- stockage
#
# Stockage objet **compatible S3** — MinIO en développement et en production,
# AWS S3 le jour venu sans toucher au code (ADR-011). Rien n'est écrit en dur :
# ni point d'accès, ni identifiants, ni nom de compartiment.
#
# Toute la mécanique vit dans `common/storage.py`, et **seulement là** : le
# reste du projet n'importe ni `boto3` ni `django-storages`, ce qu'un test
# d'architecture vérifie.

STORAGE_ENDPOINT_URL: str = config("S3_ENDPOINT_URL", default="")
STORAGE_REGION: str = config("S3_REGION", default="us-east-1")
STORAGE_ACCESS_KEY: str = config("S3_ACCESS_KEY", default="")
STORAGE_SECRET_KEY: str = config("S3_SECRET_KEY", default="")
STORAGE_USE_SSL: bool = config("S3_USE_SSL", default=False, cast=bool)

# MinIO ne résout pas un compartiment en sous-domaine (`bucket.hôte`) : il lui
# faut le chemin (`hôte/bucket`). AWS accepte les deux, donc `path` reste juste
# des deux côtés — la variable existe pour un CDN qui exigerait l'autre forme.
STORAGE_ADDRESSING_STYLE: str = config("S3_ADDRESSING_STYLE", default="path")

# Un compartiment par domaine plutôt qu'un seul fourre-tout. Ce n'est pas du
# rangement : la politique de lecture se pose **sur le compartiment**, donc
# c'est lui qui porte la frontière entre ce qui est public (le catalogue) et ce
# qui ne l'est jamais (les pièces d'identité des livreurs).
STORAGE_BUCKETS: dict[str, str] = {
    "products": config("S3_BUCKET_PRODUCTS", default="elcorazon-products"),
    "banners": config("S3_BUCKET_BANNERS", default="elcorazon-banners"),
    "users": config("S3_BUCKET_USERS", default="elcorazon-users"),
    "documents": config("S3_BUCKET_DOCUMENTS", default="elcorazon-documents"),
}

# Adresse publique des compartiments publics — celle que verra un navigateur.
# Distincte du point d'accès interne : en production, l'API parle à MinIO par
# le réseau Docker (`http://minio:9000`), que personne d'autre n'atteint. Vide,
# les URL publiques retombent sur le point d'accès, ce qui convient en
# développement.
STORAGE_PUBLIC_BASE_URL: str = config("S3_PUBLIC_URL", default="")

# Durée de vie d'une URL signée. Assez pour ouvrir un document, trop peu pour
# qu'un lien copié dans un courriel serve encore le lendemain.
STORAGE_SIGNED_URL_EXPIRE: int = config("S3_SIGNED_URL_EXPIRE", default=900, cast=int)

STORAGES = {
    # Le stockage par défaut est **privé**. C'est le sens de la sécurité par
    # défaut : un champ fichier ajouté demain sans stockage explicite atterrit
    # dans le compartiment signé, pas en libre accès.
    "default": {"BACKEND": "common.storage.CourierDocumentStorage"},
    "products": {"BACKEND": "common.storage.ProductImageStorage"},
    "banners": {"BACKEND": "common.storage.BannerStorage"},
    "users": {"BACKEND": "common.storage.UserMediaStorage"},
    "documents": {"BACKEND": "common.storage.CourierDocumentStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------- localisation

LANGUAGE_CODE = "fr"
LANGUAGES = [("fr", "Français"), ("en", "English")]
TIME_ZONE = "UTC"  # figé en UTC ; l'affichage local est la responsabilité du client
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------- métier

DEFAULT_CURRENCY = config("DEFAULT_CURRENCY", default="XOF")

# Part des frais de livraison reversée au livreur, en pourcentage. Le solde est
# la commission de la plateforme. En réglage plutôt qu'en dur : un point de
# commission ne doit pas demander un déploiement.
COURIER_FEE_SHARE_PERCENT: int = config("COURIER_FEE_SHARE_PERCENT", default=80, cast=int)

# Échantillonnage de l'écriture des positions. La diffusion temps réel, elle,
# est intégrale : c'est elle qui fait l'expérience de suivi, pas la persistance.
# À 10 s par livreur et 200 livreurs actifs, tout écrire produirait 1,7 million
# de lignes par jour pour une valeur analytique faible.
TRACKING_MIN_WRITE_SECONDS: int = config("TRACKING_MIN_WRITE_SECONDS", default=30, cast=int)
TRACKING_MIN_WRITE_METERS: int = config("TRACKING_MIN_WRITE_METERS", default=100, cast=int)

# Rétentions. Politique de conservation plutôt que constantes de code : elles
# se négocient et changent sans redéploiement.
TRACKING_RETENTION_DAYS: int = config("TRACKING_RETENTION_DAYS", default=30, cast=int)
IDEMPOTENCY_RETENTION_HOURS: int = config("IDEMPOTENCY_RETENTION_HOURS", default=72, cast=int)

# Panier collaboratif. L'échéance est **obligatoire** : sans elle, un panier de
# groupe reste ouvert indéfiniment et le groupe attend un hôte qui a oublié. Deux
# heures couvrent la commande d'un déjeuner d'équipe ; la borne haute existe pour
# qu'un panier ne survive pas au service qu'il prépare.
GROUP_CART_DEFAULT_WINDOW_MINUTES: int = config(
    "GROUP_CART_DEFAULT_WINDOW_MINUTES", default=120, cast=int
)
GROUP_CART_MAX_WINDOW_MINUTES: int = config("GROUP_CART_MAX_WINDOW_MINUTES", default=1440, cast=int)

# Service de notification push, résolu à l'appel (`apps.notifications.push`).
# `ConsolePushBackend` n'appelle personne et convient au développement comme
# aux tests ; le connecteur FCM se branche par cette variable.
PUSH_BACKEND: str = config("PUSH_BACKEND", default="apps.notifications.push.ConsolePushBackend")

# Fidélité. Un diviseur en unité mineure plutôt qu'un taux : à 100, une commande
# de 4 000 F rapporte 40 points, exactement. Un taux flottant donnerait
# 39,999… et une troncature qui dépend de l'arrondi de la machine.
LOYALTY_MINOR_UNITS_PER_POINT: int = config("LOYALTY_MINOR_UNITS_PER_POINT", default=100, cast=int)
# Les points s'éteignent après cette durée **sans mouvement**. Politique
# commerciale, donc réglage : elle se négocie et change sans redéploiement.
LOYALTY_EXPIRY_MONTHS: int = config("LOYALTY_EXPIRY_MONTHS", default=12, cast=int)
# Délai après l'échéance d'un abonnement pendant lequel un renouvellement est
# encore tenté avant de passer l'abonnement en expiré — le temps qu'un moyen
# de paiement refusé soit corrigé sans perdre l'abonnement pour un incident
# d'un jour.
SUBSCRIPTION_RENEWAL_GRACE_DAYS: int = config(
    "SUBSCRIPTION_RENEWAL_GRACE_DAYS", default=3, cast=int
)

# Firebase Cloud Messaging. Le fichier de compte de service est **monté**, comme
# les clés JWT : c'est un JSON multiligne, que ni `env_file` ni la plupart des
# gestionnaires de configuration ne savent porter sans échappement fragile.
FCM_CREDENTIALS_PATH: str = config("FCM_CREDENTIALS_PATH", default="")
FCM_PROJECT_ID: str = config("FCM_PROJECT_ID", default="")
# L'envoi est unitaire — l'API v1 n'a pas de diffusion groupée. Un délai court
# évite qu'un appareil injoignable retarde tous les suivants.
FCM_TIMEOUT_SECONDS: int = config("FCM_TIMEOUT_SECONDS", default=10, cast=int)

# Connecteur **par prestataire**, résolu à l'appel (`apps.payments.gateway`).
# Les espèces et le portefeuille n'appellent personne : le bac à sable leur
# convient et leur conviendra toujours. Seul `paydunya` s'adresse à un service
# externe, et le brancher est une variable d'environnement, pas un déploiement.
PAYMENT_GATEWAYS: dict[str, str] = {
    "paydunya": config("PAYDUNYA_GATEWAY", default="apps.payments.gateway.SandboxGateway"),
    "cash": "apps.payments.gateway.SandboxGateway",
    "wallet": "apps.payments.gateway.SandboxGateway",
}

# PayDunya. `test` vise le bac à sable du prestataire, `live` encaisse pour de
# bon : c'est la seule variable dont une erreur se paie en argent réel.
PAYDUNYA_MODE: str = config("PAYDUNYA_MODE", default="test")
PAYDUNYA_MASTER_KEY: str = config("PAYDUNYA_MASTER_KEY", default="")
PAYDUNYA_PRIVATE_KEY: str = config("PAYDUNYA_PRIVATE_KEY", default="")
PAYDUNYA_TOKEN: str = config("PAYDUNYA_TOKEN", default="")
PAYDUNYA_CALLBACK_URL: str = config("PAYDUNYA_CALLBACK_URL", default="")
# Délai court et explicite : la création de facture est dans le cycle de
# requête du client, qui attend devant son écran. Au-delà, un refus franc vaut
# mieux qu'une page bloquée.
PAYDUNYA_TIMEOUT_SECONDS: int = config("PAYDUNYA_TIMEOUT_SECONDS", default=10, cast=int)
SANDBOX_CHECKOUT_BASE_URL: str = config(
    "SANDBOX_CHECKOUT_BASE_URL", default="https://sandbox.elcorazon.app/checkout"
)

# Secret partagé avec le prestataire, servant à signer ses notifications. Sans
# valeur, aucune signature ne peut être valide — le webhook refuse tout, ce qui
# est le bon défaut : une configuration oubliée ferme la porte au lieu de
# l'ouvrir.
PAYMENT_WEBHOOK_SECRET: str = config("PAYMENT_WEBHOOK_SECRET", default="")

# --------------------------------------------------------------- journalisation

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "config.logging.JSONFormatter"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}
