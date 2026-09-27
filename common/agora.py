"""Jetons RTC Agora — format AccessToken2 (« 007 »).

Le certificat d'application ne quitte **jamais** le serveur. L'app cliente ne
reçoit qu'un jeton borné : un canal, un identifiant d'utilisateur, une
expiration. L'implémentation précédente embarquait l'`AGORA_APP_CERTIFICATE`
dans le `.env` de l'app Flutter, c'est-à-dire dans un binaire distribué —
quiconque l'extrayait pouvait fabriquer ses propres jetons, rejoindre n'importe
quel canal et écouter n'importe quelle conversation.

Le format est documenté par Agora et se réduit à de l'empaquetage plus un
HMAC-SHA256 : pas de cryptographie à concevoir, seulement une sérialisation à
respecter au bit près. C'est la raison pour laquelle il est écrit ici plutôt
que tiré d'une dépendance — les paquets PyPI disponibles implémentent encore la
version 1 du format, abandonnée par Agora.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
import zlib
from hashlib import sha256

__all__ = ["AgoraTokenError", "RtcRole", "build_rtc_token"]

#: Préfixe de version du format AccessToken2.
VERSION = "007"

#: Identifiant du service RTC dans la table des services d'un jeton.
SERVICE_TYPE_RTC = 1

#: Privilèges RTC. `joinChannel` suffit à un participant ; les privilèges de
#: publication sont accordés séparément par Agora selon le rôle du canal.
PRIVILEGE_JOIN_CHANNEL = 1
PRIVILEGE_PUBLISH_AUDIO = 2
PRIVILEGE_PUBLISH_VIDEO = 3
PRIVILEGE_PUBLISH_DATA = 4


class AgoraTokenError(RuntimeError):
    """Configuration Agora absente ou invalide.

    Levée à la construction plutôt qu'au moment de l'appel côté client : un
    jeton fabriqué avec un certificat vide serait accepté par notre API et
    refusé par Agora, ce qui déplacerait le diagnostic à l'endroit le moins
    outillé — le téléphone de l'utilisateur.
    """


class RtcRole:
    """Rôle dans le canal.

    `PUBLISHER` émet et reçoit ; `SUBSCRIBER` ne fait qu'écouter. Un appel
    point à point n'utilise que le premier, mais le second existe pour un futur
    mode « écoute » (support qui suit un appel, par exemple) et évite d'avoir à
    rouvrir cette signature ce jour-là.
    """

    PUBLISHER = "publisher"
    SUBSCRIBER = "subscriber"


def _pack_uint16(value: int) -> bytes:
    return struct.pack("<H", value)


def _pack_uint32(value: int) -> bytes:
    return struct.pack("<I", value)


def _pack_string(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return _pack_uint16(len(raw)) + raw


def _pack_map_uint32(mapping: dict[int, int]) -> bytes:
    # L'ordre est significatif : Agora reconstruit la table telle quelle pour
    # vérifier la signature. Trier rend la sortie déterministe, donc le jeton
    # reproductible à entrées égales — ce qui rend le test possible.
    packed = _pack_uint16(len(mapping))
    for key in sorted(mapping):
        packed += _pack_uint16(key) + _pack_uint32(mapping[key])
    return packed


class _ServiceRtc:
    """Service RTC d'un jeton : le canal, l'utilisateur, ses privilèges."""

    def __init__(self, channel_name: str, uid: int) -> None:
        self.channel_name = channel_name
        # `uid = 0` signifie « n'importe quel utilisateur » chez Agora. On le
        # sérialise en chaîne vide, comme le fait la référence.
        self.uid = "" if uid == 0 else str(uid)
        self.privileges: dict[int, int] = {}

    def add_privilege(self, privilege: int, expires_at: int) -> None:
        self.privileges[privilege] = expires_at

    def pack(self) -> bytes:
        return (
            _pack_uint16(SERVICE_TYPE_RTC)
            + _pack_map_uint32(self.privileges)
            + _pack_string(self.channel_name)
            + _pack_string(self.uid)
        )


def build_rtc_token(
    *,
    app_id: str,
    app_certificate: str,
    channel_name: str,
    uid: int,
    role: str = RtcRole.PUBLISHER,
    ttl_seconds: int = 3600,
    issued_at: int | None = None,
    salt: int | None = None,
) -> str:
    """Fabrique un jeton RTC pour [channel_name] et [uid].

    [ttl_seconds] borne la validité : un appel dure quelques minutes, un jeton
    valable un mois ne borne rien. La valeur par défaut d'une heure couvre un
    appel qui s'éternise sans laisser un droit d'accès traîner.

    [issued_at] et [salt] n'existent que pour les tests : ils rendent la sortie
    reproductible. En production, l'heure courante et un aléa cryptographique
    sont utilisés — deux jetons pour le même canal ne doivent pas être égaux.
    """
    if not app_id or not app_certificate:
        raise AgoraTokenError(
            "AGORA_APP_ID et AGORA_APP_CERTIFICATE doivent être configurés côté serveur."
        )
    if ttl_seconds <= 0:
        raise AgoraTokenError("La durée de validité d'un jeton doit être strictement positive.")

    now = int(time.time()) if issued_at is None else issued_at
    expires_at = now + ttl_seconds

    service = _ServiceRtc(channel_name, uid)
    service.add_privilege(PRIVILEGE_JOIN_CHANNEL, expires_at)
    if role == RtcRole.PUBLISHER:
        service.add_privilege(PRIVILEGE_PUBLISH_AUDIO, expires_at)
        service.add_privilege(PRIVILEGE_PUBLISH_VIDEO, expires_at)
        service.add_privilege(PRIVILEGE_PUBLISH_DATA, expires_at)

    signing_salt = secrets.randbelow(0xFFFFFFFF) if salt is None else salt

    body = (
        _pack_string(app_id)
        + _pack_uint32(now)
        + _pack_uint32(expires_at)
        + _pack_uint32(signing_salt)
        + _pack_uint16(1)  # une seule entrée de service : RTC
        + service.pack()
    )

    signature = hmac.new(app_certificate.encode(), body, sha256).digest()
    packed = _pack_string(signature) + body

    return VERSION + base64.b64encode(zlib.compress(packed)).decode()
