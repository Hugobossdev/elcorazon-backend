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

# Repli de développement, sur le modèle d'`ALLOWED_HOSTS` plus haut et pour la
# même raison : le service Render a été créé à la main, il ignore donc les
# `envVars` du blueprint, et `CORS_ALLOWED_ORIGINS` y est vide. Sans aucune
# origine autorisée, *aucune* réponse ne porte `Access-Control-Allow-Origin` —
# pas même un 200 sur `/health/` — et les applications Flutter Web ne reçoivent
# rien. Dart ne rapporte alors qu'un `ApiException(0, network_error)`,
# indiscernable d'un serveur éteint, et le navigateur accuse le CORS sans jamais
# nommer le port fautif.
#
# Une expression régulière et non une liste : Flutter Web tire un port au hasard
# à chaque lancement, or une origine se déclare au port près. Whitelister un
# port tiré au sort serait à refaire à chaque `flutter run`.
#
# Ce que ce repli ouvre reste borné, et il faut le mesurer avant de s'en
# inquiéter : `CORS_ALLOW_CREDENTIALS` reste faux (le défaut de
# django-cors-headers, non modifié ici) et l'authentification passe par un jeton
# porté dans `Authorization`, jamais par un cookie. Une page tierce n'hérite donc
# d'aucune session : le navigateur ne joint spontanément rien qui identifie
# l'utilisateur. Elle ne gagne que ce qu'une requête anonyme obtient déjà — et
# seulement si elle est servie depuis `localhost`, donc depuis la machine même du
# développeur.
#
# À passer à `False` dès qu'un back-office aura une adresse stable : la déclarer
# dans `CORS_ALLOWED_ORIGINS` est plus étroit, et suffit.
# L'ancre `$` n'est pas décorative : `corsheaders` compare avec `re.match`, donc
# en **préfixe**. Sans elle, `http://localhost.exemple.invalid` serait accepté.
#
# Toujours défini, vide quand le repli est fermé, plutôt que posé sous un `if` :
# un réglage qui existe ou non selon l'environnement se prête mal à la lecture
# comme au rechargement du module.
CORS_ALLOW_LOCAL_DEV_ORIGINS = config("CORS_ALLOW_LOCAL_DEV_ORIGINS", default=True, cast=bool)

CORS_ALLOWED_ORIGIN_REGEXES = (
    [r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"] if CORS_ALLOW_LOCAL_DEV_ORIGINS else []
)

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

# Le service push, lui, ne s'oublie pas en restant vide : il **retombe** sur une
# valeur par défaut parfaitement fonctionnelle, et c'est ce qui le rend
# dangereux.
#
# `ConsolePushBackend` journalise et déclare tous les jetons livrés
# (`PushResult(delivered=…)`). Rien n'échoue, rien n'est retenté, aucune métrique
# ne bouge : un déploiement qui monte ses identifiants FCM sans poser cette
# variable *paraît* configuré et n'envoie rien. Le défaut est passé exactement
# ainsi — `.env.prod.example` déclarait `FCM_CREDENTIALS_PATH`, `FCM_PROJECT_ID`
# et `FCM_TIMEOUT_SECONDS`, et taisait `PUSH_BACKEND`.
#
# Ce qu'on perd alors n'est pas un confort : l'ADR-008 pose la notification push
# comme **doublure** du WebSocket pour les offres de course, parce qu'un livreur
# n'a pas son application au premier plan en roulant. Sans elle, une course
# proposée n'atteint personne.
#
# Le contrôle porte sur la classe et non sur les identifiants : ce sont eux qui
# sont vérifiés à l'usage, par le connecteur lui-même, et un jeu d'identifiants
# valide branché sur la console reste muet.
#
# Relu par `config` plutôt que pris à l'étoile de `base` : c'est la même lecture
# que les garde-fous ci-dessus, et elle dit sans ambiguïté d'où vient la valeur.
#
# Le défaut répété ici est celui de `base` — et non `""` : c'est **l'absence** de
# la variable qui constitue le défaut qu'on attrape, et un repli sur la chaîne
# vide passerait à côté du seul cas qui s'est réellement produit.
if config("PUSH_BACKEND", default="apps.notifications.push.ConsolePushBackend").endswith(
    "ConsolePushBackend"
):
    raise RuntimeError(
        "PUSH_BACKEND pointe sur ConsolePushBackend, qui n'envoie rien et déclare "
        "pourtant tout livré. En production, poser "
        "PUSH_BACKEND=apps.notifications.fcm.FirebaseCloudMessagingBackend."
    )

# Le connecteur de **paiement** est le même piège, en plus coûteux.
#
# `base.py` fait retomber `PAYDUNYA_GATEWAY` sur `apps.payments.gateway.
# SandboxGateway`. Ce connecteur n'est pas une maquette inerte : il ouvre des
# paiements, accepte des notifications et solde des commandes. Il diffère du
# vrai sur les deux points qui comptent :
#
# * `open_checkout` rend une adresse fabriquée localement — aucune facture n'est
#   ouverte chez le prestataire, donc aucun argent n'est jamais encaissé ;
# * `parse` lit le statut **dans le corps posté**, là où `PayDunyaGateway` le
#   relit chez PayDunya (`_confirm`). Un corps qui affirme « encaissé » est donc
#   cru sur parole.
#
# Le blueprint ne déclarait aucune de ces variables. La production encaissait
# donc par le bac à sable, et `PAYMENT_WEBHOOK_SECRET` valait la chaîne vide —
# c'est-à-dire que n'importe qui pouvait calculer une signature valide, la clé
# étant le défaut publiquement lisible dans ce dépôt.
#
# Ce que cela ouvrait n'avait rien de théorique :
# `POST /payments/{order}/initiate/` rend au client sa propre
# `provider_reference` (`TransactionSerializer`), et la route de webhook est
# `AllowAny`. Il ne restait donc rien à deviner — un client pouvait signer
# lui-même une notification « encaissé » et faire confirmer sa commande sans
# payer.
#
# Comme pour le push, le contrôle porte sur la **classe** et non sur les
# identifiants : ce sont eux qui sont vérifiés à l'usage, par le connecteur, et
# un jeu d'identifiants valide branché sur le bac à sable resterait sans effet.
#
# Le défaut répété ici est celui de `base` — et non `""` : c'est **l'absence**
# de la variable qui constitue le défaut qu'on attrape, et c'est le seul cas
# qui se soit réellement produit.
if config("PAYDUNYA_GATEWAY", default="apps.payments.gateway.SandboxGateway").endswith(
    "SandboxGateway"
):
    raise RuntimeError(
        "PAYDUNYA_GATEWAY pointe sur SandboxGateway, qui n'encaisse rien et croit "
        "sur parole le statut posté dans la notification. En production, poser "
        "PAYDUNYA_GATEWAY=apps.payments.paydunya.PayDunyaGateway."
    )

# Le secret des notifications, lui, s'oublie en restant **vide** — et un HMAC
# calculé avec une clé vide se recalcule par quiconque lit ce dépôt.
#
# Il reste nécessaire même avec le vrai connecteur PayDunya branché : les
# espèces et le portefeuille passent par `SandboxGateway`
# (`PAYMENT_GATEWAYS` dans `base.py`), dont `authenticate` s'appuie sur lui.
#
# Vérifié séparément des quatre secrets de la boucle ci-dessus parce que le
# message doit dire ce qui se joue : ce n'est pas « une variable manque », c'est
# « la porte du webhook est ouverte ».
if not config("PAYMENT_WEBHOOK_SECRET", default=""):
    raise RuntimeError(
        "PAYMENT_WEBHOOK_SECRET est vide : une signature de webhook calculée "
        "avec une clé vide est reproductible par n'importe qui, et une "
        "notification forgée solderait une commande impayée. Poser une valeur "
        "aléatoire longue, la même que celle du prestataire de test."
    )

# --------------------------------------------------------------- observabilité

# Remontée des exceptions.
#
# Le projet journalisait en JSON sur la sortie standard et n'avait **aucun
# agrégateur** : une 500 en production ne se découvrait que par l'appel d'un
# client. Les journaux de Render sont consultables, pas surveillés — personne
# ne lit un flux qui défile.
#
# Le SDK reste inerte tant que `SENTRY_DSN` est vide, ce qui est le cas par
# défaut. Aucun réglage à retirer pour déployer sans, et rien à ajouter au code
# pour déployer avec.
SENTRY_DSN: str = config("SENTRY_DSN", default="")

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.types import Event, Hint

    # Les types viennent du SDK (`sentry_sdk.types`) et non d'un `dict[str, object]`
    # écrit à la main : `before_send` attend `Event | None` en retour, et un
    # rappel typé trop largement passait la vérification tout en interdisant au
    # jour où l'on voudrait **supprimer** un événement en rendant `None`.
    def _expurger(evenement: Event, _indice: Hint) -> Event | None:
        """Retire des événements ce qui ne doit jamais quitter le serveur.

        `send_default_pii=False` couvre déjà l'adresse IP, l'identité et les
        cookies. Il ne couvre **pas** le corps des requêtes ni les en-têtes, or
        c'est précisément là que vivent les choses à ne pas envoyer :

        * `Authorization` porte un jeton d'accès utilisable pendant quinze
          minutes — capturé dans un incident, il donne la session à qui lit
          l'incident ;
        * `X-Signature` est l'empreinte d'une notification de paiement ;
        * le corps de `POST /auth/login/` contient un mot de passe en clair,
          celui de `/auth/verify/` un code à usage unique, et celui de
          `/payments/webhook/` la charge du prestataire.

        Le corps est retiré **entièrement** plutôt que champ par champ : une
        liste de clés sensibles s'oublie au premier champ ajouté, et l'oubli ne
        se voit pas. Ce qui reste — méthode, chemin, code de statut, trace — est
        ce dont on a besoin pour diagnostiquer.
        """
        requete = evenement.get("request")
        if isinstance(requete, dict):
            requete.pop("data", None)
            requete.pop("cookies", None)
            entetes = requete.get("headers")
            if isinstance(entetes, dict):
                for interdit in ("Authorization", "Cookie", "X-Signature", "X-Csrftoken"):
                    entetes.pop(interdit, None)
        return evenement

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        # Jamais d'identité ni d'adresse IP : le projet traite des adresses de
        # livraison et des numéros de téléphone, et un agrégateur d'incidents
        # n'a aucune raison d'en accumuler.
        send_default_pii=False,
        before_send=_expurger,
        # Échantillonnage des traces de performance. Nul par défaut : elles se
        # facturent, et l'urgence est de voir les erreurs, pas de mesurer les
        # latences. Se relève par l'environnement quand le besoin vient.
        traces_sample_rate=config("SENTRY_TRACES_SAMPLE_RATE", default=0.0, cast=float),
        # Distingue les incidents d'un déploiement de démonstration de ceux
        # d'une exploitation réelle.
        environment=config("SENTRY_ENVIRONMENT", default="production"),
    )
