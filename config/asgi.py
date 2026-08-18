"""Point d'entrée ASGI — sert HTTP et WebSocket dans le même processus.

Voir ADR-001. Scinder en deux déploiements ajouterait de l'exploitation sans
gain à ce volume ; la séparation reste possible plus tard par routage Nginx sur
le préfixe, sans toucher au code.
"""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

# L'application HTTP est initialisée en premier : elle déclenche `django.setup()`,
# donc le chargement du registre des modèles.  Les consommateurs WebSocket
# importés ensuite peuvent alors référencer des modèles sans erreur d'application
# non chargée.
django_asgi_app = get_asgi_application()

from config.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # L'authentification n'est **pas** faite ici par un middleware de session :
        # elle est faite dans `connect()` de chaque consommateur, à partir du JWT,
        # en même temps que la vérification du droit sur la ressource (ADR-008).
        # C'est ce couplage qui ferme L3 — un socket accepté est un socket dont
        # le périmètre métier a déjà été validé.
        "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
    }
)
