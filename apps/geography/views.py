"""Points d'entrée de la géographie.

Ouverts sans authentification : l'application affiche le pays et la ville
**avant** l'écran d'inscription, et exiger un jeton ici forcerait à créer un
compte pour savoir si le service est disponible chez soi.

Aucun service métier — ADR-003 : ces routes vont de la vue à l'ORM. Y
intercaler une couche qui appellerait `.filter()` serait du coût de maintenance
déguisé en rigueur.
"""

from __future__ import annotations

from zoneinfo import available_timezones

from django.contrib.gis.geos import Point
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.geography.geocoding import GeocodingUnavailable, reverse_geocode
from apps.geography.models import City, Country
from apps.geography.resolution import resolve_zone
from apps.geography.serializers import (
    CitySerializer,
    CountrySerializer,
    DeliveryZoneSerializer,
    GeographyReferenceSerializer,
    ReverseGeocodeQuerySerializer,
    ReverseGeocodeSerializer,
    ZoneResolutionQuerySerializer,
    ZoneResolutionSerializer,
)
from common.money import CURRENCY_EXPONENTS
from common.permissions import HasPermission
from common.throttling import ResilientAnonRateThrottle, ResilientUserRateThrottle

__all__ = [
    "CityViewSet",
    "CountryViewSet",
    "GeographyReferenceView",
    "ReverseGeocodeView",
    "ZoneResolutionView",
]


class GeographyReferenceView(APIView):
    """`GET /geography/reference/` — les valeurs qu'un pays peut prendre.

    ## Ce que cette route corrige

    Le formulaire d'ouverture de marché du back-office portait **dix fuseaux
    horaires en dur** — Abidjan, Lomé, Accra, Porto-Novo… — et une liste de
    devises écrite à côté. Ouvrir un marché hors de ces dix demandait donc de
    modifier le code Flutter, de recompiler et de republier l'application :
    exactement l'opération de développement que le multi-pays existe pour
    supprimer.

    Pire, les deux listes pouvaient diverger de ce que le serveur accepte. Une
    devise proposée à l'écran mais absente de `CURRENCY_EXPONENTS` produisait
    un 400 après la saisie de tout le formulaire, sans dire lequel des champs
    était en cause.

    Les deux listes viennent maintenant **de la source qui les fait
    respecter** : `CURRENCY_EXPONENTS` est la table que `Money` consulte, et
    `available_timezones()` est ce que `validate_timezone` interroge. Ce que
    l'écran propose est donc, par construction, ce que le serveur acceptera.

    ## Pourquoi les fuseaux ne sont pas filtrés sur l'Afrique

    Ce serait recréer le même plafond, un cran plus loin : la liste des dix
    était déjà « ceux dont on avait besoin ». Six cents chaînes font une
    quinzaine de kilo-octets, servis une fois et mis en cache par le client ;
    le tri revient à l'écran, qui a un champ de recherche.

    Ouverte sans authentification, comme le reste de la géographie : ces
    valeurs sont des constantes publiques, et non l'état du réseau.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]

    @extend_schema(responses={200: GeographyReferenceSerializer}, tags=["geography"])
    def get(self, _request: Request) -> Response:
        return Response(
            GeographyReferenceSerializer(
                {
                    "currencies": [
                        {"code": code, "exponent": exposant}
                        for code, exposant in sorted(CURRENCY_EXPONENTS.items())
                    ],
                    "timezones": sorted(available_timezones()),
                }
            ).data
        )


class CountryViewSet(ReadOnlyModelViewSet[Country]):
    """Pays d'opération, actifs seulement.

    Un pays désactivé disparaît de l'API sans être supprimé : sa devise et son
    fuseau restent nécessaires à la lecture des commandes déjà passées là-bas.
    """

    serializer_class = CountrySerializer
    permission_classes = [AllowAny]
    queryset = Country.objects.filter(is_active=True)
    lookup_field = "iso_code"
    filterset_fields = ["currency"]


class CityViewSet(ReadOnlyModelViewSet[City]):
    """Villes desservies.

    Le filtre passe par le code ISO du pays et non par sa clé primaire : c'est
    ce que le client a en main (`TG`), et cela lui évite de retenir un UUID.
    """

    serializer_class = CitySerializer
    permission_classes = [AllowAny]
    queryset = (
        City.objects.filter(is_active=True, country__is_active=True)
        .select_related("country")
        .order_by("name")
    )
    lookup_field = "slug"
    filterset_fields = {"country__iso_code": ["exact"]}


class ZoneResolutionView(APIView):
    """`GET /geography/zones/resolve/?lat=…&lon=…` — cette adresse est-elle desservie ?

    C'est la question que pose l'application avant d'afficher un panier : elle
    conditionne les frais annoncés, le montant minimum et le délai estimé.

    **Un point hors couverture n'est pas une erreur** et ne renvoie donc pas de
    404. C'est une réponse légitime à une question légitime ; la traiter en
    erreur obligerait chaque client à ranger le cas nominal « je viens
    d'emménager hors zone » dans sa branche d'exception.

    La règle de départage ne vit **pas ici**. Elle vivait ici, et une seconde
    copie vivait dans `OrderService` — la première triait par surface, la seconde
    par `max_distance_km`. Les deux coïncidaient tant qu'une ville n'avait
    qu'une zone, et divergeaient dès qu'une zone en contenait une autre :
    l'écran annonçait un tarif, la commande en appliquait un autre, et rien ne
    le signalait puisque les deux réponses étaient individuellement cohérentes.

    Les deux appellent désormais `apps.geography.resolution.resolve_zone`. Cette
    vue n'est plus qu'une façade HTTP, et c'est ce qu'elle doit être.

    Pour la réponse **complète** — établissement, frais, distance, délai — voir
    `POST /restaurants/delivery-check/` : cette route-ci ne rend que la zone, et
    la garder telle quelle évite de casser les appelants qui n'ont besoin que
    d'elle.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ResilientAnonRateThrottle, ResilientUserRateThrottle]

    @extend_schema(
        parameters=[ZoneResolutionQuerySerializer],
        responses={200: ZoneResolutionSerializer},
        tags=["geography"],
    )
    def get(self, request: Request) -> Response:
        query = ZoneResolutionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        point = Point(query.validated_data["lon"], query.validated_data["lat"], srid=4326)
        zone = resolve_zone(point)

        return Response(
            {
                "is_covered": zone is not None,
                "zone": DeliveryZoneSerializer(zone).data if zone else None,
            }
        )


class ReverseGeocodeView(APIView):
    """`POST /geography/geocode/reverse/` — les composants d'une position.

    ## À quoi elle sert

    Le back-office pose un marqueur sur une carte ; cette route lui rend ce que
    Google sait de ce point : pays, région, ville, quartier, code postal,
    adresse formatée. L'administrateur **valide** ensuite, et c'est lui qui
    enregistre — rien n'écrase une donnée saisie à la main.

    Elle supprime la saisie de latitude et de longitude au clavier, qui était
    jusqu'ici le seul moyen de placer un établissement.

    ## Pourquoi elle est fermée, contrairement au reste de la géographie

    Les pays, les villes et la livrabilité sont publics : ce sont des faits sur
    le service. Le géocodage, lui, **consomme un quota facturé** chez un tiers.
    L'ouvrir sans jeton en ferait un proxy Google gratuit pour n'importe qui,
    et la facture arriverait sans qu'aucun écran du produit n'ait servi.

    `restaurants.write` plutôt qu'une permission dédiée : c'est exactement la
    population qui place des établissements et dessine des zones.

    ## Sans clé configurée

    503 avec une phrase qui dit quoi faire, plutôt qu'un 500 ou un objet vide
    qui ferait croire à une position sans pays.
    """

    permission_classes = [HasPermission.of("restaurants.write")]

    @extend_schema(
        request=ReverseGeocodeQuerySerializer,
        responses={200: ReverseGeocodeSerializer},
        tags=["geography"],
    )
    def post(self, request: Request) -> Response:
        requete = ReverseGeocodeQuerySerializer(data=request.data)
        requete.is_valid(raise_exception=True)
        donnees = requete.validated_data

        try:
            resultat = reverse_geocode(
                latitude=donnees["lat"],
                longitude=donnees["lon"],
                language=donnees.get("language", "fr"),
            )
        except GeocodingUnavailable as indisponible:
            # 503 et non 500 : le service **du projet** va bien, c'est sa
            # dépendance externe qui manque. La distinction change ce que fait
            # l'exploitant — vérifier une clé, plutôt que lire une trace.
            return Response(
                {
                    "type": "https://api.elcorazon.app/errors/geocoding-unavailable",
                    "title": "Géocodage indisponible",
                    "status": 503,
                    "code": "geocoding_unavailable",
                    "detail": str(indisponible),
                },
                status=503,
                content_type="application/problem+json",
            )

        return Response(ReverseGeocodeSerializer(resultat.as_dict()).data)
