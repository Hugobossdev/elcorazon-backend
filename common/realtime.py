"""Diffusion temps réel — ADR-008.

Un WebSocket ne garantit rien. Une coupure réseau de dix secondes — un tunnel,
un ascenseur, un changement de cellule — et le client a manqué des événements
qu'aucune reconnexion ne lui rendra. La carte reste figée, et elle ne se répare
jamais toute seule.

D'où le numéro de séquence : chaque groupe compte ses événements, chaque
message le porte, et un client qui se reconnecte demande la suite avec
`?since=<seq>`. Le journal est **borné** — les derniers événements seulement,
et pour quelques minutes : au-delà, une reconnexion tardive doit recharger
l'état par HTTP plutôt que rejouer une heure d'historique.

Ce module ne dépend d'aucune app métier. Il expose ce que les services
appellent (`publish`) et ce que les consommateurs appellent (`replay`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache

__all__ = [
    "BACKLOG_SIZE",
    "BACKLOG_TTL_SECONDS",
    "Event",
    "courier_group",
    "group_cart_group",
    "order_chat_group",
    "order_group",
    "publish",
    "replay",
    "restaurant_group",
]

#: Nombre d'événements conservés par groupe pour le rattrapage.
#:
#: Cinquante couvre plusieurs minutes de suivi à un relevé toutes les dix
#: secondes. Au-delà, le client a été absent assez longtemps pour que recharger
#: l'état complet par HTTP soit à la fois plus simple et plus juste.
BACKLOG_SIZE = 50

#: Durée de vie du journal. Une commande livrée n'a plus de rattrapage à offrir.
BACKLOG_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class Event:
    """Message diffusé, tel que le client le reçoit.

    `seq` est croissant **par groupe** et jamais réutilisé : c'est le seul
    élément sur lequel un client peut raisonner pour savoir ce qu'il a manqué.
    """

    seq: int
    type: str
    payload: dict[str, Any]


def order_group(order_id: object) -> str:
    """Suivi d'une commande : son client, son livreur, le personnel."""
    return f"order.{order_id}.tracking"


def order_chat_group(order_id: object) -> str:
    """Chat d'une commande : son client et son livreur, personne d'autre (ADR-008)."""
    return f"order.{order_id}.chat"


def group_cart_group(group_cart_id: object) -> str:
    """Panier collaboratif : ses participants, personne d'autre.

    C'est le groupe le plus « conversationnel » du projet — plusieurs personnes
    modifient la même chose en même temps, et chacune doit voir arriver les plats
    des autres. Sans diffusion, il faudrait interroger l'API en boucle pendant
    toute la durée de la commande groupée.
    """
    return f"groupcart.{group_cart_id}"


def user_group(user_id: object) -> str:
    """File personnelle d'un compte : ce qui doit le joindre où qu'il soit.

    Distincte des groupes par ressource (commande, panier, course) : un appel
    entrant doit faire sonner le destinataire même s'il n'a aucun écran ouvert
    sur la commande concernée. Un canal par commande ne peut pas le garantir —
    il faudrait que le destinataire ait deviné laquelle écouter.
    """
    return f"user.{user_id}"


def courier_group(courier_id: object) -> str:
    """File d'un livreur : les courses qu'on lui propose."""
    return f"courier.{courier_id}"


def restaurant_group(restaurant_id: object) -> str:
    """Tableau de bord d'un établissement."""
    return f"restaurant.{restaurant_id}"


def _counter_key(group: str) -> str:
    return f"realtime:{group}:seq"


def _event_key(group: str, seq: int) -> str:
    """Une clé **par événement**, et non une liste sous une clé unique.

    Une liste se relit puis se réécrit, et deux publications concurrentes sur
    le même groupe s'écrasent : la seconde repart de la liste que la première
    n'a pas encore enregistrée, et un événement disparaît du journal. Le client
    verrait alors un trou de numérotation qu'aucun rejeu ne peut combler.

    Une clé par numéro supprime le problème plutôt que de le rendre improbable :
    les écritures ne se touchent pas. Le numéro venant d'un incrément atomique,
    deux publications ne peuvent pas viser la même clé.
    """
    return f"realtime:{group}:event:{seq}"


def publish(group: str, event_type: str, payload: dict[str, Any]) -> Event:
    """Numérote, journalise et diffuse un événement.

    L'ordre compte : le numéro est attribué **avant** la diffusion, si bien
    qu'un client qui reçoit `seq=7` sait que `seq=6` existe et peut le
    réclamer. Diffuser d'abord et numéroter ensuite laisserait passer des
    messages dans le désordre sous charge.

    Les échecs de diffusion ne remontent pas : un événement temps réel manqué
    se rattrape par `?since=`, alors qu'une exception ici ferait échouer la
    transaction métier qui l'a déclenché. Perdre une commande parce que Redis
    a hoqueté serait le pire des deux mondes.
    """
    seq = _next_seq(group)
    event = Event(seq=seq, type=event_type, payload=payload)

    cache.set(_event_key(group, seq), asdict(event), timeout=BACKLOG_TTL_SECONDS)

    layer = get_channel_layer()
    if layer is not None:
        # Le `type` du message enveloppe est celui que Channels traduit en nom
        # de méthode — il ne doit **pas** être celui de l'événement métier,
        # sinon la couche cherche un `tracking_position` qui n'existe pas. Le
        # type métier voyage donc sous `event`.
        async_to_sync(layer.group_send)(
            group,
            {
                "type": "realtime.event",
                "seq": event.seq,
                "event": event.type,
                "payload": event.payload,
            },
        )

    return event


def _next_seq(group: str) -> int:
    """Incrément atomique du compteur du groupe.

    `cache.incr` lève si la clé n'existe pas ; `add` la crée sans écraser une
    valeur concurrente. Les deux ensemble donnent un incrément sûr sans verrou
    — deux processus qui publient en même temps obtiennent deux numéros
    différents.
    """
    key = _counter_key(group)
    cache.add(key, 0, timeout=BACKLOG_TTL_SECONDS)
    try:
        value: int = cache.incr(key)
    except ValueError:  # pragma: no cover - la clé a expiré entre `add` et `incr`
        cache.set(key, 1, timeout=BACKLOG_TTL_SECONDS)
        value = 1
    return value


def replay(group: str, since: int) -> list[Event]:
    """Événements postérieurs à `since`, dans l'ordre.

    La fenêtre est bornée par `BACKLOG_SIZE` même si le client demande depuis
    le début : au-delà, il a été absent assez longtemps pour que recharger
    l'état par HTTP soit plus simple et plus juste que rejouer une heure
    d'historique.

    Renvoie une liste vide si le client est à jour, et **aussi** si son retard
    dépasse la fenêtre. Le second cas n'est pas silencieux pour autant : le
    consommateur compare le premier numéro rendu à celui demandé et signale au
    client qu'il doit recharger, plutôt que de le laisser croire qu'il n'a rien
    manqué.
    """
    dernier: int = cache.get(_counter_key(group), 0)
    premier = max(since + 1, dernier - BACKLOG_SIZE + 1, 1)
    if premier > dernier:
        return []

    # Un seul aller-retour vers le cache pour toute la fenêtre : la lire
    # numéro par numéro ferait cinquante allers-retours Redis à chaque
    # reconnexion, c'est-à-dire au pire moment — quand le réseau vient
    # justement de se rétablir.
    attendues = [_event_key(group, seq) for seq in range(premier, dernier + 1)]
    trouvees: dict[str, dict[str, Any]] = cache.get_many(attendues)

    return [Event(**trouvees[cle]) for cle in attendues if cle in trouvees]
