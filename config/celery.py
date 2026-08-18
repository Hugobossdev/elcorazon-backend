"""Application Celery.

Tout appel réseau sortant quitte le cycle de requête (ADR-008) : un envoi FCM
demande un jeton OAuth puis un POST par appareil, ce qui ajouterait des
centaines de millisecondes à chaque changement de statut de commande.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("elcorazon")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Tâches planifiées.
#
# Le calendrier est rempli **au fur et à mesure que les tâches existent** : une
# entrée pointant vers une tâche non enregistrée est envoyée à chaque tour par
# beat et rejetée par le worker, ce qui produit une alerte permanente à laquelle
# l'équipe finit par ne plus prêter attention. Chaque entrée ci-dessous pointe
# vers une tâche qui existe réellement.
app.conf.beat_schedule = {
    "purge-stale-locations": {
        # Le suivi n'a de valeur qu'en direct. Sans purge, la table des
        # positions croît d'environ 1,7 M de lignes par jour à 200 livreurs.
        "task": "apps.tracking.tasks.purge_stale_locations",
        "schedule": 3600.0,
    },
    "purge-idempotency-keys": {
        # ADR-009 : les clés consommées ne servent plus au-delà de la fenêtre
        # de retry d'un client mobile.
        "task": "apps.orders.tasks.purge_idempotency_keys",
        "schedule": 3600.0,
    },
    "expire-points": {
        # Les points s'éteignent après une période sans mouvement. Quotidien :
        # la fenêtre se compte en mois, une passe par jour suffit largement.
        "task": "apps.loyalty.tasks.expire_points",
        "schedule": 86400.0,
    },
    "purge-unregistered-devices": {
        # Un appareil que le service push ne déclare jamais injoignable mais
        # qui ne se manifeste plus : téléphone perdu, application désinstallée
        # sans notification. Quotidien, la fenêtre étant de six mois.
        "task": "apps.notifications.tasks.purge_unregistered_devices",
        "schedule": 86400.0,
    },
    "expire-group-carts": {
        # Toutes les cinq minutes : l'échéance est déjà opposée à chaque ajout,
        # donc rien d'incorrect ne passe entre deux tours. Ce qui se joue ici est
        # la fermeture visible — un participant qui attend doit apprendre que
        # c'est fini en quelques minutes, pas à l'heure suivante.
        "task": "apps.groupcarts.tasks.expire_group_carts",
        "schedule": 300.0,
    },
    "renew-subscriptions": {
        # Horaire, face à des périodes comptées en jours et un délai de grâce
        # compté en jours lui aussi : une passe par jour laisserait un
        # abonnement facturable rester en attente près de 24 h avant d'être
        # même tenté.
        "task": "apps.loyalty.tasks.renew_subscriptions",
        "schedule": 3600.0,
    },
}
