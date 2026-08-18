"""Création et cycle de vie des commandes — invariants C1 à C5, ADR-010.

C'est l'agrégat comptable du produit, et le seul module du backend qui écrive
un statut de commande. Trois choses s'y jouent :

* **la valorisation** — les prix sont relus du catalogue sous verrou, jamais
  reçus du client (C1), et le total est recomposé serveur (C2) ;
* **la transition** — elle passe par la machine à états, qui vérifie, journalise
  et rend le retour arrière inexprimable (C3, C4) ;
* **la copie figée** — la commande garde son propre exemplaire de l'adresse et
  des libellés, si bien qu'une adresse effacée ou un article renommé ne
  réécrivent pas l'histoire.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Protocol

from django.contrib.gis.db.models.functions import Distance
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.carts.models import Cart
from apps.carts.services import CartService, PricedSelection, price_cart
from apps.catalog.services import StockService, record_purchase
from apps.geography.models import DeliveryZone
from apps.geography.services import DeliveryQuote, quote_delivery
from apps.orders.models import Order, OrderLine, OrderStatusEvent
from apps.orders.signals import order_status_changed
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.profiles.models import Address
from apps.promotions.services import PromotionService
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from common.money import Money
from common.realtime import order_group, publish, restaurant_group

__all__ = ["OrderService", "next_reference"]

#: Statuts depuis lesquels le client peut encore annuler lui-même.
#:
#: La machine autorise l'annulation jusqu'à `ready` ; cette liste est plus
#: étroite, et c'est une décision commerciale et non technique : une fois la
#: cuisine lancée, l'annulation appartient au restaurant, qui sait ce qui est
#: déjà perdu. Le personnel muni de `orders.cancel` n'est pas concerné.
CUSTOMER_CANCELLABLE = frozenset({OrderStatus.PENDING, OrderStatus.CONFIRMED})


def next_reference() -> str:
    """Référence courte et lisible — `EC000001`.

    Tirée d'une séquence PostgreSQL et non d'un `COUNT` : deux commandes
    simultanées obtiendraient le même numéro avec un compteur applicatif, et le
    second `INSERT` échouerait sur l'unicité — un client sur deux verrait une
    erreur aux heures de pointe. La séquence n'est pas transactionnelle, donc
    elle ne bloque personne ; les trous qu'elle laisse sur une transaction
    annulée sont sans conséquence, une référence n'étant pas un numéro de
    facture réglementaire.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('order_reference_seq')")
        (value,) = cursor.fetchone()
    return f"EC{value:06d}"


class _HasItemAndQuantity(Protocol):
    """Ce qu'une ligne doit porter pour peser sur le stock.

    Le protocole couvre `CartLine` comme `OrderLine` : la consommation part du
    panier, le retour part de la commande, et les deux se comptent de la même
    façon. Les typer par leur classe obligerait à écrire deux fois la même
    agrégation.
    """

    menu_item_id: uuid.UUID
    quantity: int


def _quantities_by_item(lines: Iterable[_HasItemAndQuantity]) -> dict[uuid.UUID, int]:
    """Totalise les quantités par article.

    Deux lignes peuvent désigner le même article avec des options différentes —
    un burger saignant et un burger à point. Les décompter séparément prendrait
    deux verrous sur la même ligne de stock et pourrait passer la vérification
    deux fois sur un reliquat d'une unité.
    """
    quantities: dict[uuid.UUID, int] = defaultdict(int)
    for line in lines:
        quantities[line.menu_item_id] += line.quantity
    return dict(quantities)


class OrderService:
    # ------------------------------------------------------------- création

    @staticmethod
    @transaction.atomic
    def create_from_cart(
        *,
        user: User,
        cart: Cart,
        address: Address,
        payment_method: str,
        instructions: str = "",
        promo_code: str = "",
    ) -> Order:
        """Transforme un panier en commande.

        Tout se passe dans une transaction : la commande, ses lignes et le
        vidage du panier réussissent ou échouent ensemble. Un panier vidé sans
        commande créée serait la pire des deux issues — le client a perdu sa
        sélection et n'a rien commandé.
        """
        cart = CartService.load(cart)
        order = OrderService.create_from_selection(
            user=user,
            restaurant=cart.restaurant,
            selection=price_cart(cart).selection,
            address=address,
            payment_method=payment_method,
            instructions=instructions,
            promo_code=promo_code,
        )
        CartService.clear(cart)
        return order

    @staticmethod
    @transaction.atomic
    def create_from_selection(
        *,
        user: User,
        restaurant: Restaurant,
        selection: PricedSelection,
        address: Address,
        payment_method: str,
        instructions: str = "",
        promo_code: str = "",
    ) -> Order:
        """Transforme une sélection déjà valorisée en commande.

        Extraite de `create_from_cart` pour que le panier collaboratif emprunte
        exactement ce chemin : même relecture des prix, même décompte de stock,
        même barème de zone, même évaluation du code promotionnel. Un second
        chemin de création aurait été le moyen le plus sûr de faire diverger C2 —
        c'est déjà ainsi que les frais de livraison de l'implémentation
        précédente avaient fini par être calculés deux fois différemment.

        Ce qui reste à l'appelant est ce qui lui est propre : vider le panier
        personnel, ou clore le panier collaboratif.
        """
        priced = selection

        if not priced.lines:
            raise BusinessRuleViolation("Le panier est vide.")
        if not priced.is_orderable:
            indisponibles = priced.unavailable_names
            raise BusinessRuleViolation(
                "Certains articles ne sont plus commandables : "
                f"{', '.join(indisponibles)}. Retirez-les du panier.",
                unavailable=indisponibles,
            )

        # Le numéro est celui du destinataire s'il diffère du titulaire —
        # livraison à un tiers — sinon celui du compte. Aucun des deux n'est
        # obligatoire pris isolément, mais une course sans numéro joignable est
        # une course perdue : à Lomé, le livreur appelle pour trouver la porte.
        recipient_phone = address.recipient_phone or user.phone
        if not recipient_phone:
            raise BusinessRuleViolation(
                "Un numéro joignable est nécessaire à la livraison : renseignez "
                "celui du compte ou celui de l'adresse."
            )

        # Le stock est décompté **dans la transaction**, avant toute écriture :
        # si la suite échoue — adresse hors zone, code promotionnel refusé —,
        # le retrait est annulé avec le reste. C'est ce qui permet de le faire
        # tôt, et donc de refuser la commande avant d'avoir créé quoi que ce
        # soit qu'il faudrait ensuite défaire.
        StockService.consume(_quantities_by_item(line.line for line in priced.lines))

        quote = OrderService._quote_for(restaurant, address, priced.subtotal)

        # C2 — le total est recomposé ici, à partir de valeurs dont aucune n'a
        # traversé le réseau depuis le client. Le code promo ne fait pas
        # exception : le client envoie une chaîne, le serveur décide ce qu'elle
        # vaut (F4).
        promotion = None
        discount = Money.zero(priced.currency)
        if promo_code.strip():
            devis = PromotionService.quote(
                code=promo_code,
                user=user,
                restaurant=restaurant,
                subtotal=priced.subtotal,
                delivery_fee=quote.fee,
            )
            promotion, discount = devis.promotion, devis.discount

        total = priced.subtotal + quote.fee - discount

        # `subtotal`, `delivery_fee`, `discount` et `total` sont des
        # `MoneyField` : deux colonnes réelles derrière une propriété, que le
        # greffon django-stubs ne sait pas relier au nom qu'on passe ici.
        order = Order.objects.create(  # type: ignore[misc]
            reference=next_reference(),
            restaurant=restaurant,
            customer=user,
            delivery_address_line=", ".join(filter(None, [address.line1, address.line2])),
            delivery_landmark=address.landmark,
            delivery_location={"lat": address.location.y, "lon": address.location.x},
            delivery_instructions=instructions or address.delivery_instructions,
            recipient_name=address.recipient_name or user.full_name,
            recipient_phone=recipient_phone,
            subtotal=priced.subtotal,
            delivery_fee=quote.fee,
            delivery_fee_gross=quote.gross_fee,
            discount=discount,
            total=total,
            payment_method=payment_method,
            # Copie figée du code, comme le reste : la promotion peut être
            # retirée du back-office sans rendre la commande illisible.
            promo_code=promotion.code if promotion else "",
            estimated_delivery_at=timezone.now()
            + dt.timedelta(
                minutes=restaurant.default_preparation_minutes + quote.estimated_minutes
            ),
        )

        OrderLine.objects.bulk_create(
            OrderLine(  # type: ignore[misc]
                order=order,
                menu_item=priced_line.line.menu_item,
                item_name=priced_line.line.menu_item.name,
                unit_price=priced_line.unit_price,
                quantity=priced_line.line.quantity,
                line_total=priced_line.total,
                # Copie figée : le libellé du groupe et celui de l'option sont
                # recopiés, pour qu'un renommage au catalogue ne réécrive pas
                # ce que le client a commandé.
                options=[
                    {
                        "group": option.group.name,
                        "option": option.name,
                        "delta": option.price_delta.amount_minor,
                        "currency": option.price_delta.currency,
                    }
                    for option in priced_line.options
                ],
                notes=priced_line.line.notes,
            )
            for priced_line in priced.lines
        )

        if promotion is not None:
            # Consommé **après** la création : le quota ne se décompte que si
            # la commande existe. L'ordre inverse laisserait un code entamé par
            # une commande refusée plus loin — panier devenu incommandable,
            # adresse hors zone.
            PromotionService.redeem(
                promotion=promotion, user=user, order_id=order.pk, discount=discount
            )

        return order

    @staticmethod
    def preview(
        *,
        user: User,
        restaurant: Restaurant,
        address: Address | None = None,
        promo_code: str = "",
    ) -> dict[str, object]:
        """Décompose un total sans rien écrire.

        Même chemin de calcul que `create_from_cart` — mêmes prix relus du
        catalogue, même barème de zone, même évaluation du code. Deux calculs
        distincts donneraient deux totaux, dont l'un serait faux, et c'est
        exactement ce qui s'était produit sur les frais de livraison de
        l'implémentation précédente.

        Rien n'est réservé : le quota d'un code ne se décompte qu'à la
        commande, sans quoi on épuiserait un code en demandant des devis.
        """
        priced = price_cart(CartService.load(CartService.cart_for(user, restaurant)))

        # Un panier vide n'a pas de course à chiffrer, et le barème de zone le
        # dirait mal : `quote_delivery` refuserait un sous-total de zéro au nom
        # du minimum de commande — « commande minimum de 1 000 XOF » pour un
        # panier qui ne contient rien, c'est-à-dire un refus qui nomme la
        # mauvaise cause et transforme l'écran de panier vide en erreur. Le
        # devis répond, `is_orderable` dit non ; la même règle vaut avec ou
        # sans adresse.
        if address is not None and priced.lines:
            frais = OrderService._quote_for(restaurant, address, priced.subtotal).fee
        else:
            # Sans adresse, le barème de l'établissement donne un ordre de
            # grandeur. L'écart avec le montant final est borné : dans le cas
            # courant, les deux zones sont la même.
            frais = restaurant.zone.base_fee

        promotion = None
        discount = Money.zero(priced.currency)
        if promo_code.strip() and priced.lines:
            devis = PromotionService.quote(
                code=promo_code,
                user=user,
                restaurant=restaurant,
                subtotal=priced.subtotal,
                delivery_fee=frais,
            )
            promotion, discount = devis.promotion, devis.discount

        return {
            "subtotal": priced.subtotal,
            "delivery_fee": frais,
            "discount": discount,
            "total": priced.subtotal + frais - discount,
            "promotion": promotion,
            "is_orderable": priced.is_orderable,
        }

    @staticmethod
    def _quote_for(restaurant: Restaurant, address: Address, subtotal: Money) -> DeliveryQuote:
        """Zone et frais de la course, calculés par PostGIS.

        La zone est celle qui couvre l'**adresse de livraison**, pas celle du
        restaurant : c'est le point d'arrivée qui détermine ce qu'on facture et
        ce qu'on refuse de desservir.
        """
        zone = (
            DeliveryZone.objects.filter(
                boundary__covers=address.location,
                is_active=True,
                city__is_active=True,
                city__country__is_active=True,
            )
            .order_by("max_distance_km")
            .first()
        )
        if zone is None:
            raise BusinessRuleViolation(
                "Cette adresse n'est desservie par aucune zone de livraison.",
                address_id=str(address.pk),
            )

        distance = (
            Restaurant.objects.filter(pk=restaurant.pk)
            .annotate(to_address=Distance("location", address.location))
            .values_list("to_address", flat=True)
            .first()
        )
        if distance is None:  # pragma: no cover - le restaurant vient d'être lu
            raise BusinessRuleViolation("Établissement introuvable.")

        return quote_delivery(zone=zone, distance_m=distance.m, subtotal=subtotal)

    # ---------------------------------------------------------- transitions

    @staticmethod
    @transaction.atomic
    def transition_to(
        *,
        order: Order,
        target: str,
        actor: User | None = None,
        reason: str = "",
    ) -> Order:
        """Fait avancer une commande — **le seul** chemin d'écriture du statut.

        La commande est verrouillée le temps de la transition : deux membres du
        personnel qui cliquent en même temps produiraient sinon deux
        événements de journal pour un seul changement, et l'un des deux
        écraserait l'autre.

        Un rejeu vers le statut courant ne fait rien et ne lève pas : c'est P1
        transposé aux commandes, et cela évite qu'un client qui tapote deux
        fois reçoive une erreur pour une action déjà accomplie.
        """
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if ORDER_MACHINE.is_noop(locked.status, target):
            return locked

        ORDER_MACHINE.validate(locked.status, target)

        previous = locked.status
        locked.status = target
        touched = ["status"]

        if target == OrderStatus.DELIVERED:
            locked.delivered_at = timezone.now()
            touched.append("delivered_at")
        elif target == OrderStatus.CANCELLED:
            locked.cancelled_at = timezone.now()
            locked.cancellation_reason = reason
            touched += ["cancelled_at", "cancellation_reason"]

        locked.save(update_fields=[*touched, "updated_at"])

        # Le journal est écrit dans la même transaction que le changement :
        # l'historique est un sous-produit gratuit, pas une écriture séparée
        # qu'on peut oublier d'appeler depuis un nouveau point d'entrée.
        OrderStatusEvent.objects.create(
            order=locked, from_status=previous, to_status=target, actor=actor, reason=reason
        )

        if target == OrderStatus.DELIVERED:
            OrderService._record_purchases(locked)
        elif target == OrderStatus.CANCELLED:
            # Le client ne doit pas perdre son code parce que le restaurant a
            # annulé : il a été décompté pour un repas qu'il n'a jamais reçu.
            PromotionService.release(order_id=locked.pk)
            # Même raisonnement pour les denrées : elles n'ont pas été servies.
            # Le rejeu est déjà écarté plus haut — une commande déjà annulée
            # sort en `is_noop`, donc rien n'est recrédité deux fois.
            StockService.restore(_quantities_by_item(locked.lines.all()))

        # La diffusion part **après le commit** et non pendant : annoncer
        # « commande confirmée » sur une transaction qui échoue ensuite laisse
        # le client devant un écran qui ment, et aucun événement ultérieur ne
        # vient le corriger.
        def _diffuser() -> None:
            payload = {
                "order": str(locked.pk),
                "reference": locked.reference,
                "from_status": previous,
                "status": target,
                "reason": reason,
            }
            publish(order_group(locked.pk), "order.status", payload)
            # Même événement, second public : le tableau de bord du personnel
            # (ADR-008) apprend qu'une commande de son établissement vient de
            # changer d'état, sans avoir à interroger l'API en boucle.
            publish(restaurant_group(locked.restaurant_id), "order.status", payload)

        transaction.on_commit(_diffuser)

        # L'événement de domaine part d'ici. `orders` ne connaît aucun de ses
        # abonnés — c'est le second mécanisme de l'ADR-002, et la seule façon
        # pour `notifications` de réagir sans que le graphe de dépendances
        # devienne cyclique.
        order_status_changed.send(
            sender=Order, order=locked, previous=previous, target=target, reason=reason
        )

        return locked

    @staticmethod
    def _record_purchases(order: Order) -> None:
        """Informe le catalogue que ces articles ont été reçus (S1).

        `orders` connaît `catalog`, jamais l'inverse : c'est donc ici que part
        l'information, et c'est ce qui permet à un avis d'être marqué « achat
        vérifié » sans que le catalogue ait à interroger les commandes.
        """
        moment = order.delivered_at or timezone.now()
        for line in order.lines.select_related("menu_item"):
            record_purchase(user=order.customer, menu_item=line.menu_item, moment=moment)

    @staticmethod
    def cancel_by_customer(*, order: Order, user: User, reason: str) -> Order:
        """Annulation à l'initiative du client.

        Plus restrictive que la machine : passé la confirmation, la cuisine a
        engagé des denrées, et c'est au restaurant de décider ce qui est
        récupérable. Le refus cite l'état courant, pour que l'application
        puisse proposer d'appeler le restaurant plutôt que d'insister.
        """
        if order.status not in CUSTOMER_CANCELLABLE:
            raise BusinessRuleViolation(
                "Cette commande ne peut plus être annulée depuis l'application ; "
                "contactez le restaurant.",
                current_status=order.status,
            )
        return OrderService.transition_to(
            order=order, target=OrderStatus.CANCELLED, actor=user, reason=reason
        )

    @staticmethod
    def cancel_by_staff(*, order: Order, actor: User, reason: str) -> Order:
        """Annulation à l'initiative de l'exploitation.

        Va plus loin que celle du client — jusqu'à `ready`, tout ce que la
        machine autorise — parce que c'est justement le cas qu'elle ne couvre
        pas : la rupture de stock découverte en cuisine, l'adresse
        introuvable, le client injoignable. Sans ce verbe, ces commandes
        restaient bloquées en préparation jusqu'à ce que quelqu'un les fasse
        avancer vers une livraison qui n'aura pas lieu.

        Le motif est **obligatoire**, là où celui du client est facultatif. Ce
        n'est pas une asymétrie gratuite : le client annule sa propre commande
        et n'a de comptes à rendre à personne, tandis qu'un opérateur annule
        celle d'un tiers, qui sera remboursé et rappellera pour comprendre. Un
        journal d'annulations sans motif ne répond pas à cette question, et
        c'est la seule pour laquelle on le consulte.

        Rien d'autre n'est fait ici : la libération du code promotionnel, la
        remise en stock, le journal et la diffusion temps réel appartiennent à
        `transition_to`, qui les fait pour toute annulation d'où qu'elle
        vienne. Les refaire ici les ferait deux fois le jour où quelqu'un
        annule par l'autre chemin.
        """
        return OrderService.transition_to(
            order=order, target=OrderStatus.CANCELLED, actor=actor, reason=reason
        )
