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
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Protocol

from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.availability.services import AvailabilityService, Demand
from apps.carts.models import Cart
from apps.carts.services import CartService, PricedLine, PricedSelection, price_cart
from apps.catalog.services import StockService, record_purchase
from apps.geography.services import DeliveryQuote
from apps.orders.models import (
    Order,
    OrderLine,
    OrderStatusEvent,
    PaymentMethod,
    accepted_payment_methods,
)
from apps.orders.signals import order_created, order_status_changed
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.production.services import MaterialService, ProducedLine
from apps.profiles.models import Address
from apps.promotions.services import PromotionService
from apps.restaurants.delivery import check_delivery
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


logger = logging.getLogger(__name__)


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


def _a_produire_depuis_le_panier(priced: Iterable[PricedLine]) -> list[ProducedLine]:
    """Traduit les lignes valorisées du panier en demandes de production.

    La traduction vit ici, et non dans `production`, parce que c'est `orders`
    qui connaît la forme de ses lignes — le graphe de dépendances va dans ce
    sens et pas dans l'autre.
    """
    return [
        ProducedLine(
            menu_item_id=priced_line.line.menu_item_id,
            quantity=priced_line.line.quantity,
            option_ids=tuple(option.pk for option in priced_line.options),
        )
        for priced_line in priced
    ]


def _a_produire_depuis_la_commande(order: Order) -> list[ProducedLine]:
    """Les mêmes demandes, relues depuis l'instantané de la commande.

    ## Les commandes antérieures n'ont pas d'identifiant d'option

    `option_id` a été ajouté à l'instantané en même temps que la consommation
    par recette. Les commandes créées **avant** portent des options sans
    identifiant, et elles seront encore en cours le jour du déploiement.

    Elles sont donc lues sans leurs options : la recette de base rend sa
    matière, les suppléments non. C'est inexact, et c'est le moins mauvais des
    trois comportements possibles — refuser l'annulation bloquerait
    l'exploitation, et deviner l'option par son libellé ferait dépendre un
    mouvement de stock d'une chaîne de caractères que le catalogue peut
    renommer.

    L'écart s'éteint de lui-même : il ne concerne que les commandes ouvertes au
    moment du déploiement.
    """
    a_produire: list[ProducedLine] = []
    for line in order.lines.all():
        identifiants: list[uuid.UUID] = []
        for option in line.options:
            brut = option.get("option_id")
            if brut is None:
                continue
            try:
                identifiants.append(uuid.UUID(brut))
            except (ValueError, AttributeError, TypeError):
                # Un instantané est une copie, pas une source de vérité : une
                # valeur illisible se saute, elle ne fait pas échouer une
                # annulation.
                continue
        a_produire.append(
            ProducedLine(
                menu_item_id=line.menu_item_id,
                quantity=line.quantity,
                option_ids=tuple(identifiants),
            )
        )
    return a_produire


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

        # Le moyen de paiement d'abord : rien n'est verrouillé ni décompté pour
        # une commande que le serveur n'encaissera pas. Le client lit la même
        # liste (`GET /payments/methods/`) ; ce refus est le filet d'une
        # application ancienne, ou d'un réglage changé entre-temps.
        if payment_method not in accepted_payment_methods():
            libelle = (
                PaymentMethod(payment_method).label
                if payment_method in PaymentMethod.values
                else payment_method
            )
            raise BusinessRuleViolation(
                f"Le paiement « {libelle} » n'est pas accepté pour le moment. "
                "Choisissez un autre moyen de paiement.",
                payment_method=payment_method,
            )

        # **La** règle, au moment d'écrire — et non d'après le verdict que la
        # sélection a pu emporter à sa lecture : un panier collaboratif se
        # compose en une heure, et le client a pu ouvrir la carte cuisine
        # ouverte puis payer après sa fermeture.
        #
        # Elle juge dans l'ordre la cuisine (relue sous verrou), la cohérence du
        # panier, la desserte de l'adresse, les articles et leurs
        # personnalisations, puis le barème. Ces vérifications étaient écrites
        # ici à la suite, et la desserte venait **après** le décompte du stock :
        # une adresse hors zone prenait des verrous de stock pour rien.
        acceptation = AvailabilityService.assert_can_accept_order(
            restaurant=restaurant,
            demands=[
                Demand(
                    menu_item=ligne.line.menu_item,
                    quantity=ligne.line.quantity,
                    options=ligne.options,
                    variant=ligne.line.variant,
                )
                for ligne in priced.lines
            ],
            delivery_point=address.location,
            subtotal=priced.subtotal,
        )
        # La cuisine relue : son délai de préparation est celui d'**à présent**.
        restaurant = acceptation.kitchen
        quote = acceptation.quote
        # La zone qui a tarifé la course — celle de l'adresse, jamais celle où la
        # cuisine est posée. Sa ville est celle de la cuisine par construction
        # (`resolve_zone` borne les zones municipales à cette ville, et une zone
        # propre se pose dans la ville de son établissement).
        zone = quote.zone

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
        # si la suite échoue — code promotionnel refusé —, le retrait est annulé
        # avec le reste. C'est ce qui permet de le faire tôt, et donc de refuser
        # la commande avant d'avoir créé quoi que ce soit qu'il faudrait ensuite
        # défaire.
        StockService.consume(_quantities_by_item(line.line for line in priced.lines))

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
            # Figés, comme l'adresse : la commande reste attribuée à ce marché,
            # cette ville et cette zone même si la cuisine est rattachée ailleurs
            # demain, ou la zone redessinée.
            country_id=zone.city.country_id,
            city_id=zone.city_id,
            delivery_zone=zone,
            delivery_zone_name=zone.name,
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
                variant=priced_line.line.variant,
                variant_name=(
                    priced_line.line.variant.name if priced_line.line.variant is not None else ""
                ),
                unit_price=priced_line.unit_price,
                quantity=priced_line.line.quantity,
                line_total=priced_line.total,
                # Copie figée : le libellé du groupe et celui de l'option sont
                # recopiés, pour qu'un renommage au catalogue ne réécrive pas
                # ce que le client a commandé.
                #
                # `option_id` s'y ajoute au même titre que `menu_item` sur cette
                # ligne : il sert à **retrouver l'origine** — ici la recette, donc
                # la matière à rendre si la commande est annulée — et rien ne s'y
                # appuie pour l'affichage ni pour la facturation. Une option
                # supprimée du catalogue laisse donc un identifiant qui ne
                # désigne plus rien, ce qui est sans conséquence : la
                # nomenclature introuvable ne rend simplement aucune matière.
                options=[
                    {
                        "group": option.group.name,
                        "option": option.name,
                        "option_id": str(option.pk),
                        "delta": option.price_delta.amount_minor,
                        "currency": option.price_delta.currency,
                    }
                    for option in priced_line.options
                ],
                notes=priced_line.line.notes,
            )
            for priced_line in priced.lines
        )

        # La matière est **promise** ici, et pas plus tôt, pour une raison qui
        # tient en un mot : la référence. Le journal de stock doit pouvoir dire
        # *pourquoi* deux kilos d'oignon sont immobilisés, et `EC001234` répond
        # là où une chaîne vide laisse un mouvement orphelin.
        #
        # Le décompte des plats finis, lui, reste en amont : il refuse la
        # commande avant toute écriture. Ici, l'ordre est sans effet sur le
        # refus — la transaction est atomique, et un manque de matière emporte
        # la commande et ses lignes avec lui.
        #
        # Réserver plutôt que consommer : entre la commande et le feu, la
        # matière est promise sans être partie. Un stock qui l'ignore annonce
        # « il reste 3 kg » quand 2,8 sont déjà dus, et c'est la commande
        # suivante qui découvre le mensonge.
        MaterialService.reserve(
            restaurant_id=order.restaurant_id,
            lines=_a_produire_depuis_le_panier(priced.lines),
            reference=order.reference,
            actor=user,
        )

        if promotion is not None:
            # Consommé **après** la création : le quota ne se décompte que si
            # la commande existe. L'ordre inverse laisserait un code entamé par
            # une commande refusée plus loin — panier devenu incommandable,
            # adresse hors zone.
            PromotionService.redeem(
                promotion=promotion, user=user, order_id=order.pk, discount=discount
            )

        # La commande **entre**, et l'exploitation doit l'apprendre maintenant.
        #
        # Diffusé après le commit, pour la même raison que dans
        # `transition_to` : annoncer une commande sur une transaction qui échoue
        # ensuite ferait apparaître au tableau de bord une ligne qui n'existe
        # pas, et rien ne viendrait la retirer.
        def _diffuser() -> None:
            publish(
                restaurant_group(order.restaurant_id),
                "order.created",
                {
                    "order": str(order.pk),
                    "reference": order.reference,
                    "status": order.status,
                    "total": order.total.amount_minor,
                    "currency": order.total.currency,
                },
            )

        transaction.on_commit(_diffuser)

        # Où la commande a été prise — sans client, sans adresse, sans montant
        # nominatif : la référence relie la ligne au reste, et le `request_id`
        # posé par `common.observabilite` à la requête.
        logger.info(
            "order.created",
            extra={
                "reference": order.reference,
                "kitchen": restaurant.slug,
                "country": zone.city.country.iso_code,
                "city": zone.city.slug,
                "delivery_zone": zone.name,
            },
        )

        # Le signal, lui, part **dans** la transaction : son abonné écrit une
        # notification en base, qui doit vivre ou mourir avec la commande.
        order_created.send(sender=Order, order=order)

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

        # Le premier motif bloquant, du plus général au plus précis : la cuisine
        # avant les articles. Un bouton « Commander » grisé sans phrase laisse
        # le client chercher ce qu'il a mal fait.
        if priced.kitchen is not None:
            code, motif = priced.unavailable_code, priced.unavailable_reason
        else:
            bloquante = next((ligne for ligne in priced.lines if not ligne.is_orderable), None)
            code = bloquante.unavailable_code if bloquante is not None else ""
            motif = bloquante.unavailable_reason if bloquante is not None else ""

        return {
            "subtotal": priced.subtotal,
            "delivery_fee": frais,
            "discount": discount,
            "total": priced.subtotal + frais - discount,
            "promotion": promotion,
            "is_orderable": priced.is_orderable,
            "unavailable_code": code,
            "unavailable_reason": motif,
        }

    @staticmethod
    def _quote_for(restaurant: Restaurant, address: Address, subtotal: Money) -> DeliveryQuote:
        """Zone et frais de la course — **par la règle unique du produit**.

        La zone est celle qui couvre l'**adresse de livraison**, pas celle du
        restaurant : c'est le point d'arrivée qui détermine ce qu'on facture et
        ce qu'on refuse de desservir.

        ## Ce que ce passage par `check_delivery` corrige

        Cette méthode choisissait la zone par `max_distance_km` croissant, tandis
        que l'écran qui annonce le devis au client (`zones/resolve/`) la
        choisissait par **surface** croissante. Les deux coïncident tant qu'une
        ville n'a qu'une zone — l'état actuel — et divergent dès qu'une zone
        « Centre-ville » est posée dans une zone « Grand Lomé » : l'application
        annonçait un tarif, la commande en appliquait un autre, et rien ne le
        signalait puisque les deux réponses étaient individuellement cohérentes.

        Les deux appellent désormais `apps.geography.resolution.resolve_zone`,
        au travers de `check_delivery` qui y ajoute l'établissement et la
        distance. Il n'y a plus qu'un endroit où la règle peut changer.
        """
        disponibilite = check_delivery(
            point=address.location, restaurant=restaurant, subtotal=subtotal
        )

        if disponibilite.quote is None:
            # Le refus est relayé **tel qu'il a été levé**, avec ses données
            # contextuelles : `min_order_amount` dit au client combien il
            # manque, `distance_km` situe l'adresse. Le remplacer par un
            # `BusinessRuleViolation` reconstruit depuis le seul message les
            # perdrait, et l'écran ne saurait plus dire que « ce n'est pas
            # possible » — c'est précisément ce qu'un test a attrapé.
            if disponibilite.refusal is not None:
                raise disponibilite.refusal
            raise BusinessRuleViolation(
                disponibilite.reason
                or "Cette adresse n'est desservie par aucune zone de livraison.",
                address_id=str(address.pk),
            )

        return disponibilite.quote

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

        if target == OrderStatus.PREPARING:
            # La cuisine allume le feu : la matière promise sort réellement du
            # stock. C'est le moment juste — `READY` serait trop tard, le plat
            # étant déjà fait, et la confirmation trop tôt, rien n'ayant encore
            # été touché.
            MaterialService.consume(
                restaurant_id=locked.restaurant_id,
                lines=_a_produire_depuis_la_commande(locked),
                reference=locked.reference,
                actor=actor,
            )
        elif target == OrderStatus.DELIVERED:
            OrderService._record_purchases(locked)
        elif target == OrderStatus.CANCELLED:
            # Le client ne doit pas perdre son code parce que le restaurant a
            # annulé : il a été décompté pour un repas qu'il n'a jamais reçu.
            PromotionService.release(order_id=locked.pk)
            # Même raisonnement pour les denrées : elles n'ont pas été servies.
            # Le rejeu est déjà écarté plus haut — une commande déjà annulée
            # sort en `is_noop`, donc rien n'est recrédité deux fois.
            StockService.restore(_quantities_by_item(locked.lines.all()))

            # La matière, elle, ne se rend que si elle n'est pas encore partie.
            #
            # Avant la préparation, elle n'était que **promise** : l'engagement
            # se libère, et le stock redevient disponible pour la commande
            # suivante. Après, elle est dans la casserole — annuler ne
            # décuisine pas un oignon, et le recréditer inventerait de la
            # matière que l'inventaire physique démentirait.
            #
            # C'est la même asymétrie que le remboursement connaît déjà : on
            # rend l'argent, jamais le travail.
            if previous in {OrderStatus.PENDING, OrderStatus.CONFIRMED}:
                MaterialService.release(
                    restaurant_id=locked.restaurant_id,
                    lines=_a_produire_depuis_la_commande(locked),
                    reference=locked.reference,
                    actor=actor,
                )

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
                "contactez El Corazón.",
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
