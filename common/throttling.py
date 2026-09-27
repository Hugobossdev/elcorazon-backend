"""Quotas des opérations coûteuses.

Le socle `anon` / `user` s'applique partout et convient à la lecture. Il est
trop large pour une poignée d'écritures dont l'abus ne coûte pas une requête de
plus mais un verrou de base, un appel à un prestataire, ou une donnée salie.

Ces classes ne portent aucune logique : elles nomment un quota. Le nommer est
tout l'intérêt — `order_create` se lit dans les réglages et se règle sans
toucher au code, là où un nombre écrit dans une vue demande un déploiement.

Toutes dérivent de `UserRateThrottle` : ces opérations exigent déjà un compte,
donc l'appelant est toujours identifiable. Compter par adresse IP y serait
moins juste — plusieurs clients derrière le même opérateur mobile partagent
souvent une sortie.

**Ce que devient un quota quand le cache tombe.** Un compteur de débit vit dans
le cache : `allow_request` le lit à chaque requête. Si le cache est injoignable,
`django_redis` laisse remonter l'erreur de connexion, DRF ne l'attrape pas, et
la requête part en 500 — *avant* d'avoir atteint la vue. Une panne de cache
rendait donc toute l'API publique indisponible, catalogue compris, pendant que
la base et l'application se portaient très bien (constaté en production le
19/08/2026 : `catalog`, `restaurants` et `schema` en 500, `/health/` et l'admin
Django en 200).

Le comportement en panne est désormais déclaré, classe par classe :

* `FailOpenOnCacheOutage` — la requête passe, sans être comptée. Pour ce dont
  l'abus reste borné autrement : la lecture, les écritures qu'une règle de base
  contraint déjà, la télémétrie, et ce qu'une signature garde ;
* `FailClosedOnCacheOutage` — la requête est refusée en 503. Pour ce dont le
  quota est la **seule** protection : la force brute sur les identifiants, et
  les gestes qui brûlent de la valeur.

Le partage ne suit pas le coût de l'opération mais ce qui reste debout sans le
compteur. Un catalogue sans quota reste un catalogue lisible ; un `/auth/login`
sans quota est une porte ouverte. Choisir le silence pour l'un et le refus pour
l'autre, c'est écarter l'alternative qui nous a coûté la production : tout
abattre.

Aucune de ces classes ne masque la panne — `FailOpenOnCacheOutage` journalise en
`WARNING` chaque requête laissée passer. Un cache absent doit rester visible
même quand il ne casse plus rien.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django_redis.exceptions import ConnectionInterrupted
from redis.exceptions import RedisError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.throttling import (
    AnonRateThrottle,
    BaseThrottle,
    ScopedRateThrottle,
    UserRateThrottle,
)

if TYPE_CHECKING:
    # **Jamais à l'exécution.** Ce module est nommé dans
    # `DEFAULT_THROTTLE_CLASSES`, et DRF résout ce réglage pendant l'import de
    # `rest_framework.views` — dans le corps de `class APIView`. Importer
    # `rest_framework.views` ici referme donc la boucle sur un module à moitié
    # construit, et Django ne démarre plus du tout : `ImportError: cannot import
    # name 'APIView' from partially initialized module`.
    #
    # Le piège ne se voit pas dans `apps.accounts.throttling`, qui importe les
    # mêmes noms sans dommage : lui n'est chargé que par une vue, donc bien
    # après. C'est l'appartenance au réglage par défaut qui l'interdit ici.
    from rest_framework.request import Request
    from rest_framework.views import APIView

__all__ = [
    "CACHE_OUTAGE",
    "CartWriteThrottle",
    "FailClosedOnCacheOutage",
    "FailOpenOnCacheOutage",
    "OrderCreationThrottle",
    "PaymentInitiationThrottle",
    "QuotaUnavailable",
    "ResilientAnonRateThrottle",
    "ResilientScopedRateThrottle",
    "ResilientUserRateThrottle",
    "ReviewWriteThrottle",
    "RewardRedemptionThrottle",
    "StrictScopedRateThrottle",
    "TrackingPingThrottle",
]

logger = logging.getLogger(__name__)

#: Ce qu'un cache injoignable fait remonter jusqu'au limiteur.
#:
#: `django_redis` enveloppe d'abord l'erreur dans `ConnectionInterrupted`, puis
#: la relaie telle quelle (`raise e.__cause__`) selon le chemin emprunté : les
#: deux formes se présentent, il faut donc les deux.
#:
#: `RedisError` et non le seul `ConnectionError` : un cache saturé refuse ses
#: écritures par une `ResponseError` (« OOM command not allowed »), qui n'est
#: pas une panne de connexion mais produit ici exactement le même 500. L'offre
#: gratuite de Render plafonnant à 25 Mio, ce cas n'a rien de théorique.
CACHE_OUTAGE: tuple[type[BaseException], ...] = (ConnectionInterrupted, RedisError)


class QuotaUnavailable(APIException):
    """Le compteur de débit est hors service, et l'opération l'exige.

    503 et non 500 : la distinction est lisible par le client comme par la
    supervision. Un 500 annonce un défaut du code, qu'aucune reprise ne
    réparera ; un 503 annonce une indisponibilité passagère, dont la reprise
    est précisément la bonne réponse.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "Le compteur de limitation est momentanément indisponible. "
        "Cette opération est suspendue le temps qu'il revienne."
    )
    default_code = "quota_unavailable"


class FailOpenOnCacheOutage(BaseThrottle):
    """Laisse passer ce que seul le débit protégeait.

    À n'appliquer qu'à des routes dont l'abus reste borné sans compteur. Le
    quota n'y est qu'une précaution contre la boucle automatisée, jamais le
    dernier verrou.
    """

    def allow_request(self, request: Request, view: APIView) -> bool:
        try:
            return super().allow_request(request, view)
        except CACHE_OUTAGE:
            logger.warning(
                "Quota « %s » non appliqué : cache injoignable. Requête non comptée.",
                getattr(self, "scope", type(self).__name__),
                exc_info=True,
            )
            return True


class FailClosedOnCacheOutage(BaseThrottle):
    """Refuse en 503 plutôt que de lever le dernier verrou.

    Laisser passer reviendrait à ouvrir la force brute — ou la dépense — au
    moment précis où la supervision est aveugle, ce qu'un attaquant peut
    provoquer plutôt qu'attendre.
    """

    def allow_request(self, request: Request, view: APIView) -> bool:
        try:
            return super().allow_request(request, view)
        except CACHE_OUTAGE as exc:
            logger.error(
                "Quota « %s » inapplicable : cache injoignable. Requête refusée en 503.",
                getattr(self, "scope", type(self).__name__),
                exc_info=True,
            )
            raise QuotaUnavailable() from exc


class ResilientAnonRateThrottle(FailOpenOnCacheOutage, AnonRateThrottle):
    """Le socle anonyme — lecture publique du catalogue et de la carte."""


class ResilientUserRateThrottle(FailOpenOnCacheOutage, UserRateThrottle):
    """Le socle authentifié — consultation d'historique, navigation."""


class ResilientScopedRateThrottle(FailOpenOnCacheOutage, ScopedRateThrottle):
    """Quota nommé par la vue, effacé si le cache manque."""


class StrictScopedRateThrottle(FailClosedOnCacheOutage, ScopedRateThrottle):
    """Quota nommé par la vue, opposable même sans cache — en refusant."""


class OrderCreationThrottle(FailClosedOnCacheOutage, UserRateThrottle):
    """Passage de commande.

    L'opération la plus chère du backend : elle verrouille la commande, relit
    le catalogue, interroge PostGIS et écrit une dizaine de lignes. Dix par
    minute est déjà dix fois ce qu'un humain fait ; au-delà, c'est une boucle.

    Fermé en panne de cache : c'est justement cette boucle que plus rien
    n'arrêterait, et chaque tour laisse une commande derrière lui.
    """

    scope = "order_create"


class PaymentInitiationThrottle(FailClosedOnCacheOutage, UserRateThrottle):
    """Ouverture d'une demande de paiement.

    Chaque appel crée une transaction et s'adresse au prestataire. Sans quota,
    un client qui tape sur le bouton laisse une traînée de transactions en
    attente que le rapprochement comptable devra expliquer.

    Fermé en panne de cache : cette traînée-là se paie chez un tiers, et ne
    s'efface pas en redémarrant.
    """

    scope = "payment_initiate"


class RewardRedemptionThrottle(FailClosedOnCacheOutage, UserRateThrottle):
    """Échange de points contre une récompense.

    Chaque appel prend un verrou sur le compte, écrit au journal et **frappe un
    code promotionnel**. Sans quota, un client qui tape sur le bouton dépense
    tout son solde en une rafale de codes qu'il n'a pas voulus : le débit est
    atomique (F1) et le solde ne devient jamais négatif, mais rien là-dedans ne
    dit qu'un second échange était voulu. Le quota est ce qui distingue le geste
    répété de la boucle.

    Fermé en panne de cache : le quota *est* ici la seule chose qui les
    distingue, et un solde brûlé ne se reconstitue pas.
    """

    scope = "reward_redeem"


class CartWriteThrottle(FailOpenOnCacheOutage, UserRateThrottle):
    """Écriture dans le panier.

    Plus permissif : ajouter, retirer, changer une quantité sont des gestes
    répétés et légitimes. Le quota n'est là que contre la boucle automatisée.

    Ouvert en panne de cache : un panier est réversible, borné à un compte, et
    n'engage rien tant qu'il n'est pas commandé — c'est `OrderCreationThrottle`
    qui garde la porte qui compte.
    """

    scope = "cart_write"


class ReviewWriteThrottle(FailOpenOnCacheOutage, UserRateThrottle):
    """Dépôt d'avis.

    Serré parce qu'un avis par article et par utilisateur est déjà la règle
    (S5) : cinq par minute ne gêne personne et arrête le remplissage
    automatisé.

    Ouvert en panne de cache : cette règle-là vit en base, pas dans le
    compteur, et elle continue de tenir sans lui.
    """

    scope = "review_write"


class TrackingPingThrottle(FailOpenOnCacheOutage, UserRateThrottle):
    """Remontée de position.

    Le seul quota **relevé** du projet. Un livreur émet toutes les dix
    secondes, et rattrape en rafale au retour du réseau : un livreur sorti d'un
    tunnel envoie d'un coup ce qu'il n'a pas pu transmettre. Le couper là
    reviendrait à perdre le suivi au moment précis où il redevient utile.

    Ouvert en panne de cache, pour cette raison exactement : refuser les
    positions parce qu'un compteur manque ferait disparaître les livraisons en
    cours de l'écran de l'exploitation.
    """

    scope = "tracking_ping"
