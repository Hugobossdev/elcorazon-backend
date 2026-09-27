"""Journal des décisions d'exploitation — qui a changé quoi, et depuis quelle valeur.

## Ce que ce module comble

Aucune trace n'existait. Déplacer un établissement de trois cents mètres,
redessiner une zone, doubler un forfait de livraison : tout cela s'écrivait en
base sans que rien ne dise qui l'avait décidé ni ce qu'il y avait avant. Le jour
où les frais d'un quartier changent sans explication, la seule réponse
disponible était « c'est comme ça maintenant ».

Ces trois écritures ont une propriété commune qui les distingue du reste : elles
sont **silencieuses et coûteuses**. Un catalogue mal saisi se voit à l'écran ; un
rayon de zone réduit de deux kilomètres ne se voit que dans les commandes qu'on
ne reçoit plus.

## Ce qui est journalisé, et ce qui ne l'est pas

Seulement ce que l'exploitation a besoin de reconstituer : la géographie, les
barèmes, **les droits** — permissions d'un rôle, rôles et périmètre d'un compte
du personnel, blocage d'un client — et **l'argent qui sort**, avec les
décisions qui engagent l'enseigne envers un client. Les droits ont la même
propriété que les barèmes : un rôle qui gagne `orders.refund` ne se voit dans
aucun écran. Les sorties d'argent en ont une autre : leur ligne dit où en est
*ce* versement, et rien ne dit ce qu'un opérateur a signé cette semaine.

Journaliser toute écriture produirait un volume qui rend le journal
illisible — et donc inutilisé, ce qui est pire qu'absent.

Les transitions d'état ont déjà leur trace ailleurs (machines à états,
événements de domaine) ; les dupliquer ici ferait deux vérités. **Sauf quand
cet ailleurs n'existe pas** : un établissement n'a aucun historique d'états,
et sa transition ignore son auteur — sa mise en service et sa suspension se
consignent donc ici (`restaurant.status`), faute de quoi personne ne pourrait
dire qui a retiré une cuisine de l'application cliente.

## Pourquoi la cible n'est pas une clé étrangère

`target_id` est un UUID nu, doublé d'un `target_label` figé au moment du
changement. Trois raisons, et la troisième est décisive :

* une clé étrangère vers `Restaurant` ferait dépendre `common` de
  `restaurants`, ce que le graphe interdit (ADR-002) ;
* une même table doit pouvoir viser des pays, des zones et des établissements ;
* **un journal doit survivre à ce qu'il décrit.** Une entrée qui disparaîtrait
  avec la zone supprimée ne servirait précisément dans aucun des cas où l'on
  ouvre un journal.

Le libellé est recopié pour la même raison : « zone Cocody » reste lisible même
si la zone a depuis été renommée ou retirée.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import User
from common.models import AuditEntry

__all__ = ["AuditAction", "AuditEntry", "record_change"]


class AuditAction:
    """Vocabulaire fermé des changements journalisés.

    Des constantes plutôt qu'un `TextChoices` : ce ne sont pas des états d'un
    objet, et le champ reste une chaîne libre en base pour qu'une action ajoutée
    demain n'impose pas de migration. Le registre existe pour que les appelants
    partagent les mêmes clés, pas pour que la base les valide.
    """

    RESTAURANT_CREATE = "restaurant.create"
    VARIANT_CREATE = "variant.create"
    VARIANT_UPDATE = "variant.update"
    VARIANT_ACTIVATION = "variant.activation"
    VARIANT_DELETE = "variant.delete"
    CATEGORY_IMAGE = "category.image"
    RESTAURANT_STATUS = "restaurant.status"
    RESTAURANT_LOCATION = "restaurant.location"
    RESTAURANT_ZONE = "restaurant.zone"
    ZONE_CREATE = "zone.create"
    ZONE_BOUNDARY = "zone.boundary"
    ZONE_TARIFF = "zone.tariff"
    ZONE_ACTIVATION = "zone.activation"
    ZONE_DELETE = "zone.delete"
    COUNTRY_ACTIVATION = "country.activation"

    # Les droits — qui peut faire quoi, et sur quoi. Même propriété que la
    # géographie : silencieux et coûteux. Une permission `orders.refund`
    # ajoutée à un rôle ne se voit dans aucun écran, et se découvre dans les
    # remboursements qu'elle a permis.
    ROLE_PERMISSIONS = "role.permissions"
    STAFF_ROLES = "staff.roles"
    STAFF_SCOPE = "staff.scope"
    STAFF_ACTIVATION = "staff.activation"
    #: Le mot de passe lui-même n'est **jamais** consigné — seulement le fait
    #: qu'un tiers l'a remplacé, ce qui est une reprise de compte possible.
    STAFF_PASSWORD = "staff.password"  # noqa: S105 — un nom d'action, pas un secret
    #: Le motif était exigé par la route de blocage, puis jeté : il n'était
    #: écrit nulle part. C'est ici qu'il vit désormais.
    CUSTOMER_BLOCK = "customer.block"
    #: Un avis client masqué ou réaffiché — ce que dit la clientèle ne sort pas
    #: de la vitrine sans que l'on sache qui l'a décidé, et pourquoi.
    REVIEW_VISIBILITY = "review.visibility"

    # L'argent qui **sort**, et les décisions qui engagent l'enseigne envers un
    # client. Ce sont des changements d'état, et le préambule dit qu'on ne les
    # duplique pas ici — mais ceux-là n'ont aucune autre lecture chronologique :
    # leur trace vit sur leur propre ligne, ce qui répond à « où en est ce
    # retrait ? » et jamais à « qu'a signé cet opérateur cette semaine ? ». Or
    # c'est la seconde question qu'on pose quand un versement manque, et c'est
    # celle que le cahier des charges veut pouvoir poser.
    #
    # La granularité est la **décision**, pas le geste : une réponse à un ticket
    # n'entre pas au journal — le fil du ticket la porte déjà, mot pour mot.
    PAYOUT_SETTLE = "payout.settle"
    PAYOUT_REJECT = "payout.reject"
    REFUND_REQUEST = "refund.request"
    REFUND_SETTLE = "refund.settle"
    #: Un remboursement abandonné. Sans lui, la ligne restait « en attente »
    #: pour toujours **et** consommait le plafond du remboursable (P3).
    REFUND_CANCEL = "refund.cancel"
    COMPLAINT_DECISION = "complaint.decision"
    RETURN_DECISION = "return.decision"
    TICKET_RESOLUTION = "ticket.resolution"

    #: Valider, refuser, suspendre ou rouvrir un dossier livreur. Le dossier ne
    #: garde que la **dernière** décision (`verified_by`, `verified_at`) : sans
    #: le journal, « qui a suspendu ce livreur samedi, et pourquoi ? » n'avait
    #: plus de réponse dès la décision suivante.
    COURIER_VERIFICATION = "courier.verification"


def record_change(
    *,
    actor: User | None,
    action: str,
    target_type: str,
    target_id: Any,
    target_label: str,
    before: dict[str, Any],
    after: dict[str, Any],
    scope_restaurant_id: Any = None,
) -> AuditEntry | None:
    """Consigne un changement — **et rien quand il n'y en a pas**.

    Rend `None` si `before == after`. Un formulaire de back-office renvoie tous
    ses champs à chaque enregistrement, y compris ceux qu'on n'a pas touchés :
    sans cette garde, corriger un numéro de téléphone écrirait une ligne
    « position inchangée », et le journal se remplirait de bruit jusqu'à ce que
    plus personne ne l'ouvre.

    L'acteur peut être nul : une commande de peuplement ou une correction en
    `shell` n'en ont pas, et refuser de journaliser dans ces cas-là ferait
    perdre précisément les changements qu'on cherche le plus souvent.

    `scope_restaurant_id` rattache l'entrée à un établissement quand la
    décision en a un — c'est ce qui la rend lisible par le compte cloisonné qui
    l'a prise. Voir `AuditEntry.scope_restaurant_id`.
    """
    if before == after:
        return None

    return AuditEntry.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=target_label,
        before=before,
        after=after,
        scope_restaurant_id=scope_restaurant_id,
    )
