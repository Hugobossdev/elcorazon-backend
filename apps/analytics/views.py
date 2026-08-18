"""Points d'entrée de l'analytics.

`EventIngestView` est ouverte à tout compte authentifié — client ou livreur —
puisque les deux émettent des événements d'usage. Les rapports, eux, exigent
`analytics.read` : ce sont des chiffres d'exploitation, pas une donnée
personnelle du client qui appelle.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Sequence
from typing import Any

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from apps.accounts.models import User, UserType
from apps.accounts.serializers import CustomerStatsSerializer
from apps.analytics.reports import ReportingService
from apps.analytics.serializers import (
    CategoryRowSerializer,
    CourierPerformanceRowSerializer,
    EventWriteSerializer,
    OverviewSerializer,
    ReportQuerySerializer,
    RevenueRowSerializer,
    StatusRowSerializer,
    TopProductRowSerializer,
)
from apps.analytics.services import AnalyticsService
from common.permissions import HasPermission, active_user

__all__ = [
    "CategoryReportView",
    "CourierPerformanceReportView",
    "CustomerStatsView",
    "EventIngestView",
    "OrderStatusReportView",
    "OverviewView",
    "RevenueReportView",
    "TopProductsReportView",
]


class EventIngestView(APIView):
    """`POST /analytics/events/` — consigne un événement d'usage.

    Toujours 201 : refuser un événement mal formé n'aiderait ni le client ni
    l'exploitation, et un `event_type` inconnu d'aujourd'hui est peut-être le
    tableau de bord de demain — le fermer à la validation empêcherait de
    l'ajouter sans redéployer le serveur.
    """

    @extend_schema(request=EventWriteSerializer, responses={201: None}, tags=["analytics"])
    def post(self, request: Request) -> Response:
        serializer = EventWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        AnalyticsService.record(
            user=active_user(request),
            event_type=serializer.validated_data["event_type"],
            data=serializer.validated_data["event_data"],
            session_id=serializer.validated_data["session_id"],
        )
        return Response(status=status.HTTP_201_CREATED)


def _period(request: Request) -> tuple[dt.date, dt.date, int]:
    query = ReportQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    return query.validated_data["start"], query.validated_data["end"], query.validated_data["limit"]


#: Paramètre documenté de l'export, déclaré une fois pour les trois rapports.
#:
#: Nommé `export` et non `format` : `format` est réservé par DRF, qui s'en sert
#: à choisir un renderer et rend 404 pour une valeur qu'il ne connaît pas — le
#: rapport disparaîtrait au lieu de s'exporter.
EXPORT = OpenApiParameter(
    name="export",
    type=OpenApiTypes.STR,
    enum=["csv"],
    description=(
        "`csv` rend le même rapport en pièce jointe téléchargeable, pour reprise dans un tableur."
    ),
)


def _rendu(
    request: Request, rows: Sequence[Any], serializer: type[Serializer[Any]], nom: str
) -> Response | HttpResponse:
    """Rend un rapport en JSON, ou en CSV si la requête le demande.

    **Le CSV part du même sérialiseur que le JSON**, et pas d'une seconde
    écriture des colonnes : deux listes de champs entretenues séparément
    divergent, et l'export finit par omettre la colonne ajoutée trois mois plus
    tôt — sans que rien ne le signale, puisqu'un fichier reste produit.

    Un vrai `HttpResponse` et non une `Response` DRF : la négociation de contenu
    de DRF choisirait un renderer sur l'en-tête `Accept`, alors qu'un export
    déclenché depuis un navigateur n'en envoie pas d'utile — il faut décider sur
    le paramètre, et poser `Content-Disposition` pour que le navigateur
    télécharge au lieu d'afficher.
    """
    donnees = serializer(rows, many=True).data
    if str(request.query_params.get("export", "")).lower() != "csv":
        return Response(donnees)

    colonnes = list(serializer().fields)
    reponse = HttpResponse(content_type="text/csv; charset=utf-8")
    reponse["Content-Disposition"] = f'attachment; filename="{nom}.csv"'
    # BOM : sans lui, Excel lit un CSV UTF-8 en codage local et affiche
    # « CorazÃ³n ». Le tableur est la destination de cet export, pas un script.
    reponse.write("﻿")

    writer = csv.DictWriter(reponse, fieldnames=colonnes)
    writer.writeheader()
    writer.writerows(donnees)
    return reponse


class RevenueReportView(APIView):
    """`GET /analytics/reports/revenue/?start=&end=` — chiffre d'affaires par jour."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: RevenueRowSerializer(many=True)}, parameters=[EXPORT], tags=["analytics"]
    )
    def get(self, request: Request) -> Response | HttpResponse:
        start, end, _ = _period(request)
        rows = ReportingService.revenue_by_day(start=start, end=end)
        return _rendu(request, rows, RevenueRowSerializer, f"chiffre-affaires-{start}-{end}")


class TopProductsReportView(APIView):
    """`GET /analytics/reports/top-products/?start=&end=&limit=` — articles les plus vendus."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: TopProductRowSerializer(many=True)}, parameters=[EXPORT], tags=["analytics"]
    )
    def get(self, request: Request) -> Response | HttpResponse:
        start, end, limit = _period(request)
        rows = ReportingService.top_products(start=start, end=end, limit=limit)
        return _rendu(request, rows, TopProductRowSerializer, f"top-produits-{start}-{end}")


class CustomerStatsView(APIView):
    """`GET /analytics/reports/customers/{id}/` — fiche chiffrée d'un client.

    Sous `customers.read` et non `analytics.read` : ce n'est pas un chiffre
    d'exploitation mais le dossier d'une personne, que lit le service client
    avant de répondre au téléphone. La permission suit la donnée, pas le module
    qui l'héberge.

    Elle vit ici parce que l'agrégat croise les commandes, les adresses et la
    fidélité, et qu'`accounts` — où se trouve la fiche client — ne dépend de
    personne (ADR-002). L'y écrire ferait du socle d'identité un module qui
    connaît tout le reste.
    """

    permission_classes = [HasPermission.of("customers.read")]

    @extend_schema(responses={200: CustomerStatsSerializer}, tags=["analytics"])
    def get(self, request: Request, pk: str) -> Response:
        customer = get_object_or_404(User, pk=pk, user_type=UserType.CUSTOMER)
        stats = ReportingService.customer_stats(customer)
        return Response(CustomerStatsSerializer(stats).data)


class CourierPerformanceReportView(APIView):
    """`GET /analytics/reports/couriers/?start=&end=` — livraisons et gains par livreur."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: CourierPerformanceRowSerializer(many=True)},
        parameters=[EXPORT],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        start, end, _ = _period(request)
        rows = ReportingService.courier_performance(start=start, end=end)
        return _rendu(request, rows, CourierPerformanceRowSerializer, f"livreurs-{start}-{end}")


class OrderStatusReportView(APIView):
    """`GET /analytics/reports/orders/?start=&end=` — commandes par statut."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: StatusRowSerializer(many=True)}, parameters=[EXPORT], tags=["analytics"]
    )
    def get(self, request: Request) -> Response | HttpResponse:
        start, end, _ = _period(request)
        rows = ReportingService.orders_by_status(start=start, end=end)
        return _rendu(request, rows, StatusRowSerializer, f"commandes-{start}-{end}")


class CategoryReportView(APIView):
    """`GET /analytics/reports/categories/?start=&end=` — ventes par catégorie."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: CategoryRowSerializer(many=True)}, parameters=[EXPORT], tags=["analytics"]
    )
    def get(self, request: Request) -> Response | HttpResponse:
        start, end, _ = _period(request)
        rows = ReportingService.sales_by_category(start=start, end=end)
        return _rendu(request, rows, CategoryRowSerializer, f"categories-{start}-{end}")


class OverviewView(APIView):
    """`GET /analytics/reports/overview/?start=&end=` — chiffres de tête.

    Pas d'export CSV : une ligne unique de compteurs n'a rien à reprendre dans
    un tableur, et les rapports qui la détaillent, eux, s'exportent.
    """

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(responses={200: OverviewSerializer}, tags=["analytics"])
    def get(self, request: Request) -> Response:
        start, end, _ = _period(request)
        return Response(OverviewSerializer(ReportingService.overview(start=start, end=end)).data)
