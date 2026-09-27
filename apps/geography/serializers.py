"""Contrats de la géographie — ADR-006, ADR-009.

Lecture seule côté client : la hiérarchie est administrée par le back-office.
Le contour des zones (`boundary`) n'est **jamais** exposé — c'est un
`MultiPolygon` de plusieurs kilo-octets qu'aucun écran n'affiche, et le client
n'a pas à savoir *où* passe la frontière : il demande si son point est
desservi, la base répond.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.geography.models import City, Country, DeliveryZone
from common.serializers import BoundaryField, LocationField, MoneyField

__all__ = [
    "CitySerializer",
    "CountrySerializer",
    "DeliveryZoneSerializer",
    "GeographyReferenceSerializer",
    "ManagedCitySerializer",
    "ManagedCountrySerializer",
    "ManagedDeliveryZoneSerializer",
    "ReverseGeocodeQuerySerializer",
    "ReverseGeocodeSerializer",
    "ZoneResolutionQuerySerializer",
    "ZoneResolutionSerializer",
]


class CurrencySerializer(serializers.Serializer[Any]):
    """Une devise et son exposant.

    L'exposant voyage avec le code parce qu'il décide de la **saisie** : un
    montant en XOF (exposant 0) se tape en francs entiers, un montant en EUR
    (exposant 2) en euros et centimes. Sans lui, l'écran devrait redéployer la
    table `CURRENCY_EXPONENTS` de son côté — c'est-à-dire la dupliquer, et la
    laisser diverger.
    """

    code = serializers.CharField()
    exponent = serializers.IntegerField()


class GeographyReferenceSerializer(serializers.Serializer[Any]):
    """Corps de `GET /geography/reference/`."""

    currencies = CurrencySerializer(many=True)
    timezones = serializers.ListField(child=serializers.CharField())


class ReverseGeocodeQuerySerializer(serializers.Serializer[Any]):
    """Corps de `POST /geography/geocode/reverse/`."""

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    language = serializers.CharField(max_length=5, required=False, default="fr")


class ReverseGeocodeSerializer(serializers.Serializer[Any]):
    """Ce que Google sait d'une position, rangé.

    Tous les membres sont facultatifs sauf les coordonnées : au large, Google ne
    rend ni pays ni ville, et inventer une valeur serait pire que de n'en rendre
    aucune. L'écran affiche alors les champs vides, que l'administrateur remplit.
    """

    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    formatted_address = serializers.CharField(allow_null=True)
    place_id = serializers.CharField(allow_null=True)
    country = serializers.CharField(allow_null=True)
    country_code = serializers.CharField(allow_null=True)
    region = serializers.CharField(allow_null=True)
    city = serializers.CharField(allow_null=True)
    district = serializers.CharField(allow_null=True)
    postal_code = serializers.CharField(allow_null=True)
    street = serializers.CharField(allow_null=True)
    street_number = serializers.CharField(allow_null=True)


class CountrySerializer(serializers.ModelSerializer[Country]):
    # `currency_symbol` voyage à côté du code ISO parce que les deux servent à
    # des choses différentes : `XOF` est ce que `Money` compare et ce qu'un
    # contrat écrit, « FCFA » est ce qu'on montre. Le symbole était jusqu'ici
    # reconstitué par trois écrans du back-office avec la même expression
    # recopiée — `zone.currency == 'XOF' ? 'FCFA' : zone.currency` — qui ne
    # connaissait qu'une devise.
    currency_symbol = serializers.SerializerMethodField()
    centroid = LocationField(read_only=True, allow_null=True)

    class Meta:
        model = Country
        fields = [
            "id",
            "iso_code",
            "iso3_code",
            "name",
            "currency",
            "currency_symbol",
            "phone_prefix",
            "timezone",
            "default_language",
            "centroid",
        ]
        read_only_fields = fields

    def get_currency_symbol(self, obj: Country) -> str:
        """Le symbole, ou le code à défaut.

        Retomber sur le code plutôt que sur une chaîne vide : un montant sans
        aucune unité est ambigu, un montant en « XOF » ne l'est pas.
        """
        return obj.currency_symbol or obj.currency


class CitySerializer(serializers.ModelSerializer[City]):
    # Le pays est imbriqué plutôt que référencé : sans lui le client ne connaît
    # pas la devise, et il ne peut donc pas formater un seul prix sans un
    # second appel.
    country = CountrySerializer(read_only=True)
    centroid = LocationField(read_only=True)

    class Meta:
        model = City
        fields = ["id", "name", "slug", "country", "centroid"]
        read_only_fields = fields


class DeliveryZoneSerializer(serializers.ModelSerializer[DeliveryZone]):
    city = CitySerializer(read_only=True)
    base_fee = MoneyField(read_only=True)
    fee_per_km = MoneyField(read_only=True)
    free_delivery_threshold = MoneyField(read_only=True)
    min_order_amount = MoneyField(read_only=True)

    class Meta:
        model = DeliveryZone
        fields = [
            "id",
            "name",
            "city",
            "base_fee",
            "fee_per_km",
            "free_delivery_threshold",
            "min_order_amount",
            "max_distance_km",
            "estimated_delivery_minutes",
        ]
        read_only_fields = fields


# --------------------------------------------------------------- back-office


class ManagedCountrySerializer(serializers.ModelSerializer[Country]):
    """Pays vu de l'exploitation.

    `currency` reste modifiable, et c'est un piège qu'il faut connaître : les
    montants déjà écrits portent leur propre devise (ADR-007), donc rien ne se
    convertit rétroactivement. Changer la devise d'un pays en activité
    produirait un catalogue et un historique dans deux unités. La colonne est
    donc laissée ouverte pour l'ouverture d'un marché, pas pour sa correction.
    """

    centroid = LocationField(required=False, allow_null=True)

    class Meta:
        model = Country
        fields = [
            "id",
            "iso_code",
            "iso3_code",
            "name",
            "currency",
            "currency_symbol",
            "phone_prefix",
            "timezone",
            "default_language",
            "centroid",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedCitySerializer(serializers.ModelSerializer[City]):
    country = serializers.SlugRelatedField[Country](
        slug_field="iso_code", queryset=Country.objects.all()
    )
    centroid = LocationField()

    class Meta:
        model = City
        fields = [
            "id",
            "country",
            "name",
            "slug",
            "centroid",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ManagedDeliveryZoneSerializer(serializers.ModelSerializer[DeliveryZone]):
    """Zone et son barème — le seul endroit où se décide un frais de livraison.

    Il remplace la constante contradictoire de l'implémentation précédente
    (`5.00` d'un côté, `500.0` de l'autre), et le barème vit **en donnée** :
    ouvrir un quartier, relever le forfait d'une zone excentrée ou offrir la
    livraison au-dessus d'un seuil se font depuis cet écran, sans déploiement.
    """

    city = serializers.PrimaryKeyRelatedField[City](queryset=City.objects.all())

    # **Le contour peut être envoyé, ou déduit.** Trois écritures le produisent,
    # et aucune n'a été retirée :
    #
    # * `boundary` — un GeoJSON complet. C'est le contrat d'origine, et il reste
    #   le bon chemin pour un contour administratif importé d'un outil
    #   cartographique ;
    # * `polygon_coordinates` — des sommets bruts, ce que rend une carte sur
    #   laquelle on vient de dessiner ;
    # * `center` + `radius_meters` — un disque, discrétisé par
    #   `apps.geography.shapes`.
    #
    # Le rendre en lecture seule aurait cassé les appelants existants pour
    # n'apporter qu'une uniformité de façade. Ce qui compte est ailleurs : la
    # discrétisation d'un cercle vit **une fois**, côté serveur, plutôt que dans
    # chaque écran qui en dessine un — deux écrans en auraient eu deux
    # différentes, donc deux zones pour la même saisie.
    boundary = BoundaryField(required=False)

    #: Sommets d'un contour tracé à la main, en `[[lon, lat], …]` — l'ordre
    #: GeoJSON, celui que produisent les outils de cartographie.
    polygon_coordinates = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField(), min_length=2, max_length=2),
        write_only=True,
        required=False,
        help_text="Sommets [[lon, lat], …] — pour une zone de type `polygon`.",
    )

    # Rattachement d'établissement : **lisible ici, écrit ailleurs.**
    #
    # Le rendre inscriptible demanderait un jeu de requête sur `Restaurant`,
    # donc un import que `geography` n'a pas le droit de faire — l'arête
    # `restaurants → geography` existe déjà, et l'inverse fermerait un cycle que
    # le test d'architecture refuse (et il l'a refusé : c'est ainsi que ce champ
    # a trouvé sa place).
    #
    # L'écriture vit donc du côté qui a le droit de connaître les deux :
    # `ManagedRestaurantZoneViewSet`, dans `apps.restaurants`. Une traversée
    # d'attribut n'est pas un import : celle-ci ne coûte rien au graphe.
    restaurant = serializers.CharField(source="restaurant.slug", read_only=True, allow_null=True)

    center = LocationField(required=False, allow_null=True)
    base_fee = MoneyField()
    fee_per_km = MoneyField()
    free_delivery_threshold = MoneyField(required=False, allow_null=True)
    min_order_amount = MoneyField(required=False, allow_null=True)

    #: Zones actives dont le contour recoupe celui-ci.
    #:
    #: **Un avertissement, jamais un refus.** Une zone « Centre-ville » posée
    #: dans une zone « Grand Lomé » est la façon normale d'exprimer une exception
    #: tarifaire, et la résolution sait laquelle l'emporte. L'écran le signale
    #: pour que la décision soit consciente.
    overlaps = serializers.SerializerMethodField()

    class Meta:
        model = DeliveryZone
        fields = [
            "id",
            "city",
            "restaurant",
            "name",
            "shape",
            "boundary",
            "polygon_coordinates",
            "center",
            "radius_meters",
            "priority",
            "base_fee",
            "fee_per_km",
            "free_delivery_threshold",
            "min_order_amount",
            "max_distance_km",
            "estimated_delivery_minutes",
            "is_active",
            "overlaps",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "restaurant", "overlaps", "created_at", "updated_at"]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_overlaps(self, obj: DeliveryZone) -> list[str]:
        # Seulement à la lecture d'une zone existante : sur une création, il n'y
        # a pas encore de contour en base à comparer.
        if obj.pk is None:
            return []
        return list(
            DeliveryZone.objects.filter(
                boundary__intersects=obj.boundary,
                is_active=True,
                city__country=obj.city.country_id,
            )
            .exclude(pk=obj.pk)
            .order_by("name")
            .values_list("name", flat=True)[:10]
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Le barème est libellé dans la devise du pays.

        La devise n'est pas choisie au niveau de la zone : elle est héritée
        (ADR-006). Un forfait en euros sur une zone d'un pays en francs CFA ne
        se verrait qu'au calcul des frais, c'est-à-dire au passage de commande
        d'un client — en 500, et sur le chemin du chiffre d'affaires.
        """
        instance = self.instance
        city = attrs.get("city") or (instance.city if instance else None)
        if city is None:  # pragma: no cover - `city` est obligatoire à la création
            return attrs

        devise = city.country.currency
        for champ in ("base_fee", "fee_per_km", "free_delivery_threshold", "min_order_amount"):
            montant = attrs.get(champ)
            if montant is not None and montant.currency != devise:
                raise serializers.ValidationError(
                    {champ: f"Ce pays facture en {devise} ; montant reçu en {montant.currency}."}
                )

        attrs["boundary"] = self._contour(attrs)
        return attrs

    def _contour(self, attrs: dict[str, Any]) -> Any:
        """Construit le `MultiPolygon` depuis le mode de saisie demandé.

        **Le contour n'est jamais envoyé, il est déduit.** Un back-office qui
        devrait produire lui-même le polygone d'un disque porterait sa propre
        discrétisation, et deux écrans en auraient deux différentes — donc deux
        zones différentes pour la même saisie. La conversion vit dans
        `apps.geography.shapes`, une fois.

        Sur une modification qui ne touche pas à la géométrie, le contour
        existant est conservé : corriger un tarif ne doit pas obliger à
        redessiner.
        """
        from apps.geography.models import ZoneShape
        from apps.geography.shapes import check_boundary, circle_to_boundary, polygon_to_boundary

        forme = attrs.get("shape") or (self.instance.shape if self.instance else None)
        sommets = attrs.pop("polygon_beacon", None) or attrs.pop("polygon_coordinates", None)

        # `absent` distingue « le champ n'était pas dans la requête » — auquel
        # cas on garde ce que porte l'instance — de « le champ valait `null` »,
        # qui efface. Un sentinelle est nécessaire parce que `None` est une
        # valeur légitime des deux côtés ; le confondre avec l'absence ferait
        # perdre le centre d'un cercle à la première correction de tarif.
        absent = object()
        centre = attrs.get("center", absent)
        rayon = attrs.get("radius_meters", absent)

        if forme == ZoneShape.CIRCLE:
            if centre is absent:
                centre = self.instance.center if self.instance else None
            if rayon is absent:
                rayon = self.instance.radius_meters if self.instance else None
            if centre is None or not rayon:
                raise serializers.ValidationError(
                    {
                        "radius_meters": (
                            "Une zone circulaire demande un centre et un rayon strictement positif."
                        )
                    }
                )
            attrs["center"], attrs["radius_meters"] = centre, rayon
            disque = circle_to_boundary(centre, float(rayon))
            try:
                check_boundary(disque)
            except ValueError as erreur:
                raise serializers.ValidationError({"radius_meters": str(erreur)}) from erreur
            return disque

        # Polygone et zone administrative partagent la même représentation : des
        # sommets. Ce qui les distingue est l'outil de saisie — l'un se trace à
        # la main, l'autre vient d'un contour administratif importé — et c'est
        # `shape` qui le retient, pour que l'écran rouvre le bon éditeur.
        attrs["center"], attrs["radius_meters"] = None, None

        if sommets is not None:
            try:
                return polygon_to_boundary(sommets)
            except ValueError as erreur:
                raise serializers.ValidationError({"polygon_coordinates": str(erreur)}) from erreur

        # Un GeoJSON complet — le contrat d'origine, et le bon chemin pour un
        # contour administratif produit ailleurs.
        contour = attrs.get("boundary")
        if contour is not None:
            try:
                check_boundary(contour)
            except ValueError as erreur:
                raise serializers.ValidationError({"boundary": str(erreur)}) from erreur
            return contour

        # Modification qui ne touche pas à la géométrie : corriger un tarif ne
        # doit pas obliger à redessiner.
        if self.instance is not None:
            return self.instance.boundary

        raise serializers.ValidationError(
            {
                "boundary": (
                    "Un contour est nécessaire : envoyez `boundary` en GeoJSON, "
                    "`polygon_coordinates` en [[lon, lat], …], ou `shape=circle` "
                    "avec `center` et `radius_meters`."
                )
            }
        )


class ZoneResolutionQuerySerializer(serializers.Serializer[Any]):
    """Paramètres de `GET /geography/zones/resolve/`.

    Déclarés en sérialiseur plutôt que lus à la main : la validation des bornes
    est faite une fois, et `drf-spectacular` documente les paramètres depuis
    cette classe au lieu d'une annotation manuelle qui se périme.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class ZoneResolutionSerializer(serializers.Serializer[Any]):
    """Réponse de la résolution.

    `is_covered` est redondant avec `zone is not null` — délibérément. Le
    booléen est ce que le client teste, et il reste juste si la réponse gagne
    un jour un cas « couvert mais momentanément suspendu ».
    """

    is_covered = serializers.BooleanField(read_only=True)
    zone = DeliveryZoneSerializer(read_only=True, allow_null=True)
