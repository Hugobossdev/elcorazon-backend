"""Sortie vers les prestataires d'encaissement.

Le backend a besoin de trois choses d'un prestataire : ouvrir une demande de
paiement, **authentifier** les notifications qu'il renvoie, et les **lire**. Les
deux dernières sont dans le port et non dans la vue, et c'est ce que la
première rédaction avait manqué : elle supposait un unique schéma HMAC-SHA256
sur le corps brut, valable pour le bac à sable et pour personne d'autre.

PayDunya n'authentifie pas ainsi — il joint à sa notification l'empreinte
SHA-512 de la clé maîtresse — et son corps est un formulaire imbriqué, pas le
JSON plat qu'attendait le service. Un prestataire de plus, et c'était un `if`
de plus dans la vue. L'authentification et la lecture appartiennent donc à
chaque connecteur.

Ce qui **décide** — gardes C5, idempotence P1, transitions, plafond P3 — reste
en dehors : c'est ce qui permet de tout vérifier sans compte marchand.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from apps.payments.models import PaymentStatus, Transaction

__all__ = [
    "CheckoutInstruction",
    "GatewayError",
    "Notification",
    "PaymentGateway",
    "SandboxGateway",
    "gateway_for",
]


class GatewayError(Exception):
    """Le prestataire n'a pas pu ouvrir la demande de paiement.

    Distincte d'une erreur métier : rien n'est reproché au client, c'est le
    service externe qui n'a pas répondu ou a refusé. La vue la traduit en 502 —
    « le problème est en amont » — plutôt qu'en 400, qui ferait chercher au
    client une faute dans sa requête.
    """


@dataclass(frozen=True, slots=True)
class CheckoutInstruction:
    """Ce que le client doit faire pour payer.

    `provider_reference` est la clé de rapprochement : c'est elle que la
    notification citera, et son unicité en base empêche d'enregistrer deux fois
    le même encaissement.
    """

    provider_reference: str
    checkout_url: str
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class Notification:
    """Notification normalisée, extraite du format propre au prestataire.

    `event_id` porte l'idempotence (P1). Il doit distinguer deux notifications
    successives sur **la même** transaction — un prestataire annonce souvent
    « en cours » puis « encaissé » — tout en rendant identiques deux envois du
    même événement. D'où la combinaison de la référence et du statut, plutôt
    que la référence seule : celle-ci ferait ignorer l'encaissement au motif
    qu'on a déjà vu passer l'attente.
    """

    event_id: str
    provider_reference: str
    status: str
    reason: str = ""


class PaymentGateway(Protocol):
    def open_checkout(self, transaction: Transaction) -> CheckoutInstruction: ...

    def authenticate(
        self, *, raw_body: bytes, headers: Mapping[str, str], data: Mapping[str, Any]
    ) -> bool: ...

    def parse(self, data: Mapping[str, Any]) -> Notification: ...


class SandboxGateway:
    """Prestataire de bac à sable — aucun appel réseau.

    Il produit une référence déterministe et une URL locale, et authentifie ses
    notifications par HMAC-SHA256 sur le corps brut. Le paiement se confirme
    donc exactement comme en production : par une notification signée, seule
    source de vérité de l'encaissement.
    """

    def open_checkout(self, transaction: Transaction) -> CheckoutInstruction:
        """Référence déterministe, dérivée de la clé primaire **entière**.

        Les 16 premiers caractères hexadécimaux ne suffisaient pas, et le
        raccourci produisait des doublons sur
        `payments_transaction_provider_reference_key`. Un UUIDv7 (ADR-007) n'est
        aléatoire que sur sa fin : ses 48 premiers bits sont l'horodatage en
        millisecondes et les 4 suivants le numéro de version, constant. Sur 64
        bits tronqués il ne restait donc que **12 bits** pour départager deux
        transactions ouvertes dans la même milliseconde — 4 096 valeurs, soit
        une collision plus probable qu'improbable dès la soixante-quinzième
        (paradoxe des anniversaires). Un import de commandes, une rafale de
        parts de paiement partagé ou une suite de tests un peu rapide suffisent
        à l'atteindre.
        """
        return CheckoutInstruction(
            provider_reference=f"SBX-{transaction.pk.hex.upper()}",
            checkout_url=f"{settings.SANDBOX_CHECKOUT_BASE_URL}/{transaction.pk}",
            instructions="Bac à sable : confirmez par une notification signée.",
        )

    def authenticate(
        self, *, raw_body: bytes, headers: Mapping[str, str], data: Mapping[str, Any]
    ) -> bool:
        """Vérifie la signature HMAC-SHA256 du corps brut.

        Sur le corps **brut** et non sur le JSON reparsé : deux sérialisations
        du même objet diffèrent par l'ordre des clés et les espaces, et la
        signature ne tomberait juste que par chance.

        `compare_digest` et non `==` : la comparaison naïve s'arrête au premier
        octet différent, et le temps qu'elle met révèle combien de caractères
        sont justes — de quoi reconstituer une signature valide en quelques
        milliers de requêtes.
        """
        expected = hmac.new(
            settings.PAYMENT_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, headers.get("X-Signature", ""))

    def parse(self, data: Mapping[str, Any]) -> Notification:
        reference = str(data.get("provider_reference", "")).strip()
        status = str(data.get("status", "")).strip()
        return Notification(
            event_id=str(data.get("event_id", "")).strip(),
            provider_reference=reference,
            status=status,
            reason=str(data.get("reason", "")),
        )


def gateway_for(provider: str) -> PaymentGateway:
    """Connecteur d'un prestataire, résolu au moment de l'appel.

    Un registre par prestataire et non un connecteur unique : les espèces, le
    portefeuille et PayDunya coexistent sur la même plateforme, et une commande
    payée à la livraison n'a rien à demander à un service en ligne.

    Résolu à l'appel plutôt qu'à l'import : les tests le remplacent par un
    réglage, et un déploiement change de prestataire sans déploiement de code.
    """
    chemins: Mapping[str, str] = settings.PAYMENT_GATEWAYS
    if provider not in chemins:
        raise GatewayError(f"Aucun connecteur configuré pour {provider!r}.")

    resolved: PaymentGateway = import_string(chemins[provider])()
    return resolved


#: Correspondance des statuts d'un prestataire vers les nôtres.
#:
#: Partagée parce qu'elle pose une question que chaque connecteur se repose :
#: un paiement **abandonné** par le client arrive après que la demande est
#: passée « en cours », et la machine à états n'autorise pas
#: `processing → cancelled` — seulement `pending → cancelled`, pour une demande
#: annulée avant d'avoir commencé. L'abandon est donc traduit en échec, avec un
#: motif qui dit ce qui s'est réellement passé. Élargir la machine serait
#: l'autre option ; elle demanderait de rouvrir une contrainte de base pour un
#: cas que le motif documente aussi bien.
ABANDONED = PaymentStatus.FAILED
