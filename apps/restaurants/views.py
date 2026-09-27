"""Points d'entrée des établissements.

Lecture publique : le client parcourt les restaurants avant d'avoir un compte.
L'administration des établissements passera par le back-office et ses
permissions `restaurants.write` — elle n'est pas ouverte ici.
"""

from __future__ import annotations

from typing import Any

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.restaurants.delivery import check_delivery
from apps.restaurants.models import Restaurant, kitchen_state_prefetches
from apps.restaurants.serializers import (
    DeliveryCheckQuerySerializer,
    DeliveryCheckSerializer,
    NearbyQuerySerializer,
    RestaurantDetailSerializer,
    RestaurantSerializer,
)
from common.throttling import ResilientAnonRateThrottle, ResilientUserRateThrottle

__all__ = ["DeliveryCheckView", "RestaurantViewSet"]


@extend_schema(parameters=[NearbyQuerySerializer], tags=["restaurants"])
class RestaurantViewSet(ReadOnlyModelViewSet[Restaurant]):
    """Établissements actifs, avec tri par proximité facultatif.

    Le tri par distance n'est pas fait en Python : PostGIS trie sur
    `geography`, donc en mètres sur l'ellipsoïde, et l'index GiST le sert. La
    variante Python — charger tout, calculer, trier — donnerait le même
    résultat sur dix restaurants et deviendrait impraticable à mille.

    Il n'existe **pas** de filtre `open_now` : l'ouverture se calcule dans le
    fuseau de chaque pays à partir de plages dont certaines franchissent
    minuit, ce que SQL ne sait pas exprimer sans dénormaliser. Un filtre
    calculé en Python trierait après la pagination et rendrait des pages de
    tailles arbitraires — un défaut bien pire que l'absence du filtre.
    `is_open` est donc rendu sur chaque élément, et le client filtre l'écran
    qu'il affiche.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]
    lookup_field = "slug"
    filterset_fields = {
        "zone__city__slug": ["exact"],
        "zone__city__country__iso_code": ["exact"],
        "accepts_orders": ["exact"],
    }

    def get_serializer_class(self) -> type[BaseSerializer[Restaurant]]:
        # Les horaires complets ne sont utiles qu'en fiche : les charger pour
        # chaque élément d'une liste multiplierait la réponse par sept.
        return RestaurantDetailSerializer if self.action == "retrieve" else RestaurantSerializer

    def get_queryset(self) -> QuerySet[Restaurant]:
        queryset = (
            # `is_active` est la projection de `status == "active"` (voir
            # `Restaurant.save`) : seul un établissement mis en service sort
            # ici. Un brouillon, une fiche en cours de configuration ou un
            # établissement suspendu restent invisibles du public.
            #
            # La cascade sur la zone, la ville et le pays manquait : fermer un
            # marché depuis le back-office retirait ses villes de
            # `GET /geography/cities/` — `CityViewSet` la fait — mais laissait
            # ses restaurants dans `GET /restaurants/`. L'application cliente
            # affichait donc un établissement d'un pays fermé, dont la ville
            # n'existait plus pour elle : la fiche s'ouvrait, la commande
            # échouait plus loin, sans que rien n'explique pourquoi.
            Restaurant.objects.filter(
                is_active=True,
                zone__is_active=True,
                zone__city__is_active=True,
                zone__city__country__is_active=True,
            )
            .select_related("zone__city__country")
            # `is_open` interroge les plages de chaque établissement : sans ce
            # préchargement, une page de vingt restaurants ferait vingt
            # requêtes de plus.
            .prefetch_related(*kitchen_state_prefetches())
        )

        query = NearbyQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        origin = self._origin(query.validated_data)

        if origin is None:
            return queryset.order_by("name")
        return queryset.annotate(distance=Distance("location", origin)).order_by("distance")

    @staticmethod
    def _origin(params: dict[str, Any]) -> Point | None:
        if "lat" not in params:
            return None
        return Point(params["lon"], params["lat"], srid=4326)


class DeliveryCheckView(APIView):
    """`POST /restaurants/delivery-check/` — **le référentiel unique de livrabilité**.

    ## Ce que cette route remplace

    Trois applications posaient la même question et n'obtenaient chacune qu'un
    morceau de la réponse :

    * `GET /geography/zones/resolve/` rendait **la zone seule** — ni
      établissement, ni frais, ni distance, ni délai ;
    * le devis complet n'existait qu'à l'intérieur d'`OrderService`, donc
      seulement pour quelqu'un ayant déjà un panier ouvert sur un restaurant
      choisi.

    Chaque écran recomposait le reste à sa façon, et l'écart le plus grave était
    invisible : la zone retenue pour *afficher* un tarif n'était pas choisie par
    la même règle que celle retenue pour le *facturer*. Les deux passent
    désormais par `check_delivery`, et il n'existe plus qu'un endroit où cette
    règle peut changer.

    ## Pourquoi POST plutôt que GET

    Le sous-total est un montant — un objet, pas un scalaire (ADR-007) — et il
    voyage mal en paramètre d'URL. Un `GET` obligerait à l'aplatir en deux
    paramètres corrélés qu'aucune validation ne tiendrait ensemble.

    La route ne modifie rien, et son POST ne doit donc pas être lu comme une
    écriture : c'est une interrogation dont les critères sont trop structurés
    pour une chaîne de requête, comme l'autocomplétion de Places.

    ## Ouverte sans jeton

    Un visiteur doit pouvoir savoir si on le livre **avant** de créer un compte.
    C'est le même raisonnement que pour la géographie et l'annuaire : exiger une
    inscription pour répondre « non, pas encore chez vous » est le meilleur
    moyen de ne jamais revoir la personne.

    Aucune donnée personnelle n'en sort : la réponse ne parle que de zones, de
    barèmes et d'établissements, tous publics.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]

    @extend_schema(
        request=DeliveryCheckQuerySerializer,
        responses={200: DeliveryCheckSerializer},
        tags=["restaurants"],
    )
    def post(self, request: Request) -> Response:
        requete = DeliveryCheckQuerySerializer(data=request.data)
        requete.is_valid(raise_exception=True)
        donnees = requete.validated_data

        disponibilite = check_delivery(
            point=Point(donnees["lon"], donnees["lat"], srid=4326),
            restaurant=donnees.get("restaurant"),
            subtotal=donnees.get("subtotal"),
        )

        devis = disponibilite.quote
        return Response(
            DeliveryCheckSerializer(
                {
                    "is_available": disponibilite.is_available,
                    "reason": disponibilite.reason,
                    "unavailable_code": disponibilite.unavailable_code,
                    "restaurant": disponibilite.restaurant,
                    "zone": disponibilite.zone,
                    "distance_m": (
                        round(disponibilite.distance_m, 1)
                        if disponibilite.distance_m is not None
                        else None
                    ),
                    "estimated_minutes": disponibilite.estimated_minutes,
                    # Les deux montants, et pas seulement celui qu'on facture :
                    # le franco offre la course au client sans que le livreur
                    # roule gratuitement, et l'écran doit pouvoir montrer le
                    # prix barré.
                    "delivery_fee": devis.fee if devis else None,
                    "gross_delivery_fee": devis.gross_fee if devis else None,
                    "is_free_delivery": devis.is_free if devis else None,
                }
            ).data
        )
