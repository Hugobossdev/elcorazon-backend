"""Rapports agrégés — lecture seule, jamais de table dédiée.

Chaque rapport interroge directement les commandes, leurs lignes ou les
courses : ce sont elles la source de vérité du chiffre d'affaires, du produit
qui se vend et du livreur qui livre. Les recalculer à la demande coûte une
requête d'agrégation ; les dupliquer dans des tables de reporting coûterait un
second endroit où « le chiffre d'affaires » peut ne plus être le même chiffre
que celui des commandes.

Bornés en dates : un rapport sans fenêtre finirait par agréger toute la vie de
la plateforme à chaque appel, de plus en plus lentement à mesure qu'elle
grandit.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate

from apps.accounts.models import User, UserType
from apps.analytics.perimetre import Perimetre, fenetre_metier
from apps.catalog.models import MenuItem
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.loyalty.models import PointsAccount
from apps.orders.models import Order, OrderLine
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from common.money import Money

__all__ = [
    "NETWORK_LEVELS",
    "CategoryRow",
    "CourierPerformanceRow",
    "CurrencyRevenue",
    "CustomerStats",
    "NetworkRow",
    "Overview",
    "ReportingService",
    "RevenueRow",
    "StatusRow",
    "TopProductRow",
]


# **Chaque montant porte sa devise.** Un périmètre qui couvre le Togo et le
# Cameroun encaisse des XOF et des XAF : deux monnaies distinctes, même à
# parité, qu'aucune ligne n'additionne. Les séries sont donc découpées par
# devise, comme le rapport réseau l'était déjà (`NetworkRow`) ; c'est l'écran
# qui choisit laquelle il montre, jamais le serveur qui les mêle.


@dataclass(frozen=True, slots=True)
class RevenueRow:
    day: dt.date
    currency: str
    orders_count: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class TopProductRow:
    menu_item_id: str
    item_name: str
    currency: str
    quantity_sold: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class CourierPerformanceRow:
    courier_id: str
    courier_name: str
    currency: str
    deliveries: int
    earnings_minor: int


@dataclass(frozen=True, slots=True)
class StatusRow:
    """Commandes par statut — un **compte**, sans montant.

    La ligne portait `revenue_minor`, somme de toutes les commandes du statut :
    sur un périmètre à deux devises, c'était une addition de XOF et de XAF, et
    aucun écran ne la lisait. Le chiffre d'affaires a ses propres rapports,
    découpés par devise.
    """

    status: str
    orders_count: int


@dataclass(frozen=True, slots=True)
class CategoryRow:
    category_id: str
    category_name: str
    currency: str
    quantity_sold: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class CurrencyRevenue:
    """Chiffre d'affaires livré d'une devise, sur la fenêtre de l'aperçu."""

    currency: str
    orders_delivered: int
    revenue_minor: int
    average_basket_minor: int


#: Les quatre étages du réseau, et ce qui identifie une ligne à chacun.
#:
#: Tous lus sur la géographie **figée** de la commande (`Order.country`,
#: `city`, `delivery_zone`) : une cuisine rattachée ailleurs depuis ne fait pas
#: migrer son chiffre d'affaires d'une ville à l'autre. La cuisine, elle, est
#: une clé stable — son nom est lu tel qu'il est aujourd'hui.
NETWORK_LEVELS: dict[str, tuple[str, str]] = {
    "country": ("country_id", "country__name"),
    "city": ("city_id", "city__name"),
    "zone": ("delivery_zone_id", "delivery_zone_name"),
    "kitchen": ("restaurant_id", "restaurant__name"),
}


@dataclass(frozen=True, slots=True)
class NetworkRow:
    """Une ligne du rapport réseau : un pays, une ville, une zone ou une cuisine.

    `revenue_minor` porte sur les commandes **livrées** seulement — ce qui est
    encaissé —, `orders_count` sur tout ce qui a été commandé dans la fenêtre :
    la différence est ce qu'on vient chercher. Une ligne par devise : on
    n'additionne pas des francs CFA et des nairas.
    """

    key: str
    name: str
    city: str
    country: str
    currency: str
    orders_count: int
    in_progress_count: int
    delivered_count: int
    cancelled_count: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class Overview:
    """Instantané du tableau de bord.

    Deux natures de chiffres cohabitent ici, et c'est assumé : les commandes et
    le chiffre d'affaires portent sur la **fenêtre demandée**, tandis que la
    carte et la flotte sont des états **du moment** — un article disponible
    l'est aujourd'hui, pas « entre le 1er et le 15 ». Les borner comme le reste
    n'aurait pas de sens ; les rendre sans fenêtre agrégerait toute la vie de la
    plateforme à chaque affichage.
    """

    orders_count: int
    orders_delivered: int
    orders_cancelled: int
    #: Chiffre d'affaires et panier moyen **quand le périmètre n'encaisse
    #: qu'une devise** (`currency`), nuls sinon : les rendre sur un périmètre à
    #: deux devises reviendrait à additionner des XOF et des XAF. `revenues`
    #: porte le détail, une ligne par devise, dans tous les cas.
    revenue_minor: int | None
    average_basket_minor: int | None
    currency: str | None
    revenues: list[CurrencyRevenue]
    customers_count: int
    couriers_online: int
    menu_items_available: int
    menu_items_total: int

    #: La fenêtre effectivement agrégée, et le fuseau dans lequel elle a été
    #: découpée.
    #:
    #: Republiées parce que l'écran ne les connaît plus : il envoyait deux dates
    #: calculées sur l'horloge du **poste** du back-office, si bien qu'un siège
    #: consultant à minuit et demi demandait les chiffres d'une journée qui
    #: n'avait pas commencé chez la cuisine. Il peut désormais ne rien envoyer,
    #: et c'est le serveur qui dit de quelle journée il parle.
    #:
    #: `timezone_certain` est faux quand le périmètre traverse plusieurs
    #: fuseaux : « la journée » n'y a pas de sens unique, et l'écran doit le dire
    #: plutôt que d'afficher une date qui ne vaut pour personne.
    start: dt.date
    end: dt.date
    timezone_name: str
    timezone_certain: bool


@dataclass(frozen=True, slots=True)
class CustomerStats:
    """Fiche chiffrée d'un client.

    Sans borne de dates, contrairement aux autres rapports : c'est la valeur
    d'un compte depuis son ouverture qu'on lit avant de décider d'un geste
    commercial, et elle porte sur les commandes d'**une** personne — pas sur
    toute la vie de la plateforme.
    """

    orders_count: int
    orders_delivered: int
    orders_cancelled: int
    total_spent: Money
    average_basket: Money
    first_order_at: dt.datetime | None
    last_order_at: dt.datetime | None
    addresses_count: int
    loyalty_balance: int
    loyalty_lifetime_earned: int


class ReportingService:
    @staticmethod
    def customer_stats(customer: User, *, perimetre: Perimetre) -> CustomerStats:
        """Agrège le dossier d'un client en une requête d'agrégation.

        Le total et le panier moyen ne comptent que les commandes **livrées** :
        une commande annulée n'a rien encaissé, et une commande en cours n'a
        rien encaissé *encore*. Les inclure ferait d'un client qui annule tout
        un client à forte valeur.

        Le panier moyen est calculé ici et non côté client : sur une liste
        paginée, une moyenne faite à l'écran ne porte que sur la page affichée
        et change quand on tourne la page.

        **Cloisonné comme les six autres rapports.** Le lot du 8 septembre a
        porté le périmètre dans tous les agrégats et a oublié celui-ci, qui ne
        partait pas de la même vue : un gérant de Lomé lisait donc le nombre de
        commandes et la dépense totale d'un client **tous pays confondus**, et
        rappelait un habitué d'Abidjan en le croyant sien. Le compte client,
        lui, reste d'enseigne (`CustomerViewSet`) : ce sont ses **commandes**
        qui appartiennent à une cuisine, pas lui.

        Les points de fidélité et les adresses ne sont pas cloisonnables — ils
        n'appartiennent à aucun établissement — et restent rendus tels quels.
        """
        commandes = Order.objects.filter(customer=customer)
        if perimetre.restaurant_ids is not None:
            commandes = commandes.filter(restaurant_id__in=perimetre.restaurant_ids)

        agregat = commandes.aggregate(
            total=Count("id"),
            livrees=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
            annulees=Count("id", filter=Q(status=OrderStatus.CANCELLED)),
            depense=Sum("total_minor", filter=Q(status=OrderStatus.DELIVERED)),
            premiere=Min("placed_at"),
            derniere=Max("placed_at"),
        )

        livrees = agregat["livrees"]
        depense = agregat["depense"] or 0
        # La devise est celle des commandes du client, pas une constante : un
        # même compte peut commander dans deux pays (ADR-006). On prend celle de
        # sa dernière commande, et le défaut de configuration seulement s'il n'en
        # a aucune — auquel cas le montant est nul et la devise n'affiche rien.
        devise = (
            commandes.order_by("-placed_at").values_list("total_currency", flat=True).first()
            or settings.DEFAULT_CURRENCY
        )

        points = PointsAccount.objects.filter(user=customer).first()

        return CustomerStats(
            orders_count=agregat["total"],
            orders_delivered=livrees,
            orders_cancelled=agregat["annulees"],
            total_spent=Money(depense, devise),
            # Division entière : un panier moyen en unité mineure n'a pas de
            # sous-unité à répartir, et arrondir au franc près est la précision
            # de la monnaie elle-même.
            average_basket=Money(depense // livrees if livrees else 0, devise),
            first_order_at=agregat["premiere"],
            last_order_at=agregat["derniere"],
            addresses_count=Address.objects.filter(user=customer).count(),
            loyalty_balance=points.balance if points else 0,
            loyalty_lifetime_earned=points.lifetime_earned if points else 0,
        )

    @staticmethod
    def revenue_by_day(*, start: dt.date, end: dt.date, perimetre: Perimetre) -> list[RevenueRow]:
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        rows = (
            Order.objects.filter(
                status=OrderStatus.DELIVERED,
                delivered_at__gte=debut,
                delivered_at__lt=fin,
                **perimetre.filtre("restaurant_id"),
            )
            # Le regroupement se fait dans le **même** fuseau que les bornes :
            # trancher la fenêtre chez la cuisine puis grouper les jours en UTC
            # rendrait des lignes coupées au milieu de la nuit locale.
            .annotate(day=TruncDate("delivered_at", tzinfo=ZoneInfo(perimetre.timezone_name)))
            .values("day", "total_currency")
            .annotate(orders_count=Count("id"), revenue_minor=Sum("total_minor"))
            .order_by("day", "total_currency")
        )
        return [
            RevenueRow(
                day=row["day"],
                currency=row["total_currency"],
                orders_count=row["orders_count"],
                revenue_minor=row["revenue_minor"],
            )
            for row in rows
        ]

    @staticmethod
    def top_products(
        *, start: dt.date, end: dt.date, perimetre: Perimetre, limit: int = 10
    ) -> list[TopProductRow]:
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        rows = (
            OrderLine.objects.filter(
                order__status=OrderStatus.DELIVERED,
                order__delivered_at__gte=debut,
                order__delivered_at__lt=fin,
                **perimetre.filtre("order__restaurant_id"),
            )
            .values("menu_item_id", "item_name", "line_total_currency")
            .annotate(quantity_sold=Sum("quantity"), revenue_minor=Sum("line_total_minor"))
            .order_by("-quantity_sold")[:limit]
        )
        return [
            TopProductRow(
                menu_item_id=str(row["menu_item_id"]),
                item_name=row["item_name"],
                currency=row["line_total_currency"],
                quantity_sold=row["quantity_sold"],
                revenue_minor=row["revenue_minor"],
            )
            for row in rows
        ]

    @staticmethod
    def orders_by_status(*, start: dt.date, end: dt.date, perimetre: Perimetre) -> list[StatusRow]:
        """Répartition des commandes par statut sur la fenêtre.

        Sur `placed_at` et non `delivered_at`, contrairement au chiffre
        d'affaires : la question est « qu'est devenu ce qui a été commandé
        cette semaine ». Dater sur la livraison ferait disparaître du décompte
        les commandes annulées, qui ne sont jamais livrées — et c'est
        précisément ce qu'on vient regarder.
        """
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        rows = (
            Order.objects.filter(
                placed_at__gte=debut,
                placed_at__lt=fin,
                **perimetre.filtre("restaurant_id"),
            )
            .values("status")
            .annotate(orders_count=Count("id"))
            .order_by("-orders_count")
        )
        return [StatusRow(status=row["status"], orders_count=row["orders_count"]) for row in rows]

    @staticmethod
    def sales_by_category(
        *, start: dt.date, end: dt.date, perimetre: Perimetre
    ) -> list[CategoryRow]:
        """Ventes agrégées par catégorie de la carte.

        La jointure passe par l'article, seul chemin vers la catégorie : la
        ligne de commande garde le nom de l'article au moment de l'achat
        (`item_name`) mais pas sa catégorie, parce qu'un article peut changer de
        rayon sans que la commande passée en soit affectée.
        """
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        rows = (
            OrderLine.objects.filter(
                order__status=OrderStatus.DELIVERED,
                order__delivered_at__gte=debut,
                order__delivered_at__lt=fin,
                menu_item__isnull=False,
                **perimetre.filtre("order__restaurant_id"),
            )
            .values("menu_item__category_id", "menu_item__category__name", "line_total_currency")
            .annotate(quantity_sold=Sum("quantity"), revenue_minor=Sum("line_total_minor"))
            .order_by("-revenue_minor")
        )
        return [
            CategoryRow(
                category_id=str(row["menu_item__category_id"]),
                category_name=row["menu_item__category__name"],
                currency=row["line_total_currency"],
                quantity_sold=row["quantity_sold"],
                revenue_minor=row["revenue_minor"] or 0,
            )
            for row in rows
        ]

    @staticmethod
    def network(
        *,
        start: dt.date,
        end: dt.date,
        perimetre: Perimetre,
        level: str,
        zone_id: uuid.UUID | None = None,
    ) -> list[NetworkRow]:
        """Commandes et chiffre d'affaires par pays, ville, zone ou cuisine — une requête.

        Datées sur `placed_at`, comme la répartition par statut : la question
        est « qu'est devenu ce qui a été commandé ici cette semaine ». Le
        périmètre du compte s'applique d'abord, sur les cuisines ; `zone_id`
        affine ensuite, sur la zone figée de la commande.

        Une commande antérieure que la reprise n'a pas su situer tombe dans une
        ligne sans clé plutôt que de disparaître : un total qui ne retombe pas
        sur celui des commandes serait une réponse fausse.
        """
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        cle, libelle = NETWORK_LEVELS[level]
        commandes = Order.objects.filter(
            placed_at__gte=debut,
            placed_at__lt=fin,
            **perimetre.filtre("restaurant_id"),
        )
        if zone_id:
            commandes = commandes.filter(delivery_zone_id=zone_id)

        en_cours = [
            s for s in OrderStatus.values if s not in {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
        ]
        rows = (
            commandes.values(cle, libelle, "city__name", "country__iso_code", "total_currency")
            .annotate(
                orders_count=Count("id"),
                in_progress_count=Count("id", filter=Q(status__in=en_cours)),
                delivered_count=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
                cancelled_count=Count("id", filter=Q(status=OrderStatus.CANCELLED)),
                revenue_minor=Sum("total_minor", filter=Q(status=OrderStatus.DELIVERED)),
            )
            .order_by("-orders_count")
        )

        # Au niveau du pays et de la ville, la ville (ou le pays) de la ligne
        # n'a pas à se répéter dans la colonne voisine.
        return [
            NetworkRow(
                key=str(row[cle]) if row[cle] is not None else "",
                name=row[libelle] or "Non situé",
                city="" if level == "country" else (row["city__name"] or ""),
                country=row["country__iso_code"] or "",
                currency=row["total_currency"],
                orders_count=row["orders_count"],
                in_progress_count=row["in_progress_count"],
                delivered_count=row["delivered_count"],
                cancelled_count=row["cancelled_count"],
                revenue_minor=row["revenue_minor"] or 0,
            )
            for row in rows
        ]

    @staticmethod
    def overview(*, start: dt.date, end: dt.date, perimetre: Perimetre) -> Overview:
        """Chiffres de tête du tableau de bord, en trois requêtes d'agrégation.

        L'écran précédent en obtenait autant en **téléchargeant toutes les
        lignes** — commandes, comptes, articles, livreurs — pour les compter
        dans le navigateur. Le tableau de bord ralentissait à mesure que la
        plateforme grandissait, et les totaux dépendaient de ce que la
        pagination avait rendu.
        """
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        commandes = Order.objects.filter(
            placed_at__gte=debut,
            placed_at__lt=fin,
            **perimetre.filtre("restaurant_id"),
        )
        agregat = commandes.aggregate(
            total=Count("id"),
            livrees=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
            annulees=Count("id", filter=Q(status=OrderStatus.CANCELLED)),
        )
        # Le chiffre d'affaires par devise : une requête groupée, pas une
        # somme globale. Trié du plus gros au plus petit pour que l'écran
        # puisse mettre en tête la devise dominante du périmètre.
        revenus = [
            CurrencyRevenue(
                currency=ligne["total_currency"],
                orders_delivered=ligne["livrees"],
                revenue_minor=ligne["chiffre"] or 0,
                average_basket_minor=(ligne["chiffre"] or 0) // ligne["livrees"],
            )
            for ligne in commandes.filter(status=OrderStatus.DELIVERED)
            .order_by()
            .values("total_currency")
            .annotate(livrees=Count("id"), chiffre=Sum("total_minor"))
            .order_by("-chiffre", "total_currency")
        ]
        unique = revenus[0] if len(revenus) == 1 else None

        catalogue = (
            MenuItem.objects.alive()
            .filter(**perimetre.filtre("restaurant_id"))
            .aggregate(total=Count("id"), disponibles=Count("id", filter=Q(is_available=True)))
        )

        return Overview(
            orders_count=agregat["total"],
            orders_delivered=agregat["livrees"],
            orders_cancelled=agregat["annulees"],
            # Une seule devise — ou aucune livraison : zéro n'additionne rien.
            # Plusieurs : nul, et `revenues` dit tout.
            revenue_minor=unique.revenue_minor if unique else (None if revenus else 0),
            average_basket_minor=(
                unique.average_basket_minor if unique else (None if revenus else 0)
            ),
            currency=unique.currency if unique else None,
            revenues=revenus,
            customers_count=ReportingService._clients_du_perimetre(perimetre),
            # Les trois termes de L1, et pas le seul `is_online`. Le tableau de
            # bord intitule ce nombre « Livreurs actifs » : ce que le
            # superviseur y lit, c'est combien de livreurs peuvent prendre une
            # course à cet instant. `is_online` seul est une **déclaration** du
            # livreur — un dossier en attente peut la faire, un dossier suspendu
            # la conserve — et comptait donc des livreurs à qui le serveur
            # refuse toute course. Le chiffre annonçait une capacité de
            # livraison qui n'existait pas, ce qui se voit au pire moment :
            # celui où on décide d'accepter un afflux de commandes.
            couriers_online=CourierProfile.objects.filter(
                is_online=True,
                verification_status=VerificationStatus.APPROVED,
                user__is_active=True,
                **perimetre.filtre("restaurant_id"),
            ).count(),
            menu_items_available=catalogue["disponibles"],
            menu_items_total=catalogue["total"],
            start=start,
            end=end,
            timezone_name=perimetre.timezone_name,
            timezone_certain=perimetre.timezone_est_certain,
        )

    @staticmethod
    def _clients_du_perimetre(perimetre: Perimetre) -> int:
        """Clients actifs rattachés à ce périmètre.

        La définition est la même dans les deux cas — « les clients de ce
        périmètre » — seule la façon de l'établir change, parce qu'un client n'a
        pas de clé vers un établissement : il commande où il veut, et peut
        commander dans deux pays.

        Sur l'enseigne entière, tout client actif en fait partie. Restreint, le
        rattachement se lit sur ses commandes : est client de cet établissement
        celui qui y a commandé. Pas de borne de date, pour rester parallèle au
        décompte global qui n'en a pas non plus : les deux répondent « combien
        de clients », pas « combien ont commandé cette semaine » — c'est
        `orders_count` qui répond à celle-là.
        """
        clients = User.objects.filter(user_type=UserType.CUSTOMER, is_active=True)
        if not perimetre.is_global:
            clients = clients.filter(
                pk__in=Order.objects.filter(**perimetre.filtre("restaurant_id")).values(
                    "customer_id"
                )
            )
        return clients.count()

    @staticmethod
    def courier_performance(
        *, start: dt.date, end: dt.date, perimetre: Perimetre
    ) -> list[CourierPerformanceRow]:
        debut, fin = fenetre_metier(start=start, end=end, timezone_name=perimetre.timezone_name)
        rows = (
            Assignment.objects.filter(
                status=DeliveryStatus.DELIVERED,
                delivered_at__gte=debut,
                delivered_at__lt=fin,
                **perimetre.filtre("order__restaurant_id"),
            )
            # La devise de la commande, qui est celle de la rémunération : un
            # livreur de Douala est payé en XAF, celui de Lomé en XOF.
            .values("courier_id", "courier__user__full_name", "order__total_currency")
            .annotate(deliveries=Count("id"), earnings_minor=Sum("courier_fee_minor"))
            .order_by("-deliveries")
        )
        return [
            CourierPerformanceRow(
                courier_id=str(row["courier_id"]),
                courier_name=row["courier__user__full_name"],
                currency=row["order__total_currency"],
                deliveries=row["deliveries"],
                earnings_minor=row["earnings_minor"] or 0,
            )
            for row in rows
        ]
