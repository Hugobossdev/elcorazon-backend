"""Tarification de la livraison.

Le barème vit sur la zone (ADR-006) ; ce module l'applique. Il est isolé des
commandes pour deux raisons : il se teste sans créer de commande, et la même
règle sert au devis affiché *avant* la validation du panier comme au montant
figé *sur* la commande. Deux implémentations donneraient deux prix, dont l'un
serait faux — c'est exactement ce qu'avait produit l'implémentation
précédente, avec `5.00` d'un côté et `500.0` de l'autre.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.geography.models import DeliveryZone
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["DeliveryQuote", "quote_delivery"]


@dataclass(frozen=True, slots=True)
class DeliveryQuote:
    """Devis d'une course.

    `fee` est ce qu'on facture, `gross_fee` ce que la course vaut. Les deux
    diffèrent quand le franco s'applique — et il faut les deux, parce que
    l'offrir au client ne veut pas dire que le livreur travaille gratuitement.
    """

    zone: DeliveryZone
    distance_km: Decimal
    fee: Money
    gross_fee: Money
    is_free: bool
    estimated_minutes: int


def quote_delivery(*, zone: DeliveryZone, distance_m: float, subtotal: Money) -> DeliveryQuote:
    """Frais de livraison pour une course dans cette zone.

    Trois refus possibles, tous métier et non techniques :

    * au-delà de `max_distance_km`, la course est refusée **même si le point
      est dans le contour** — un contour se dessine large, la distance
      réellement parcourue est ce qui coûte ;
    * en deçà de `min_order_amount`, la commande ne couvre pas le déplacement ;
    * une devise étrangère à la zone n'est pas convertie en silence.
    """
    if subtotal.currency != zone.base_fee.currency:
        raise BusinessRuleViolation(
            f"Le panier est en {subtotal.currency}, la zone facture en "
            f"{zone.base_fee.currency}. Aucune conversion implicite n'est faite."
        )

    distance_km = (Decimal(distance_m) / 1000).quantize(Decimal("0.01"))
    if distance_km > zone.max_distance_km:
        raise BusinessRuleViolation(
            f"Adresse à {distance_km} km, au-delà des {zone.max_distance_km} km "
            f"desservis depuis cet établissement.",
            distance_km=str(distance_km),
            max_distance_km=str(zone.max_distance_km),
        )

    if zone.min_order_amount is not None and subtotal < zone.min_order_amount:
        raise BusinessRuleViolation(
            f"Commande minimum de {zone.min_order_amount} dans cette zone.",
            min_order_amount=str(zone.min_order_amount.amount_minor),
        )

    # `percentage` est la seule multiplication par un décimal que `Money`
    # expose, et son arrondi est explicite : × 3,50 km s'écrit × 350 %.
    # Multiplier par un flottant introduirait l'imprécision que tout le reste
    # de la chaîne s'attache à exclure.
    gross = zone.base_fee + zone.fee_per_km.percentage(distance_km * 100)

    # Le franco est une remise commerciale faite au client. Elle ne change pas
    # ce que la course coûte, et c'est pourquoi `gross_fee` est calculé dans
    # tous les cas : la commission du livreur s'appuie dessus, sans quoi il
    # roulerait gratuitement chaque fois qu'un panier dépasse le seuil.
    free = zone.free_delivery_threshold is not None and subtotal >= zone.free_delivery_threshold

    return DeliveryQuote(
        zone=zone,
        distance_km=distance_km,
        fee=Money.zero(subtotal.currency) if free else gross,
        gross_fee=gross,
        is_free=free,
        estimated_minutes=zone.estimated_delivery_minutes,
    )
