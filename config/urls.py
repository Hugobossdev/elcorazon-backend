"""Routage racine.

Le versionnement est porté par l'URL (`/api/v1/`) — voir ADR-009 : visible dans
les journaux et les traces, trivial à router côté Nginx, et une v2 pourra
coexister sans négociation de contenu.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import admin
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

# Le back-office s'annonce pour ce qu'il est. Un titre par défaut « Django
# administration » sur un écran qui pilote une flotte et des encaissements
# laisse croire à un outil de développement qu'on peut manipuler sans
# conséquence.
admin.site.site_header = "El Corazón — exploitation"
admin.site.site_title = "El Corazón"
admin.site.index_title = "Back-office"

logger = logging.getLogger(__name__)


def healthcheck(_request: HttpRequest) -> JsonResponse:
    """Sonde de vivacité, sans accès base.

    Volontairement dissociée de l'état de PostgreSQL : une sonde de *liveness*
    qui échoue parce que la base est momentanément indisponible ferait
    redémarrer en boucle des conteneurs parfaitement sains.
    """
    return JsonResponse({"status": "ok", "version": settings.SPECTACULAR_SETTINGS["VERSION"]})


def _migrations_en_attente() -> int:
    """Nombre de migrations connues du code et absentes de la base.

    Lit le graphe des fichiers de migration et la table `django_migrations` :
    aucune écriture, aucun verrou. Le coût — charger le graphe — convient à une
    sonde de diagnostic, pas à `/health/`, que l'hébergeur interroge en boucle.
    """
    executeur = MigrationExecutor(connection)
    cibles = executeur.loader.graph.leaf_nodes()
    return len(executeur.migration_plan(cibles))


def readiness(_request: HttpRequest) -> JsonResponse:
    """Sonde de **disponibilité** — l'application peut-elle réellement servir ?

    ## Pourquoi elle est distincte de `/health/`

    Les deux répondent à des questions différentes, et les confondre coûte cher
    dans les deux sens :

    * `/health/` dit « le processus est vivant ». C'est elle que Render
      interroge, et un échec y déclenche un redémarrage. La lier à PostgreSQL
      ferait redémarrer en boucle des conteneurs parfaitement sains le jour
      d'une indisponibilité de base — le redémarrage ne réparant rien, la
      panne durerait plus longtemps ;
    * `/ready/` dit « les dépendances répondent ». Elle sert au diagnostic, au
      routage de trafic et à l'alerte. Elle **ne doit pas** être branchée sur
      `healthCheckPath`.

    Cette seconde sonde n'existait pas. Le seul moyen de savoir si la base
    répondait était d'appeler une route métier et de lire un 500 — c'est-à-dire
    de découvrir la panne par une requête d'utilisateur.

    ## Ce qu'elle vérifie, et ce qu'elle en dit

    PostgreSQL par un `SELECT 1`, le cache par un aller-retour, et le schéma
    par les migrations en attente. Chaque dépendance est rapportée séparément :
    « prêt » ou « pas prêt » ne dit pas laquelle a lâché, et c'est la première
    chose qu'on cherche. Un schéma en retard fait échouer la sonde comme une
    base absente : dans les deux cas, aucune route métier ne peut répondre.

    Le cache est signalé mais **ne fait pas échouer** la sonde. Redis en offre
    gratuite est volatil et plafonné ; l'application dégrade proprement sans lui
    (les quotas laissent passer plutôt que de refuser — voir
    `common.throttling`), alors qu'elle ne peut rien faire sans base. Les
    traiter pareil ferait sortir du service un serveur encore utile.

    503 et non 500 : c'est une indisponibilité temporaire d'une dépendance, pas
    une erreur de l'application. Les orchestrateurs et les sondes externes
    distinguent les deux.
    """
    dependances: dict[str, str] = {}

    try:
        with connection.cursor() as curseur:
            curseur.execute("SELECT 1")
            curseur.fetchone()
        dependances["database"] = "ok"
    except Exception as erreur:
        # Le message de l'exception peut porter un hôte, un port, parfois un
        # nom d'utilisateur : il va au journal, jamais à la réponse.
        logger.error("readiness.database", extra={"detail": str(erreur)})
        dependances["database"] = "indisponible"

    try:
        cache.set("readiness", "1", timeout=5)
        dependances["cache"] = "ok" if cache.get("readiness") == "1" else "degrade"
    except Exception as erreur:
        logger.warning("readiness.cache", extra={"detail": str(erreur)})
        dependances["cache"] = "indisponible"

    # Le schéma, ensuite — et seulement si la base répond.
    #
    # Panne vécue le 2026-09-13 : un champ ajouté à `Restaurant` pendant que le
    # conteneur de développement tournait. uvicorn `--reload` a rechargé le
    # modèle, mais `migrate` ne s'exécute qu'au **démarrage** du conteneur
    # (`docker-compose.yml`). Chaque requête touchant un établissement —
    # annuaire, carte, panier — rendait alors 500 `ProgrammingError`, pendant
    # que cette sonde répondait « ready ». L'application cliente, elle, lisait
    # « aucun restaurant en service ».
    en_attente = 0
    if dependances["database"] == "ok":
        try:
            en_attente = _migrations_en_attente()
            dependances["migrations"] = "ok" if en_attente == 0 else "en attente"
        except Exception as erreur:
            logger.warning("readiness.migrations", extra={"detail": str(erreur)})
            dependances["migrations"] = "illisible"

    pret = dependances["database"] == "ok" and en_attente == 0
    if en_attente:
        logger.error("readiness.migrations_pending", extra={"pending": en_attente})
    return JsonResponse(
        {
            "status": "ready" if pret else "not-ready",
            "version": settings.SPECTACULAR_SETTINGS["VERSION"],
            "dependencies": dependances,
        },
        status=200 if pret else 503,
    )


def index(_request: HttpRequest) -> JsonResponse:
    """Racine de service.

    Sans elle, `/` répond 404 : c'est correct — aucune route n'y est déclarée —
    mais c'est la première adresse qu'ouvre quiconque reçoit l'URL du service,
    et le journal se remplit d'avertissements `Not Found: /` provenant du
    sondage de l'hébergeur autant que des curieux. Une page d'accueil qui
    annonce les points d'entrée coûte six lignes et évite de faire croire à une
    panne là où le service va bien.

    Volontairement sans accès base, comme la sonde : cette réponse doit rester
    vraie même quand PostgreSQL est indisponible.
    """
    return JsonResponse(
        {
            "service": "El Corazón — API",
            "version": settings.SPECTACULAR_SETTINGS["VERSION"],
            "endpoints": {
                "health": "/health/",
                "ready": "/ready/",
                "api": "/api/v1/",
                "schema": "/api/v1/schema/",
                "admin": "/admin/",
            },
        }
    )


api_v1 = [
    path("auth/", include("apps.accounts.urls")),
    path("administration/", include("apps.accounts.backoffice_urls")),
    path("geography/", include("apps.geography.urls")),
    path("restaurants/", include("apps.restaurants.urls")),
    path("catalog/", include("apps.catalog.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("production/", include("apps.production.urls")),
    path("profiles/", include("apps.profiles.urls")),
    path("carts/", include("apps.carts.urls")),
    path("group-carts/", include("apps.groupcarts.urls")),
    path("promotions/", include("apps.promotions.urls")),
    path("orders/", include("apps.orders.urls")),
    path("payments/", include("apps.payments.urls")),
    path("delivery/", include("apps.delivery.urls")),
    path("tracking/", include("apps.tracking.urls")),
    path("calls/", include("apps.calls.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("loyalty/", include("apps.loyalty.urls")),
    path("gamification/", include("apps.gamification.urls")),
    path("social/", include("apps.social.urls")),
    path("support/", include("apps.support.urls")),
    path("analytics/", include("apps.analytics.urls")),
    path("search/", include("apps.search.urls")),
    # Renseigné au fil des phases — voir docs/architecture/README.md
]

urlpatterns = [
    path("", index, name="index"),
    path("health/", healthcheck, name="health"),
    path("ready/", readiness, name="ready"),
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"), namespace="v1")),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
]

if settings.DEBUG:
    urlpatterns += [
        path(
            "api/v1/docs/",
            SpectacularSwaggerView.as_view(url_name="schema"),
            name="docs",
        ),
    ]
