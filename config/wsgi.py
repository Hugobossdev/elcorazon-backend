"""Point d'entrée WSGI.

Conservé pour les commandes de gestion et les outils qui l'attendent. Le
service réel passe par ASGI (`config.asgi`), qui sert HTTP et WebSocket.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_wsgi_application()
