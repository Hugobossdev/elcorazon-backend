"""Valide un envoi FCM contre un vrai projet Firebase — voir `apps/notifications/fcm.py`.

Aucun projet Firebase n'est configuré dans ce dépôt : le connecteur suit le
contrat documenté par Google pour l'API v1, mais n'a jamais été confronté au
service réel. Cette commande existe pour que cette validation, le jour où des
credentials réels sont disponibles, tienne en une ligne plutôt qu'en un script
jetable écrit dans l'urgence.

Usage, une fois les credentials en place :

    FCM_CREDENTIALS_PATH=/chemin/vers/service-account.json \\
    FCM_PROJECT_ID=mon-projet-firebase \\
    PUSH_BACKEND=apps.notifications.fcm.FirebaseCloudMessagingBackend \\
    python manage.py send_test_push <jeton-appareil-de-test>

Elle passe par `apps.notifications.push.backend()` — donc par le réglage
`PUSH_BACKEND` — exactement le chemin emprunté en production. Un appel direct
au connecteur FCM laisserait passer une erreur de câblage (mauvais backend
configuré) que cette commande est justement censée détecter.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.notifications.push import PushMessage, backend


class Command(BaseCommand):
    help = (
        "Envoie une notification de test à un jeton d'appareil réel, via le "
        "PUSH_BACKEND configuré — pour valider FCM contre un vrai projet Firebase."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("token", help="Jeton d'appareil FCM à tester.")
        parser.add_argument(
            "--title", default="El Corazón", help="Titre de la notification de test."
        )
        parser.add_argument(
            "--body",
            default="Ceci est un envoi de validation.",
            help="Corps de la notification de test.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        message = PushMessage(title=options["title"], body=options["body"], data={"kind": "test"})
        result = backend().send([options["token"]], message)

        if result.delivered:
            self.stdout.write(self.style.SUCCESS("Livré."))

        if result.unregistered:
            self.stdout.write(
                self.style.WARNING(
                    "Appareil signalé définitivement injoignable "
                    "(UNREGISTERED / INVALID_ARGUMENT / SENDER_ID_MISMATCH / NOT_FOUND). "
                    "C'est le point à vérifier en premier : le code exact figure dans la "
                    "ligne « fcm.rejet » ci-dessus. Confirmer que ce jeton est effectivement "
                    "mort avant de faire confiance à cette classification pour la purge "
                    "automatique en production."
                )
            )

        if result.failed:
            self.stdout.write(
                self.style.ERROR(
                    "Échec — panne passagère ou erreur de configuration. Le détail est "
                    "dans les journaux ci-dessus : « fcm.rejet » (Google a répondu, `code` "
                    "porte son refus), « fcm.authentification » (compte de service illisible "
                    "ou refusé) ou « fcm.reseau » (rien n'est parti)."
                )
            )
