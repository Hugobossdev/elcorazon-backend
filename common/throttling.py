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
"""

from __future__ import annotations

from rest_framework.throttling import UserRateThrottle

__all__ = [
    "CartWriteThrottle",
    "OrderCreationThrottle",
    "PaymentInitiationThrottle",
    "ReviewWriteThrottle",
    "RewardRedemptionThrottle",
    "TrackingPingThrottle",
]


class OrderCreationThrottle(UserRateThrottle):
    """Passage de commande.

    L'opération la plus chère du backend : elle verrouille la commande, relit
    le catalogue, interroge PostGIS et écrit une dizaine de lignes. Dix par
    minute est déjà dix fois ce qu'un humain fait ; au-delà, c'est une boucle.
    """

    scope = "order_create"


class PaymentInitiationThrottle(UserRateThrottle):
    """Ouverture d'une demande de paiement.

    Chaque appel crée une transaction et s'adresse au prestataire. Sans quota,
    un client qui tape sur le bouton laisse une traînée de transactions en
    attente que le rapprochement comptable devra expliquer.
    """

    scope = "payment_initiate"


class RewardRedemptionThrottle(UserRateThrottle):
    """Échange de points contre une récompense.

    Chaque appel prend un verrou sur le compte, écrit au journal et **frappe un
    code promotionnel**. Sans quota, un client qui tape sur le bouton dépense
    tout son solde en une rafale de codes qu'il n'a pas voulus : le débit est
    atomique (F1) et le solde ne devient jamais négatif, mais rien là-dedans ne
    dit qu'un second échange était voulu. Le quota est ce qui distingue le geste
    répété de la boucle.
    """

    scope = "reward_redeem"


class CartWriteThrottle(UserRateThrottle):
    """Écriture dans le panier.

    Plus permissif : ajouter, retirer, changer une quantité sont des gestes
    répétés et légitimes. Le quota n'est là que contre la boucle automatisée.
    """

    scope = "cart_write"


class ReviewWriteThrottle(UserRateThrottle):
    """Dépôt d'avis.

    Serré parce qu'un avis par article et par utilisateur est déjà la règle
    (S5) : cinq par minute ne gêne personne et arrête le remplissage
    automatisé.
    """

    scope = "review_write"


class TrackingPingThrottle(UserRateThrottle):
    """Remontée de position.

    Le seul quota **relevé** du projet. Un livreur émet toutes les dix
    secondes, et rattrape en rafale au retour du réseau : un livreur sorti d'un
    tunnel envoie d'un coup ce qu'il n'a pas pu transmettre. Le couper là
    reviendrait à perdre le suivi au moment précis où il redevient utile.
    """

    scope = "tracking_ping"
