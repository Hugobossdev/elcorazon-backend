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
from apps.analytics.perimetre import (
    Perimetre,
    PerimetreQuerySerializer,
    aujourd_hui_chez,
    resolve_perimetre,
)
from apps.analytics.reports import ReportingService
from apps.analytics.serializers import (
    CategoryRowSerializer,
    CourierPerformanceRowSerializer,
    EventWriteSerializer,
    NetworkQuerySerializer,
    NetworkRowSerializer,
    OverviewSerializer,
    ReportQuerySerializer,
    RevenueRowSerializer,
    StatusRowSerializer,
    TopProductRowSerializer,
)
from apps.analytics.services import AnalyticsService
from common.permissions import HasPermission, active_user, authenticated_user

__all__ = [
    "CategoryReportView",
    "CourierPerformanceReportView",
    "CustomerStatsView",
    "EventIngestView",
    "NetworkReportView",
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


def _period(request: Request, perimetre: Perimetre) -> tuple[dt.date, dt.date, int]:
    """La fenêtre demandée, ou la journée en cours **chez l'établissement**.

    Le périmètre est un paramètre et non une seconde lecture de la requête :
    c'est lui qui porte le fuseau, et sans lui le défaut retomberait sur
    l'horloge du serveur — UTC — c'est-à-dire sur le même genre d'erreur que
    celle qu'on corrige, un cran plus loin.
    """
    query = ReportQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    debut = query.validated_data["start"]
    fin = query.validated_data["end"]
    if debut is None or fin is None:
        debut = fin = aujourd_hui_chez(perimetre.timezone_name)
    return debut, fin, query.validated_data["limit"]


def _perimetre(request: Request) -> Perimetre:
    """Sur quels établissements porte ce rapport.

    Appelé par chaque vue de rapport plutôt que posé dans une classe de base :
    les six vues sont des `APIView` sans ancêtre commun, et en introduire un
    pour trois lignes cacherait dans une hiérarchie ce qui doit rester lisible
    au point d'appel — c'est **le** filtre qui décide de ce qu'on a le droit de
    lire.
    """
    query = PerimetreQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    # `authenticated_user` et non `active_user` : les six vues de rapport portent
    # `HasPermission`, donc l'anonyme est déjà écarté — mais `active_user` rend
    # `User | None`, et `resolve_perimetre` attend un utilisateur. Le repli sur
    # `None` aurait résolu un périmètre vide, c'est-à-dire un rapport muet, là où
    # l'on veut une erreur franche si une vue était un jour publiée sans
    # permission. C'est exactement le filet que cet assistant documente.
    return resolve_perimetre(user=authenticated_user(request), params=query.validated_data)


#: Les trois filtres de réseau, documentés une fois pour les six rapports.
#:
#: Déclarés en paramètres explicites et non par `PerimetreQuerySerializer` :
#: `extend_schema(parameters=...)` accepte les deux, mais le sérialiseur y
#: apparaîtrait comme un corps de requête sur des vues qui n'en ont pas.
PERIMETRE = [
    OpenApiParameter(
        name="country",
        type=OpenApiTypes.STR,
        description="Restreint au pays (code ISO 3166-1 alpha-2, ex. `TG`).",
    ),
    OpenApiParameter(
        name="city",
        type=OpenApiTypes.STR,
        description="Restreint à la ville (slug, ex. `lome`).",
    ),
    OpenApiParameter(
        name="restaurant",
        type=OpenApiTypes.STR,
        description=(
            "Restreint à l'établissement (slug). Se compose avec le périmètre du "
            "compte : un filtre hors périmètre rend un rapport vide, jamais les "
            "chiffres d'un établissement qu'on n'administre pas."
        ),
    ),
]


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
        responses={200: RevenueRowSerializer(many=True)},
        parameters=[EXPORT, *PERIMETRE],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        rows = ReportingService.revenue_by_day(start=start, end=end, perimetre=perimetre)
        return _rendu(request, rows, RevenueRowSerializer, f"chiffre-affaires-{start}-{end}")


class TopProductsReportView(APIView):
    """`GET /analytics/reports/top-products/?start=&end=&limit=` — articles les plus vendus."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: TopProductRowSerializer(many=True)},
        parameters=[EXPORT, *PERIMETRE],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, limit = _period(request, perimetre)
        rows = ReportingService.top_products(start=start, end=end, limit=limit, perimetre=perimetre)
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
        # Le compte du client est d'enseigne ; ses commandes appartiennent à des
        # cuisines. La fiche chiffrée porte donc le périmètre du compte qui la
        # lit, comme les six rapports d'exploitation.
        stats = ReportingService.customer_stats(
            customer,
            perimetre=resolve_perimetre(user=authenticated_user(request), params={}),
        )
        return Response(CustomerStatsSerializer(stats).data)


class CourierPerformanceReportView(APIView):
    """`GET /analytics/reports/couriers/?start=&end=` — livraisons et gains par livreur."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: CourierPerformanceRowSerializer(many=True)},
        parameters=[EXPORT, *PERIMETRE],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        rows = ReportingService.courier_performance(start=start, end=end, perimetre=perimetre)
        return _rendu(request, rows, CourierPerformanceRowSerializer, f"livreurs-{start}-{end}")


class OrderStatusReportView(APIView):
    """`GET /analytics/reports/orders/?start=&end=` — commandes par statut."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: StatusRowSerializer(many=True)},
        parameters=[EXPORT, *PERIMETRE],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        rows = ReportingService.orders_by_status(start=start, end=end, perimetre=perimetre)
        return _rendu(request, rows, StatusRowSerializer, f"commandes-{start}-{end}")


class CategoryReportView(APIView):
    """`GET /analytics/reports/categories/?start=&end=` — ventes par catégorie."""

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: CategoryRowSerializer(many=True)},
        parameters=[EXPORT, *PERIMETRE],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        rows = ReportingService.sales_by_category(start=start, end=end, perimetre=perimetre)
        return _rendu(request, rows, CategoryRowSerializer, f"categories-{start}-{end}")


class NetworkReportView(APIView):
    """`GET /analytics/reports/network/?start=&end=&level=` — le réseau de cuisines chiffré.

    `level` vaut `country`, `city`, `zone` ou `kitchen` (défaut). Les filtres de
    réseau habituels (`country`, `city`, `restaurant`) se composent avec le
    périmètre du compte, et `zone` restreint à une zone de livraison.
    """

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(
        responses={200: NetworkRowSerializer(many=True)},
        parameters=[
            EXPORT,
            *PERIMETRE,
            OpenApiParameter(
                name="level",
                type=OpenApiTypes.STR,
                enum=["country", "city", "zone", "kitchen"],
                description="Étage du réseau sur lequel regrouper. Défaut : `kitchen`.",
            ),
            OpenApiParameter(
                name="zone",
                type=OpenApiTypes.UUID,
                description="Restreint à une zone de livraison (zone figée sur la commande).",
            ),
        ],
        tags=["analytics"],
    )
    def get(self, request: Request) -> Response | HttpResponse:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        query = NetworkQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        niveau = query.validated_data["level"]
        zone = query.validated_data["zone"]
        rows = ReportingService.network(
            start=start,
            end=end,
            perimetre=perimetre,
            level=niveau,
            zone_id=zone,
        )
        return _rendu(request, rows, NetworkRowSerializer, f"reseau-{niveau}-{start}-{end}")


class OverviewView(APIView):
    """`GET /analytics/reports/overview/?start=&end=` — chiffres de tête.

    Pas d'export CSV : une ligne unique de compteurs n'a rien à reprendre dans
    un tableur, et les rapports qui la détaillent, eux, s'exportent.
    """

    permission_classes = [HasPermission.of("analytics.read")]

    @extend_schema(responses={200: OverviewSerializer}, parameters=PERIMETRE, tags=["analytics"])
    def get(self, request: Request) -> Response:
        perimetre = _perimetre(request)
        start, end, _ = _period(request, perimetre)
        rapport = ReportingService.overview(start=start, end=end, perimetre=perimetre)
        return Response(OverviewSerializer(rapport).data)
