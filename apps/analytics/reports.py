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
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate

from apps.accounts.models import User, UserType
from apps.catalog.models import MenuItem
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.loyalty.models import PointsAccount
from apps.orders.models import Order, OrderLine
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from common.money import Money

__all__ = [
    "CategoryRow",
    "CourierPerformanceRow",
    "CustomerStats",
    "Overview",
    "ReportingService",
    "RevenueRow",
    "StatusRow",
    "TopProductRow",
]


@dataclass(frozen=True, slots=True)
class RevenueRow:
    day: dt.date
    orders_count: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class TopProductRow:
    menu_item_id: str
    item_name: str
    quantity_sold: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class CourierPerformanceRow:
    courier_id: str
    courier_name: str
    deliveries: int
    earnings_minor: int


@dataclass(frozen=True, slots=True)
class StatusRow:
    status: str
    orders_count: int
    revenue_minor: int


@dataclass(frozen=True, slots=True)
class CategoryRow:
    category_id: str
    category_name: str
    quantity_sold: int
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
    revenue_minor: int
    average_basket_minor: int
    customers_count: int
    couriers_online: int
    menu_items_available: int
    menu_items_total: int


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
    def customer_stats(customer: User) -> CustomerStats:
        """Agrège le dossier d'un client en une requête d'agrégation.

        Le total et le panier moyen ne comptent que les commandes **livrées** :
        une commande annulée n'a rien encaissé, et une commande en cours n'a
        rien encaissé *encore*. Les inclure ferait d'un client qui annule tout
        un client à forte valeur.

        Le panier moyen est calculé ici et non côté client : sur une liste
        paginée, une moyenne faite à l'écran ne porte que sur la page affichée
        et change quand on tourne la page.
        """
        agregat = Order.objects.filter(customer=customer).aggregate(
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
            Order.objects.filter(customer=customer)
            .order_by("-placed_at")
            .values_list("total_currency", flat=True)
            .first()
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
    def revenue_by_day(*, start: dt.date, end: dt.date) -> list[RevenueRow]:
        rows = (
            Order.objects.filter(
                status=OrderStatus.DELIVERED, delivered_at__date__range=(start, end)
            )
            .annotate(day=TruncDate("delivered_at"))
            .values("day")
            .annotate(orders_count=Count("id"), revenue_minor=Sum("total_minor"))
            .order_by("day")
        )
        return [
            RevenueRow(
                day=row["day"], orders_count=row["orders_count"], revenue_minor=row["revenue_minor"]
            )
            for row in rows
        ]

    @staticmethod
    def top_products(*, start: dt.date, end: dt.date, limit: int = 10) -> list[TopProductRow]:
        rows = (
            OrderLine.objects.filter(
                order__status=OrderStatus.DELIVERED,
                order__delivered_at__date__range=(start, end),
            )
            .values("menu_item_id", "item_name")
            .annotate(quantity_sold=Sum("quantity"), revenue_minor=Sum("line_total_minor"))
            .order_by("-quantity_sold")[:limit]
        )
        return [
            TopProductRow(
                menu_item_id=str(row["menu_item_id"]),
                item_name=row["item_name"],
                quantity_sold=row["quantity_sold"],
                revenue_minor=row["revenue_minor"],
            )
            for row in rows
        ]

    @staticmethod
    def orders_by_status(*, start: dt.date, end: dt.date) -> list[StatusRow]:
        """Répartition des commandes par statut sur la fenêtre.

        Sur `placed_at` et non `delivered_at`, contrairement au chiffre
        d'affaires : la question est « qu'est devenu ce qui a été commandé
        cette semaine ». Dater sur la livraison ferait disparaître du décompte
        les commandes annulées, qui ne sont jamais livrées — et c'est
        précisément ce qu'on vient regarder.
        """
        rows = (
            Order.objects.filter(placed_at__date__range=(start, end))
            .values("status")
            .annotate(orders_count=Count("id"), revenue_minor=Sum("total_minor"))
            .order_by("-orders_count")
        )
        return [
            StatusRow(
                status=row["status"],
                orders_count=row["orders_count"],
                revenue_minor=row["revenue_minor"] or 0,
            )
            for row in rows
        ]

    @staticmethod
    def sales_by_category(*, start: dt.date, end: dt.date) -> list[CategoryRow]:
        """Ventes agrégées par catégorie de la carte.

        La jointure passe par l'article, seul chemin vers la catégorie : la
        ligne de commande garde le nom de l'article au moment de l'achat
        (`item_name`) mais pas sa catégorie, parce qu'un article peut changer de
        rayon sans que la commande passée en soit affectée.
        """
        rows = (
            OrderLine.objects.filter(
                order__status=OrderStatus.DELIVERED,
                order__delivered_at__date__range=(start, end),
                menu_item__isnull=False,
            )
            .values("menu_item__category_id", "menu_item__category__name")
            .annotate(quantity_sold=Sum("quantity"), revenue_minor=Sum("line_total_minor"))
            .order_by("-revenue_minor")
        )
        return [
            CategoryRow(
                category_id=str(row["menu_item__category_id"]),
                category_name=row["menu_item__category__name"],
                quantity_sold=row["quantity_sold"],
                revenue_minor=row["revenue_minor"] or 0,
            )
            for row in rows
        ]

    @staticmethod
    def overview(*, start: dt.date, end: dt.date) -> Overview:
        """Chiffres de tête du tableau de bord, en trois requêtes d'agrégation.

        L'écran précédent en obtenait autant en **téléchargeant toutes les
        lignes** — commandes, comptes, articles, livreurs — pour les compter
        dans le navigateur. Le tableau de bord ralentissait à mesure que la
        plateforme grandissait, et les totaux dépendaient de ce que la
        pagination avait rendu.
        """
        commandes = Order.objects.filter(placed_at__date__range=(start, end)).aggregate(
            total=Count("id"),
            livrees=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
            annulees=Count("id", filter=Q(status=OrderStatus.CANCELLED)),
            chiffre=Sum("total_minor", filter=Q(status=OrderStatus.DELIVERED)),
        )
        livrees = commandes["livrees"]
        chiffre = commandes["chiffre"] or 0

        catalogue = MenuItem.objects.alive().aggregate(
            total=Count("id"), disponibles=Count("id", filter=Q(is_available=True))
        )

        return Overview(
            orders_count=commandes["total"],
            orders_delivered=livrees,
            orders_cancelled=commandes["annulees"],
            revenue_minor=chiffre,
            average_basket_minor=chiffre // livrees if livrees else 0,
            customers_count=User.objects.filter(
                user_type=UserType.CUSTOMER, is_active=True
            ).count(),
            couriers_online=CourierProfile.objects.filter(is_online=True).count(),
            menu_items_available=catalogue["disponibles"],
            menu_items_total=catalogue["total"],
        )

    @staticmethod
    def courier_performance(*, start: dt.date, end: dt.date) -> list[CourierPerformanceRow]:
        rows = (
            Assignment.objects.filter(
                status=DeliveryStatus.DELIVERED, delivered_at__date__range=(start, end)
            )
            .values("courier_id", "courier__user__full_name")
            .annotate(deliveries=Count("id"), earnings_minor=Sum("courier_fee_minor"))
            .order_by("-deliveries")
        )
        return [
            CourierPerformanceRow(
                courier_id=str(row["courier_id"]),
                courier_name=row["courier__user__full_name"],
                deliveries=row["deliveries"],
                earnings_minor=row["earnings_minor"] or 0,
            )
            for row in rows
        ]
