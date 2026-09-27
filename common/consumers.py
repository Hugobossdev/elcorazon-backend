"""Socle des consommateurs WebSocket — ADR-008.

**L'autorisation est faite avant l'acceptation du socket.** C'est le point où
l'implémentation précédente échouait : un abonnement Supabase donnait accès à
des *lignes*, pas à un périmètre métier, et un livreur pouvait publier des
positions sur la course d'un autre (L3).

Ici, `connect()` valide le jeton, puis demande à la sous-classe le groupe
auquel l'appelant a droit. Un socket accepté est un socket dont le périmètre a
déjà été vérifié — il n'existe pas d'état « connecté mais pas encore
autorisé » pendant lequel un message pourrait passer.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from apps.accounts.models import User
from common.realtime import replay

__all__ = ["CLOSE_FORBIDDEN", "CLOSE_STALE", "CLOSE_UNAUTHENTICATED", "AuthorizedConsumer"]

#: Codes de fermeture applicatifs (plage 4000-4999 réservée à l'application).
#:
#: Un socket refusé est **fermé** avec un code explicite, jamais laissé ouvert
#: en lecture seule : le client doit pouvoir distinguer « rejeton ton jeton »
#: de « tu n'as rien à faire ici » sans lire un message d'erreur.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_STALE = 4409


class AuthorizedConsumer(AsyncJsonWebsocketConsumer):
    """Consommateur dont la connexion est conditionnée à un droit métier.

    Les sous-classes implémentent `authorized_group()` : elles renvoient le nom
    du groupe si l'appelant y a droit, `None` sinon. Rien d'autre n'est à
    écrire pour être sûr — c'est le seul chemin d'acceptation.
    """

    user: User
    group: str

    async def connect(self) -> None:
        authenticated = await self._authenticate()
        if authenticated is None:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        self.user = authenticated
        group = await self.authorized_group()
        if group is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return

        self.group = group
        await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()
        await self._catch_up()

    async def disconnect(self, code: int) -> None:
        group = getattr(self, "group", None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def authorized_group(self) -> str | None:  # pragma: no cover - abstraite
        raise NotImplementedError

    # ------------------------------------------------------ authentification

    async def _authenticate(self) -> User | None:
        """Valide le JWT porté par la connexion.

        Deux emplacements acceptés, dans cet ordre : l'en-tête `Authorization`,
        et à défaut le paramètre `?token=`. Le premier est le bon — un jeton en
        chaîne de requête se retrouve dans les journaux d'accès et l'historique
        du navigateur. Le second existe parce que l'API WebSocket des
        navigateurs ne permet pas de poser d'en-tête ; les clients natifs, eux,
        n'ont aucune raison de s'en servir.
        """
        raw = self._token_from_header() or self._token_from_query()
        if not raw:
            return None

        return await self._user_for(raw)

    def _token_from_header(self) -> str:
        for name, value in self.scope.get("headers", []):
            if name == b"authorization":
                header = value.decode()
                if header.lower().startswith("bearer "):
                    return header[7:].strip()
        return ""

    def _token_from_query(self) -> str:
        query = parse_qs(self.scope.get("query_string", b"").decode())
        return query.get("token", [""])[0]

    @database_sync_to_async
    def _user_for(self, raw: str) -> User | None:
        """Décode le jeton et charge le compte.

        La révocation est vérifiée par simple-jwt lui-même : un jeton en liste
        noire lève ici. C'est ce qui fait qu'un changement de mot de passe (T2)
        ferme aussi les sockets — au prochain établissement de connexion, du
        moins ; les sockets déjà ouverts sont coupés par le redémarrage ou par
        l'expiration du jeton d'accès, qui dure quinze minutes.
        """
        backend = JWTAuthentication()
        try:
            validated = backend.get_validated_token(raw.encode())
            user = backend.get_user(validated)
        except (InvalidToken, TokenError, KeyError):
            return None

        return user if isinstance(user, User) and user.is_active else None

    # ------------------------------------------------------------ rattrapage

    async def _catch_up(self) -> None:
        """Rejoue ce que le client a manqué, s'il dit d'où il repart.

        Sans `?since=`, on ne rejoue rien : un client qui se connecte pour la
        première fois n'a pas d'historique à rattraper, et lui déverser les
        cinquante derniers événements lui ferait afficher un trajet passé.
        """
        since = self._since()
        if since is None:
            return

        missed = await database_sync_to_async(replay)(self.group, since)
        if not missed:
            return

        # Un trou entre ce que le client demande et ce que le journal contient
        # encore signifie qu'il a été absent trop longtemps. Le lui dire est
        # tout l'intérêt de la numérotation : sans ce signal, il croirait avoir
        # tout reçu et afficherait un état incomplet indéfiniment.
        if missed[0].seq > since + 1:
            await self.send_json(
                {"type": "realtime.gap", "from_seq": since, "next_seq": missed[0].seq}
            )

        for event in missed:
            await self.send_json({"seq": event.seq, "type": event.type, **event.payload})

    def _since(self) -> int | None:
        query = parse_qs(self.scope.get("query_string", b"").decode())
        raw = query.get("since", [""])[0]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------- diffusion

    async def realtime_event(self, message: dict[str, Any]) -> None:
        """Relaie un événement du groupe vers ce client.

        Le nom de la méthode est imposé par Channels : il est dérivé du `type`
        du message, les points devenant des soulignés.
        """
        await self.send_json(
            {"seq": message["seq"], "type": message["event"], **message["payload"]}
        )
