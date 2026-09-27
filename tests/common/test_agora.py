"""Jetons RTC Agora — format AccessToken2.

L'enjeu n'est pas la couverture : c'est que le jeton **borne** ce qu'il
autorise. Un jeton sans expiration, ou valable sur un autre canal, rend inutile
le fait d'avoir sorti le certificat de l'app.
"""

from __future__ import annotations

import base64
import zlib

import pytest

from common.agora import AgoraTokenError, RtcRole, build_rtc_token

APP_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CERTIFICATE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def token(**overrides: object) -> str:
    params: dict[str, object] = {
        "app_id": APP_ID,
        "app_certificate": CERTIFICATE,
        "channel_name": "order-1",
        "uid": 42,
        "issued_at": 1_800_000_000,
        "salt": 12345,
    }
    params.update(overrides)
    return build_rtc_token(**params)  # type: ignore[arg-type]


def payload(value: str) -> bytes:
    """Corps décompressé du jeton — de quoi vérifier ce qu'il contient."""
    return zlib.decompress(base64.b64decode(value[3:]))


class TestForme:
    def test_prefixe_de_version(self) -> None:
        assert token().startswith("007")

    def test_porte_le_canal_et_l_app(self) -> None:
        body = payload(token())

        assert APP_ID.encode() in body
        assert b"order-1" in body

    def test_ne_porte_jamais_le_certificat(self) -> None:
        """Le certificat signe, il ne voyage pas."""
        assert CERTIFICATE.encode() not in payload(token())

    def test_reproductible_a_entrees_egales(self) -> None:
        """Sans quoi le test précédent ne prouverait rien de stable."""
        assert token() == token()


class TestBornes:
    def test_deux_jetons_successifs_different(self) -> None:
        """L'aléa de signature est tiré à chaque appel : deux jetons pour le
        même canal ne doivent pas être interchangeables."""
        assert build_rtc_token(
            app_id=APP_ID, app_certificate=CERTIFICATE, channel_name="order-1", uid=42
        ) != build_rtc_token(
            app_id=APP_ID, app_certificate=CERTIFICATE, channel_name="order-1", uid=42
        )

    def test_un_canal_different_donne_un_jeton_different(self) -> None:
        assert token() != token(channel_name="order-2")

    def test_un_uid_different_donne_un_jeton_different(self) -> None:
        assert token() != token(uid=43)

    def test_une_expiration_differente_donne_un_jeton_different(self) -> None:
        assert token() != token(ttl_seconds=7200)

    def test_l_abonne_ne_recoit_pas_les_privileges_de_publication(self) -> None:
        """Un rôle d'écoute ne doit pas pouvoir émettre."""
        assert token(role=RtcRole.SUBSCRIBER) != token(role=RtcRole.PUBLISHER)


class TestConfiguration:
    def test_refuse_un_certificat_absent(self) -> None:
        with pytest.raises(AgoraTokenError):
            build_rtc_token(app_id=APP_ID, app_certificate="", channel_name="order-1", uid=1)

    def test_refuse_un_app_id_absent(self) -> None:
        with pytest.raises(AgoraTokenError):
            build_rtc_token(app_id="", app_certificate=CERTIFICATE, channel_name="order-1", uid=1)

    def test_refuse_une_validite_nulle(self) -> None:
        """Un jeton sans durée n'ouvre rien — autant le dire à la construction."""
        with pytest.raises(AgoraTokenError):
            token(ttl_seconds=0)
