"""Encaissement des espèces à la remise.

## Ce qui manquait

Les autres moyens de paiement se soldent par une notification signée du
prestataire (`PaymentService._apply`). Les espèces n'ont pas de prestataire :
l'argent passe de la main du client à celle du livreur, et **rien ne
l'enregistrait**. La transaction ouverte par `initiate` restait `processing`
pour toujours, et `Order.amount_paid` restait nul sur une commande livrée et
payée — le back-office y lisait « rien à rembourser », les chiffres
d'encaissement ignoraient toute l'activité en espèces.

## La règle

L'espèce est encaissée **à la remise** : au passage de la commande en
`delivered`, pour le solde qui restait dû — `amount_to_collect`, exactement ce
que l'application du livreur lui a demandé de percevoir. Ni avant (en route, il
n'a rien reçu), ni pour un autre moyen (une commande mobile money livrée sans
encaissement reste impayée : ce n'est pas le livreur qui a reçu cet argent).

Le moment est celui de la **commande**, et non de la course : une livraison
menée à la main par le personnel, sans course, encaisse de la même façon.
"""

from __future__ import annotations

import logging

from apps.orders.models import Order, PaymentMethod
from apps.payments.gateway import gateway_for
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.payments.services import PaymentService, report_settled_total, settled_total
from apps.payments.signals import payment_transaction_settled

logger = logging.getLogger(__name__)


def record_cash_collected(order: Order) -> Transaction | None:
    """Enregistre les espèces remises au livreur pour cette commande livrée.

    Appelée dans la transaction du passage en `delivered` : la commande livrée
    et son encaissement s'écrivent ensemble, ou pas du tout. Rend la
    transaction soldée, ou `None` s'il n'y avait rien à encaisser.

    Idempotente par construction : une fois soldée, la commande n'a plus de
    reste dû, et un second appel ne trouve rien à faire.
    """
    if order.payment_method != PaymentMethod.CASH:
        return None

    locked = Order.objects.select_for_update().get(pk=order.pk)
    reste = locked.total - settled_total(locked)
    if not reste.is_positive:
        return None

    # La demande ouverte par le client à l'écran de règlement, s'il y est
    # passé. La solder plutôt qu'en créer une seconde : deux lignes pour une
    # seule remise d'argent fausseraient le journal des encaissements.
    especes = (
        locked.transactions.select_for_update()
        .filter(
            provider=PaymentProvider.CASH,
            status__in=(PaymentStatus.PENDING, PaymentStatus.PROCESSING),
            amount_minor=reste.amount_minor,
            amount_currency=reste.currency,
        )
        .order_by("-created_at")
        .first()
    )
    if especes is None:
        # Le client n'a jamais ouvert l'écran de règlement : la transaction
        # naît ici, par le même chemin qu'`initiate` pour sa référence.
        especes = Transaction(  # type: ignore[misc]
            order=locked,
            provider=PaymentProvider.CASH,
            provider_reference="",
            amount=reste,
            payer=locked.customer,
            status=PaymentStatus.PENDING,
        )
        especes.provider_reference = (
            gateway_for(PaymentProvider.CASH).open_checkout(especes).provider_reference
        )
        especes.save()

    if especes.status == PaymentStatus.PENDING:
        PaymentService._move(especes, PaymentStatus.PROCESSING)
    PaymentService._move(especes, PaymentStatus.COMPLETED)

    # Même suite qu'un encaissement par prestataire, moins la confirmation :
    # la commande est déjà livrée.
    report_settled_total(locked)
    payment_transaction_settled.send(sender=Transaction, transaction=especes)
    logger.info(
        "payments.cash_collected",
        extra={
            "order": str(locked.pk),
            "transaction": str(especes.pk),
            "amount_minor": especes.amount.amount_minor,
        },
    )
    return especes
