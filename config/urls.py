"""Routage racine.

Le versionnement est porté par l'URL (`/api/v1/`) — voir ADR-009 : visible dans
les journaux et les traces, trivial à router côté Nginx, et une v2 pourra
coexister sans négociation de contenu.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
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


def healthcheck(_request: HttpRequest) -> JsonResponse:
    """Sonde de vivacité, sans accès base.

    Volontairement dissociée de l'état de PostgreSQL : une sonde de *liveness*
    qui échoue parce que la base est momentanément indisponible ferait
    redémarrer en boucle des conteneurs parfaitement sains.
    """
    return JsonResponse({"status": "ok", "version": settings.SPECTACULAR_SETTINGS["VERSION"]})


api_v1 = [
    path("auth/", include("apps.accounts.urls")),
    path("administration/", include("apps.accounts.backoffice_urls")),
    path("geography/", include("apps.geography.urls")),
    path("restaurants/", include("apps.restaurants.urls")),
    path("catalog/", include("apps.catalog.urls")),
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
    path("health/", healthcheck, name="health"),
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
