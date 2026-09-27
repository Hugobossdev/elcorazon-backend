"""Point d'entrée de la recherche transverse — `/api/v1/search/`."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.search.services import SearchService
from common.permissions import IsStaff, authenticated_user

__all__ = ["SearchView"]


class SearchQuerySerializer(serializers.Serializer[Any]):
    q = serializers.CharField(min_length=3, max_length=120)
    limit = serializers.IntegerField(min_value=1, max_value=10, required=False, default=5)


class SearchHitSerializer(serializers.Serializer[Any]):
    kind = serializers.CharField(read_only=True)
    id = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    subtitle = serializers.CharField(read_only=True)


class SearchView(APIView):
    """`GET /search/?q=…&limit=…` — un champ, plusieurs familles de résultats.

    Réservée au **personnel** : les familles qu'elle traverse sont celles du
    back-office, et un client n'a rien à y chercher — il a sa carte et ses
    commandes.

    Le filtrage par droit est dans le service, pas ici : chaque famille exige sa
    propre permission, et une famille interdite est **absente** de la réponse
    plutôt que vide. La nuance compte pour qui lit l'écran : « rien trouvé » et
    « vous n'y avez pas accès » ne se corrigent pas de la même façon.
    """

    permission_classes = [IsStaff]

    @extend_schema(
        parameters=[SearchQuerySerializer],
        responses={200: SearchHitSerializer(many=True)},
        tags=["search"],
    )
    def get(self, request: Request) -> Response:
        query = SearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        resultats = SearchService.search(
            user=authenticated_user(request),
            query=query.validated_data["q"],
            limit=query.validated_data["limit"],
        )
        return Response(SearchHitSerializer(resultats, many=True).data)
