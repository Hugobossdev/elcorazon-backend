"""Connecteur Firebase Cloud Messaging — API HTTP v1.

**Confronté au service réel le 5 août 2026** (projet `elcorazon-9595`) : le
compte de service s'authentifie, l'API accepte les envois, et les codes de
refus observés sont bien ceux que `ERREURS_DEFINITIVES` classe. Voir
`docs/firebase.md` §5 pour le détail de ce qui a été exercé — et de ce qui
demande encore un appareil physique.

Trois choses distinguent cette intégration d'un simple POST :

* **l'authentification est un jeton OAuth**, pas une clé d'API. L'ancienne clé
  serveur a été retirée par Google ; il faut désormais signer une assertion
  avec un compte de service, et le jeton obtenu expire. `google-auth` s'en
  charge — c'est exactement le genre de code qu'on n'écrit pas soi-même ;
* **l'envoi est unitaire.** L'API v1 n'a pas de diffusion groupée : c'est une
  requête par appareil. Un client à cinq téléphones coûte cinq appels, et c'est
  pourquoi tout cela vit dans une tâche Celery et jamais dans le cycle de
  requête ;
* **la réponse d'erreur porte la décision.** Distinguer l'appareil
  définitivement injoignable de la panne passagère est toute la raison d'être
  de ce module : confondre les deux donne soit une purge d'appareils sains au
  premier hoquet, soit la boucle infinie de l'implémentation précédente.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from django.conf import settings
from google.auth import transport as google_transport

from apps.notifications.push import PushMessage, PushResult

__all__ = ["FirebaseCloudMessagingBackend"]

logger = logging.getLogger(__name__)

#: Portée OAuth exigée par l'API d'envoi.
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

#: Codes signalant un appareil **définitivement** injoignable.
#:
#: `UNREGISTERED` — l'application a été désinstallée ou le jeton régénéré.
#: `INVALID_ARGUMENT` — le jeton ne correspond à rien de valide.
#: `SENDER_ID_MISMATCH` — le jeton appartient à un autre projet Firebase ; il
#: ne fonctionnera jamais depuis celui-ci, et le garder ferait réessayer
#: éternellement une erreur de configuration.
ERREURS_DEFINITIVES = frozenset({"UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH"})


class _ReponseOAuth(google_transport.Response):
    """Réponse HTTP telle que `google-auth` s'attend à la lire."""

    def __init__(self, reponse: httpx.Response) -> None:
        self._reponse = reponse

    @property
    def status(self) -> int:
        return self._reponse.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._reponse.headers

    @property
    def data(self) -> bytes:
        return self._reponse.content


class _TransportOAuth(google_transport.Request):
    """Transport de `google-auth`, bâti sur **httpx**.

    Sans lui, `credentials.refresh()` importe `google.auth.transport.requests`,
    qui exige le paquet `requests` — absent des dépendances, et absent à
    dessein : le projet a choisi `httpx` pour son transport simulable
    (`MockTransport`), qui permet de tester un connecteur hors réseau.

    Ce n'était pas une question de goût. L'import manquant levait une
    `ImportError` à chaque rafraîchissement de jeton, donc **avant tout envoi**.
    `send()` l'attrape, journalise `fcm.authentification` et rend tous les
    appareils en échec passager : le push ne partait jamais, sans qu'aucune
    erreur ne remonte à l'appelant. Le défaut a échappé aux tests parce qu'ils
    court-circuitaient `_authorization` — la seule ligne qui échouait.

    Le point d'extension est documenté par `google-auth` : n'importe quel
    appelable respectant cette signature fait l'affaire.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> google_transport.Response:
        with httpx.Client(timeout=timeout or settings.FCM_TIMEOUT_SECONDS) as client:
            reponse = client.request(
                method,
                url,
                content=body,
                headers=dict(headers) if headers else None,
            )
        return _ReponseOAuth(reponse)


class FirebaseCloudMessagingBackend:
    """Envoi par l'API HTTP v1 de FCM."""

    def send(self, tokens: list[str], message: PushMessage) -> PushResult:
        """Adresse le message à chaque appareil, un appel par jeton.

        Un échec sur un appareil n'interrompt pas les autres : les trois listes
        rendues sont indépendantes, et c'est ce qui permet à la tâche de
        supprimer les morts, de reprendre les indécis et de laisser tranquilles
        ceux qui ont reçu.
        """
        livres: list[str] = []
        morts: list[str] = []
        rates: list[str] = []

        try:
            entete = self._authorization()
        except Exception as exc:
            # Sans jeton, aucun envoi ne peut aboutir. Tous les appareils sont
            # donc « en échec passager » et non « morts » : c'est notre
            # configuration qui est en cause, pas leurs jetons, et les purger
            # serait la pire réaction possible.
            logger.error("fcm.authentification", extra={"detail": str(exc)})
            return PushResult(failed=tuple(tokens))

        url = f"https://fcm.googleapis.com/v1/projects/{settings.FCM_PROJECT_ID}/messages:send"
        with httpx.Client(timeout=settings.FCM_TIMEOUT_SECONDS) as client:
            for token in tokens:
                issue = self._send_one(client, url, entete, token, message)
                {"delivered": livres, "unregistered": morts, "failed": rates}[issue].append(token)

        return PushResult(delivered=tuple(livres), unregistered=tuple(morts), failed=tuple(rates))

    # ------------------------------------------------------ authentification

    def _authorization(self) -> dict[str, str]:
        """Jeton d'accès OAuth, rafraîchi si nécessaire.

        L'objet d'identifiants est mis en cache au niveau du module : il
        conserve le jeton et sa date d'expiration, et le redemande seul quand
        il expire. Le recréer à chaque envoi provoquerait un aller-retour
        OAuth par notification, soit plusieurs centaines de millisecondes pour
        rien.
        """
        credentials = _credentials()
        if not credentials.valid:
            credentials.refresh(_TransportOAuth())

        return {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------- envoi

    def _send_one(
        self,
        client: httpx.Client,
        url: str,
        entete: dict[str, str],
        token: str,
        message: PushMessage,
    ) -> str:
        try:
            response = client.post(url, json=self._payload(token, message), headers=entete)
        except httpx.HTTPError as exc:
            logger.warning("fcm.reseau", extra={"detail": str(exc)})
            return "failed"

        if response.status_code == httpx.codes.OK:
            return "delivered"

        code, definitif = self._cause(response)
        # Le seul endroit où le refus de Google est lisible. Sans cette ligne,
        # une erreur de configuration — mauvais `FCM_PROJECT_ID`, compte de
        # service sans droit d'envoi — se présente comme une suite d'échecs
        # muets, indiscernable d'une panne réseau. C'est aussi ce qu'on compare
        # à `ERREURS_DEFINITIVES` lors de la validation d'avant mise en service.
        logger.warning(
            "fcm.rejet",
            extra={
                "status": response.status_code,
                "code": code,
                "definitif": definitif,
                # Les huit derniers caractères suffisent à reconnaître un
                # appareil dans une rafale ; le jeton entier n'a rien à faire
                # dans un journal qui part chez un collecteur.
                "device": token[-8:],
            },
        )
        return "unregistered" if definitif else "failed"

    @staticmethod
    def _payload(token: str, message: PushMessage) -> dict[str, Any]:
        """Corps d'un envoi.

        `data` ne contient que des chaînes — l'API refuse tout autre type, et
        la conversion a déjà été faite en amont. La priorité haute sur Android
        et le réveil sur iOS ne sont pas décoratifs : sans eux, une
        notification de commande arrive quand le système le décide, c'est-à-dire
        parfois après la livraison.
        """
        return {
            "message": {
                "token": token,
                "notification": {"title": message.title, "body": message.body},
                "data": message.data,
                "android": {"priority": "high"},
                "apns": {"headers": {"apns-priority": "10"}},
            }
        }

    @staticmethod
    def _cause(response: httpx.Response) -> tuple[str, bool]:
        """Code d'erreur porté par la réponse, et son caractère définitif.

        La décision se lit dans `error.details[].errorCode` et **pas** dans le
        statut HTTP : un 400 peut signaler un jeton mort comme une charge utile
        mal formée, et purger sur le second effacerait des appareils sains à
        cause d'un défaut de notre côté.

        Le code est rendu même quand il ne conclut à rien : c'est ce qui
        distingue, dans le journal, un quota dépassé d'un projet mal configuré,
        alors que les deux mènent à la même reprise.
        """
        try:
            corps: dict[str, Any] = response.json()
        except ValueError:
            return "", False

        erreur = corps.get("error", {})
        codes = [
            str(detail["errorCode"])
            for detail in erreur.get("details", [])
            if isinstance(detail, dict) and detail.get("errorCode")
        ]
        for code in codes:
            if code in ERREURS_DEFINITIVES:
                return code, True
        if codes:
            return codes[0], False

        # Certaines réponses ne portent que le statut canonique. `NOT_FOUND` y
        # désigne un jeton qui n'existe plus ; le reste est passager — y
        # compris un `INVALID_ARGUMENT` à ce niveau, qui parle de la requête et
        # non du jeton.
        statut = str(erreur.get("status", ""))
        return statut, statut == "NOT_FOUND"


_cache: dict[str, Any] = {}


def _credentials() -> Any:
    """Identifiants du compte de service, chargés une seule fois.

    Le fichier est monté en volume — comme les clés JWT — et non passé en
    variable : c'est un JSON multiligne, et c'est la forme qu'attendent les
    `Secret` Kubernetes.
    """
    chemin = settings.FCM_CREDENTIALS_PATH
    if not chemin:
        raise RuntimeError(
            "FCM_CREDENTIALS_PATH n'est pas renseigné : aucune notification ne peut partir."
        )

    if chemin not in _cache:
        from google.oauth2 import service_account

        # `google-auth` publie des annotations partielles : cette fabrique n'en
        # a pas, et le mode strict refuse d'appeler une fonction non typée.
        _cache[chemin] = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            chemin, scopes=[SCOPE]
        )

    return _cache[chemin]
