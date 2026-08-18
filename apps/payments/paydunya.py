"""Connecteur PayDunya.

PayDunya est l'agrégateur Mobile Money du marché ouest-africain : il expose
Moov, Togocom, Orange et Wave derrière une facture unique, ce qui évite
d'intégrer quatre opérateurs.

**Avertissement.** Les noms de champs ci-dessous suivent le contrat documenté
par PayDunya, mais n'ont pas pu être confrontés à l'API réelle depuis ce poste :
aucun compte marchand n'y est configuré. Avant la mise en service, il faut
faire une facture en mode `test` et **comparer la réponse reçue** à
`_read_checkout`, ainsi qu'une notification réelle à `parse` et `_confirm`. Les
points à vérifier en priorité sont l'emplacement de l'URL de paiement dans la
réponse de création, la forme exacte du corps de l'IPN — un formulaire imbriqué
et non du JSON — et celle de la réponse de `checkout-invoice/confirm`.

Les gardes C5, l'idempotence P1 et les transitions restent en dehors de ce
module et vivent dans `services.py`. **Ce qui décide du statut, en revanche,
n'est plus la notification posée** : PayDunya n'y joint qu'une empreinte
constante de sa clé maîtresse (voir `authenticate`), qui ne prouve rien de
spécifique à une facture donnée et peut être rejouée sur un statut forgé.
`parse` reprend donc le jeton de la notification pour interroger PayDunya en
retour (`_confirm`) et fonde le statut appliqué sur **cette** réponse, jamais
sur ce que le corps posté prétend. C'est le seul appel réseau du module côté
entrant, et il est délibéré : sans lui, quiconque a vu passer l'empreinte une
fois — y compris sur son propre paiement légitime — pouvait encaisser
n'importe quelle commande.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from typing import Any

import httpx
from django.conf import settings

from apps.payments.gateway import (
    ABANDONED,
    CheckoutInstruction,
    GatewayError,
    Notification,
)
from apps.payments.models import PaymentStatus, Transaction

__all__ = ["PayDunyaGateway"]

logger = logging.getLogger(__name__)

#: Statut PayDunya → statut interne.
#:
#: `cancelled` n'a pas d'équivalent direct : il désigne un client qui a fermé
#: la page de paiement, donc après le passage en « en cours ». La machine à
#: états n'autorisant pas `processing → cancelled`, l'abandon est enregistré
#: comme un échec, avec un motif qui dit ce qui s'est passé.
STATUS_MAP: Mapping[str, str] = {
    "completed": PaymentStatus.COMPLETED,
    "pending": PaymentStatus.PROCESSING,
    "failed": PaymentStatus.FAILED,
    "cancelled": ABANDONED,
}

#: Code de succès de l'API. Tout le reste est une erreur, quel que soit le
#: statut HTTP — PayDunya répond 200 même sur un refus métier, et se fier au
#: code HTTP ferait prendre un rejet pour une facture ouverte.
SUCCESS_CODE = "00"


class PayDunyaGateway:
    """Facturation et notifications PayDunya."""

    @property
    def _base_url(self) -> str:
        """Bac à sable ou production, selon `PAYDUNYA_MODE`.

        Deux URL distinctes plutôt qu'un drapeau dans la requête : une erreur
        de configuration envoie alors les appels vers un service qui n'a pas
        d'argent réel, ce qui se voit tout de suite. L'inverse — le mode
        production atteint par mégarde — encaisserait pour de bon.
        """
        if settings.PAYDUNYA_MODE == "live":
            return "https://app.paydunya.com/api/v1"
        return "https://app.paydunya.com/sandbox-api/v1"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "PAYDUNYA-MASTER-KEY": settings.PAYDUNYA_MASTER_KEY,
            "PAYDUNYA-PRIVATE-KEY": settings.PAYDUNYA_PRIVATE_KEY,
            "PAYDUNYA-TOKEN": settings.PAYDUNYA_TOKEN,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------- sortant

    def open_checkout(self, transaction: Transaction) -> CheckoutInstruction:
        """Crée une facture et rend l'URL de paiement.

        Le montant part en **unité mineure**, telle qu'elle est stockée : le
        XOF n'ayant pas de décimale, 4 000 F s'écrit 4000. Convertir ici
        introduirait le flottant que toute la chaîne s'attache à exclure.

        `custom_data` transporte notre identifiant de transaction. Il revient
        tel quel dans la notification, ce qui donne un second chemin de
        rapprochement si le jeton de facture venait à manquer.

        `order` est absent pour un encaissement qui n'en règle pas — un
        abonnement. La facture décrit alors la transaction elle-même plutôt
        qu'une commande qui n'existe pas.
        """
        order = transaction.order
        description = (
            f"Commande {order.reference} — {order.restaurant.name}" if order else "El Corazón"
        )
        store_name = order.restaurant.name if order else "El Corazón"
        custom_data = {"transaction": str(transaction.pk)}
        if order:
            custom_data |= {"order": str(order.pk), "reference": order.reference}

        payload = {
            "invoice": {
                "total_amount": transaction.amount.amount_minor,
                "description": description,
            },
            "store": {"name": store_name},
            "custom_data": custom_data,
            "actions": {"callback_url": settings.PAYDUNYA_CALLBACK_URL},
        }

        try:
            with httpx.Client(timeout=settings.PAYDUNYA_TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{self._base_url}/checkout-invoice/create",
                    json=payload,
                    headers=self._headers,
                )
        except httpx.HTTPError as exc:
            # Le réseau a lâché. Ce n'est pas une faute du client : la vue
            # traduira en 502, et le client pourra réessayer sans rien corriger.
            raise GatewayError(f"PayDunya injoignable : {exc}") from exc

        return self._read_checkout(response)

    def _read_checkout(self, response: httpx.Response) -> CheckoutInstruction:
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise GatewayError("Réponse PayDunya illisible.") from exc

        if str(body.get("response_code")) != SUCCESS_CODE:
            # PayDunya répond 200 même sur un refus métier : c'est
            # `response_code` qui fait autorité, jamais le statut HTTP.
            raise GatewayError(
                f"PayDunya a refusé la facture : {body.get('response_text', 'sans motif')}"
            )

        token = str(body.get("token", "")).strip()
        url = str(body.get("invoice_url") or body.get("response_text") or "").strip()
        if not token or not url.startswith("http"):
            raise GatewayError("Réponse PayDunya incomplète : jeton ou URL manquant.")

        return CheckoutInstruction(
            provider_reference=token,
            checkout_url=url,
            instructions="Réglez sur la page PayDunya, puis revenez sur l'application.",
        )

    # ------------------------------------------------------------- entrant

    def authenticate(
        self, *, raw_body: bytes, headers: Mapping[str, str], data: Mapping[str, Any]
    ) -> bool:
        """Premier filtre, pas une authentification à elle seule.

        PayDunya ne signe pas le corps : il joint l'empreinte SHA-512 de la clé
        maîtresse, **identique sur toute notification légitime**. Ce n'est pas
        seulement rejouable — P1 (idempotence sur `event_id`) fermerait le
        rejeu à l'identique — c'est *fabricable* : qui l'a vue passer une fois
        peut forger un événement inédit (nouveau statut, nouveau jeton) qui
        porte la même empreinte. L'idempotence ne protège pas contre ça.

        Ce filtre écarte donc seulement ce qui n'a même pas l'empreinte
        attendue — un scan, un client mal configuré. La décision qui compte —
        quel est le **vrai** statut de cette facture — ne se prend jamais sur
        la foi de ce test : elle est reprise auprès de PayDunya lui-même dans
        `parse` (`_confirm`), qui est la seule source de vérité.

        `compare_digest` pour la même raison qu'ailleurs : une comparaison
        naïve fuit par son temps d'exécution.
        """
        recu = str(self._section(data).get("hash", "")).strip()
        if not recu or not settings.PAYDUNYA_MASTER_KEY:
            # Sans clé configurée, aucune empreinte ne peut être juste. Le
            # défaut ferme la porte plutôt que de l'ouvrir.
            return False

        attendu = hashlib.sha512(settings.PAYDUNYA_MASTER_KEY.encode()).hexdigest()
        return hmac.compare_digest(attendu, recu)

    def parse(self, data: Mapping[str, Any]) -> Notification:
        """Extrait ce qui nous intéresse d'une notification PayDunya.

        Le corps posté n'est **jamais** la source du statut appliqué — c'est
        le cœur du correctif. `authenticate` vient de le dire : l'empreinte
        jointe ne prouve rien de spécifique à cette facture, donc rien dans ce
        corps n'est probant sur `completed`/`failed`/`cancelled`. Seul le
        **jeton** en est tiré, pour savoir *de quelle facture* PayDunya
        prétend parler ; le statut, lui, est relu directement chez PayDunya
        (`_confirm`), qui seul sait ce qui s'est réellement passé sur ce
        jeton. Un corps forgé peut bien réclamer « payé » : la confirmation
        active répondra ce que PayDunya sait vraiment, jamais ce que le
        corps affirme.

        Le corps est un formulaire **imbriqué** — `data[invoice][token]` — que
        Django présente soit comme un dictionnaire de dictionnaires, soit à
        plat selon l'encodage reçu. Les deux formes sont acceptées : refuser la
        seconde ferait échouer l'intégration sur un détail de sérialisation que
        nous ne maîtrisons pas.
        """
        section = self._section(data)

        token = str(
            self._nested(section, "invoice", "token")
            or self._nested(section, "custom_data", "transaction")
            or ""
        ).strip()
        if not token:
            raise GatewayError("Notification sans jeton de facture.")

        confirmee = self._confirm(token)

        brut = str(confirmee.get("status", "")).strip().lower()

        status = STATUS_MAP.get(brut)
        if status is None:
            raise GatewayError(f"Statut PayDunya inconnu : {brut!r}.")

        motif = ""
        if brut == "cancelled":
            motif = "Paiement abandonné par le client sur la page PayDunya."
        elif brut == "failed":
            motif = str(confirmee.get("response_text", "Échec signalé par PayDunya."))

        return Notification(
            # Référence **et** statut : PayDunya notifie plusieurs fois la même
            # facture au fil de sa progression, et la référence seule ferait
            # ignorer l'encaissement au motif qu'on a déjà vu passer l'attente.
            event_id=f"{token}:{brut}",
            provider_reference=token,
            status=status,
            reason=motif,
        )

    def _confirm(self, token: str) -> Mapping[str, Any]:
        """Interroge PayDunya pour le statut réel d'une facture.

        C'est l'appel qui ferme la faille de `authenticate` : la notification
        posée dit « voici le token, et une empreinte qui ne prouve rien sur son
        statut » ; cet appel demande directement à PayDunya, avec nos propres
        clés marchand, ce qu'il en est *réellement*. Un corps forgé peut
        prétendre à un statut qu'il n'a pas — il ne peut pas faire répondre
        PayDunya à sa place.

        **Avertissement**, comme pour `_read_checkout` : la forme exacte de
        cette réponse suit le contrat documenté par PayDunya, non confrontée à
        l'API réelle depuis ce poste. À vérifier avant la mise en service avec
        une facture réelle en mode `test`.
        """
        try:
            with httpx.Client(timeout=settings.PAYDUNYA_TIMEOUT_SECONDS) as client:
                response = client.get(
                    f"{self._base_url}/checkout-invoice/confirm/{token}",
                    headers=self._headers,
                )
            body: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise GatewayError(f"Confirmation PayDunya impossible : {exc}") from exc
        except ValueError as exc:
            raise GatewayError("Réponse de confirmation PayDunya illisible.") from exc

        if str(body.get("response_code")) != SUCCESS_CODE:
            raise GatewayError(
                f"PayDunya n'a pas confirmé la facture : {body.get('response_text', 'sans motif')}"
            )
        return body

    @staticmethod
    def _section(data: Mapping[str, Any]) -> Mapping[str, Any]:
        """Contenu utile, que PayDunya place sous `data`."""
        section = data.get("data")
        return section if isinstance(section, Mapping) else data

    @staticmethod
    def _nested(section: Mapping[str, Any], *path: str) -> Any:
        """Lit une valeur imbriquée, quelle que soit la forme reçue.

        `{"invoice": {"token": "x"}}` et `{"invoice[token]": "x"}` disent la
        même chose : le second est ce que produit un formulaire encodé à plat.
        """
        courant: Any = section
        for cle in path:
            if isinstance(courant, Mapping) and cle in courant:
                courant = courant[cle]
            else:
                return section.get(f"{path[0]}[{']['.join(path[1:])}]") if len(path) > 1 else None
        return courant
