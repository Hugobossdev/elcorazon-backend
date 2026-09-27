"""Application Celery.

Tout appel réseau sortant quitte le cycle de requête (ADR-008) : un envoi FCM
demande un jeton OAuth puis un POST par appareil, ce qui ajouterait des
centaines de millisecondes à chaque changement de statut de commande.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("elcorazon")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

#: L'heure des tâches quotidiennes, en UTC.
#:
#: **Un horaire, et non un intervalle de 24 h**, pour une raison d'exploitation
#: qui ne se voit pas en développement : une entrée planifiée à l'intervalle
#: compte à partir du démarrage de `beat`, et `beat` perd son dernier passage à
#: chaque redéploiement (son fichier vit dans `/tmp` chez Render). Un service
#: qui déploie plus d'une fois par jour repousse donc indéfiniment ses tâches
#: quotidiennes — rappels d'expiration, extinction des points, purge des
#: appareils : elles **ne tournaient jamais**.
#:
#: 5 h UTC : la nuit sur tous les marchés d'Afrique de l'Ouest desservis
#: (UTC−1 à UTC+1), donc hors du service, et la même journée civile pour tous —
#: ce que `timezone.localdate()` lit du côté serveur.
HEURE_QUOTIDIENNE = crontab(hour=5, minute=0)

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
    "remind-document-expiry": {
        # Les rappels tombent aux seuils J-30, J-7, J-1 et J0. La tâche est
        # idempotente et **rattrape** les journées manquées (`DocumentReminder`) :
        # une panne de `beat` retarde un rappel, elle ne le perd plus.
        "task": "apps.delivery.tasks.remind_document_expiry",
        "schedule": HEURE_QUOTIDIENNE,
    },
    "expire-points": {
        # Les points s'éteignent après une période sans mouvement. Quotidien :
        # la fenêtre se compte en mois, une passe par jour suffit largement.
        "task": "apps.loyalty.tasks.expire_points",
        "schedule": HEURE_QUOTIDIENNE,
    },
    "purge-unregistered-devices": {
        # Un appareil que le service push ne déclare jamais injoignable mais
        # qui ne se manifeste plus : téléphone perdu, application désinstallée
        # sans notification. Quotidien, la fenêtre étant de six mois.
        "task": "apps.notifications.tasks.purge_unregistered_devices",
        "schedule": HEURE_QUOTIDIENNE,
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
    "send-scheduled-campaigns": {
        # Toutes les cinq minutes : une campagne programmée à 18 h part entre
        # 18 h et 18 h 05, ce qui est la précision d'un envoi commercial. Le
        # tour est idempotent (`send_campaign` relit sous verrou), et rattrape
        # les heures passées pendant un arrêt — une campagne perdue vaut bien
        # pire qu'une campagne en retard de dix minutes.
        "task": "apps.notifications.tasks.send_scheduled_campaigns",
        "schedule": 300.0,
    },
    "expire-unpaid-orders": {
        # Toutes les cinq minutes, face à un délai de trente : une commande
        # abandonnée rend son stock au plus tard trente-cinq minutes après la
        # dernière tentative de paiement.
        "task": "apps.payments.tasks.expire_unpaid_orders",
        "schedule": 300.0,
    },
    "expire-stale-offers": {
        # Chaque minute : une proposition sans réponse bloque un repas prêt. Le
        # tour clôt ce qui a dépassé `DELIVERY_OFFER_TTL_SECONDS`, propose au
        # livreur suivant, et rattrape les commandes prêtes restées sans course
        # faute de livreur en ligne au moment où elles l'étaient devenues.
        "task": "apps.delivery.tasks.expire_stale_offers",
        "schedule": 60.0,
    },
}
