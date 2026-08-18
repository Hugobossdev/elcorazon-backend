"""Sortie vers le service de notification push — ADR-008.

Le backend a besoin d'une chose d'un service push : livrer un message à une
liste de jetons, et **dire lesquels sont morts**. Cette seconde moitié est ce
que l'implémentation précédente ignorait : elle retentait trois fois un
appareil désinstallé, à chaque notification, indéfiniment. Un utilisateur parti
coûtait ainsi du quota et de la latence pour toujours.

D'où un port réduit à une méthode et un résultat qui **distingue l'échec
transitoire du définitif**. Le premier se retente, le second supprime le jeton.
Les confondre donne soit une purge d'appareils sains au premier hoquet réseau,
soit la boucle infinie d'origine.

`ConsolePushBackend` est l'implémentation fournie et celle des tests. Le
connecteur FCM s'ajoutera ici, sans toucher au service : il demande un compte
Firebase et un client HTTP, c'est-à-dire ce que ce découpage isole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

__all__ = [
    "ConsolePushBackend",
    "PushBackend",
    "PushDeliveryIncomplete",
    "PushMessage",
    "PushResult",
    "backend",
]


class PushDeliveryIncomplete(Exception):
    """Une partie des jetons n'a pas été servie, pour une raison passagère.

    Porte les jetons concernés, et **eux seuls** : c'est ce qui permet à la
    reprise de ne pas re-notifier ceux qui ont déjà reçu. Une reprise à
    l'aveugle sur toute la liste ferait vibrer deux fois le téléphone de
    quelqu'un dont l'envoi avait fonctionné.
    """

    def __init__(self, tokens: tuple[str, ...]) -> None:
        self.tokens = tokens
        super().__init__(f"{len(tokens)} appareil(s) non servi(s), reprise demandée.")


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    #: Charge utile **minimale** : de quoi ouvrir le bon écran, pas une copie
    #: de l'objet métier. Le client recharge, et reçoit l'état du moment plutôt
    #: qu'un instantané périmé pendant le trajet.
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PushResult:
    """Issue d'un envoi, par jeton.

    `unregistered` est la raison d'être de ce type : ce sont les appareils que
    le service déclare définitivement injoignables — application désinstallée,
    jeton régénéré. Ils doivent être supprimés, pas retentés.

    `failed` est l'inverse : une panne passagère, un quota, un délai dépassé.
    Ces jetons restent, et la tâche réessaiera.
    """

    delivered: tuple[str, ...] = ()
    unregistered: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class PushBackend(Protocol):
    def send(self, tokens: list[str], message: PushMessage) -> PushResult: ...


class ConsolePushBackend:
    """Envoi journalisé, sans appel réseau.

    Sert le développement et les tests. Tous les jetons sont réputés livrés :
    un bac à sable qui inventerait des échecs rendrait les tests dépendants du
    hasard sans rien vérifier de réel.
    """

    def send(self, tokens: list[str], message: PushMessage) -> PushResult:
        logger.info("push.console", extra={"tokens": len(tokens), "title": message.title})
        return PushResult(delivered=tuple(tokens))


def backend() -> PushBackend:
    """Service configuré, résolu au moment de l'appel.

    Résolu à l'appel et non à l'import, comme le prestataire de paiement : les
    tests le remplacent par un réglage, et un déploiement change de service
    sans déploiement de code.
    """
    resolved: PushBackend = import_string(settings.PUSH_BACKEND)()
    return resolved


def payload_for(kind: str, data: dict[str, Any]) -> dict[str, str]:
    """Normalise la charge utile.

    FCM n'accepte que des chaînes dans `data` : un entier ou un UUID envoyé tel
    quel est refusé par l'API, et l'erreur ne se voit qu'en production sur un
    type de notification qu'on n'a pas testé. La conversion est donc faite ici,
    une fois.
    """
    return {"kind": kind, **{key: str(value) for key, value in data.items()}}
