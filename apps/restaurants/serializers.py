"""Contrats des établissements — ADR-006, ADR-009.

Trois booléens sortent séparément — `is_open`, `accepts_orders`,
`can_order_now` — au lieu d'un seul « disponible ». C'est ce qui permet à
l'application de dire *pourquoi* : « fermé, ouvre à 11 h » n'est pas
« débordé, réessayez dans dix minutes », et les deux n'appellent pas le même
geste de la part du client.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import Role, User, UserType
from apps.geography.models import City, Country, DeliveryZone
from apps.geography.serializers import DeliveryZoneSerializer, ManagedDeliveryZoneSerializer
from apps.restaurants.availability import KitchenState, kitchen_state
from apps.restaurants.duplication import known_sections
from apps.restaurants.models import (
    AreaMembership,
    KitchenClosure,
    OpeningHours,
    Restaurant,
    StaffMembership,
    zone_anchoring_problem,
)
from apps.restaurants.states import RestaurantStatus
from common.audit import AuditEntry
from common.availability import Unavailability
from common.serializers import LocationField, MoneyField

__all__ = [
    "DeliveryCheckQuerySerializer",
    "DeliveryCheckSerializer",
    "ManagedOpeningHoursSerializer",
    "ManagedRestaurantSerializer",
    "ManagedRestaurantZoneSerializer",
    "NearbyQuerySerializer",
    "OpeningHoursSerializer",
    "RestaurantDetailSerializer",
    "RestaurantDuplicationSerializer",
    "RestaurantPerimeterSerializer",
    "RestaurantSerializer",
    "RestaurantStatusTransitionSerializer",
    "StaffSerializer",
]


class OpeningHoursSerializer(serializers.ModelSerializer[OpeningHours]):
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpeningHours
        fields = ["id", "weekday", "opens_at", "closes_at", "crosses_midnight"]
        read_only_fields = fields


class RestaurantSerializer(serializers.ModelSerializer[Restaurant]):
    """Forme de liste.

    `distance_m` n'apparaît que si la requête portait un point de référence :
    l'annotation est absente sinon, et inventer un `0` ou un `null` ferait
    croire à une proximité qu'on n'a pas mesurée.
    """

    location = LocationField(read_only=True)
    city = serializers.CharField(source="zone.city.name", read_only=True)

    # Le **slug** de la ville et le code du pays, à côté de leurs libellés.
    #
    # L'application cliente en a besoin pour deux choses qu'elle écrivait
    # jusqu'ici en dur : rattacher une adresse à la bonne ville
    # (`AddressSerializer.city` attend une clé, que le client résout par slug)
    # et borner l'autocomplétion de lieux au bon pays. Sans eux, elle portait
    # `citySlug = 'lome'` et `countryCode = 'tg'` dans ses constantes — donc un
    # second établissement ailleurs aurait enregistré ses adresses à Lomé.
    #
    # Le nom seul ne suffit pas : « Lomé » ne se compare pas à une clé, et
    # deux villes homonymes de deux pays ne se distinguent que par là.
    city_slug = serializers.CharField(source="zone.city.slug", read_only=True)
    country = serializers.CharField(source="zone.city.country.iso_code", read_only=True)
    phone_prefix = serializers.CharField(source="zone.city.country.phone_prefix", read_only=True)

    currency = serializers.CharField(read_only=True)
    delivery_fee_from = MoneyField(source="zone.base_fee", read_only=True)
    estimated_delivery_minutes = serializers.IntegerField(
        source="zone.estimated_delivery_minutes", read_only=True
    )

    is_open = serializers.SerializerMethodField()
    can_order_now = serializers.SerializerMethodField()
    # Pourquoi `can_order_now` est faux — vides sinon. Le code est stable
    # (`common.availability.UnavailabilityCode`) ; la phrase est affichable.
    unavailable_code = serializers.SerializerMethodField()
    unavailable_reason = serializers.SerializerMethodField()
    # « Fermé — réouverture demain à 11 h 00 ». L'instant, et sa phrase composée
    # dans le fuseau du pays — jamais sur le téléphone, dont l'horloge et le
    # fuseau ne sont pas ceux de la cuisine. Nuls quand la cuisine est ouverte
    # ou que sa réouverture sort de l'horizon connu.
    is_temporarily_closed = serializers.SerializerMethodField()
    reopens_at = serializers.SerializerMethodField()
    reopens_label = serializers.SerializerMethodField()
    distance_m = serializers.SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "address",
            "location",
            "city",
            "city_slug",
            "country",
            "phone_prefix",
            "phone",
            "cover_image",
            "currency",
            "delivery_fee_from",
            "estimated_delivery_minutes",
            "default_preparation_minutes",
            "is_open",
            "accepts_orders",
            "can_order_now",
            "unavailable_code",
            "unavailable_reason",
            "is_temporarily_closed",
            "reopens_at",
            "reopens_label",
            "distance_m",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def _verdict(self, obj: Restaurant) -> Unavailability | None:
        """Le verdict du juge, calculé **une fois** par établissement et par réponse.

        Trois champs le lisent ; le recalculer trois fois relirait trois fois
        les plages d'ouverture — et, à la seconde d'une ouverture, pourrait
        rendre `can_order_now` vrai et `unavailable_code` non vide.
        """
        return self._etat(obj).unavailability

    def _etat(self, obj: Restaurant) -> KitchenState:
        """L'état de la cuisine, relevé une fois par établissement et par réponse."""
        cache: dict[object, KitchenState] = self.context.setdefault("kitchen_states", {})
        if obj.pk not in cache:
            cache[obj.pk] = kitchen_state(obj, self._now())
        return cache[obj.pk]

    def _now(self) -> dt.datetime:
        """Instant de référence, calculé **une fois** par réponse.

        Sans cette mise en cache, une liste de vingt restaurants appellerait
        vingt fois `timezone.now()` et pourrait, à la seconde près, se retrouver
        à cheval sur une ouverture — deux établissements de la même ville
        rendus dans deux états contradictoires.
        """
        now: dt.datetime = self.context.setdefault("now", timezone.now())
        return now

    def get_is_open(self, obj: Restaurant) -> bool:
        return obj.is_open_at(self._now())

    def get_can_order_now(self, obj: Restaurant) -> bool:
        # La composition vivait ici, et **seulement** ici : la création de
        # commande ne la lisait pas. Elle vit désormais dans le juge, que la
        # commande consulte aussi — l'écran et le serveur ne peuvent plus dire
        # deux choses différentes.
        return self._verdict(obj) is None

    def get_unavailable_code(self, obj: Restaurant) -> str:
        verdict = self._verdict(obj)
        return str(verdict.code) if verdict is not None else ""

    def get_unavailable_reason(self, obj: Restaurant) -> str:
        verdict = self._verdict(obj)
        return verdict.message if verdict is not None else ""

    def get_is_temporarily_closed(self, obj: Restaurant) -> bool:
        return self._etat(obj).is_temporarily_closed

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_reopens_at(self, obj: Restaurant) -> str | None:
        instant = self._etat(obj).reopens_at
        return instant.isoformat() if instant is not None else None

    def get_reopens_label(self, obj: Restaurant) -> str:
        return self._etat(obj).reopens_label

    def get_distance_m(self, obj: Restaurant) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.m, 1) if distance is not None else None


class RestaurantDetailSerializer(RestaurantSerializer):
    opening_hours = OpeningHoursSerializer(many=True, read_only=True)

    class Meta(RestaurantSerializer.Meta):
        fields = [*RestaurantSerializer.Meta.fields, "email", "opening_hours"]
        read_only_fields = fields


class DeliveryCheckQuerySerializer(serializers.Serializer[Any]):
    """Corps de `POST /restaurants/delivery-check/`.

    Une position, et deux facultatifs qui changent la question posée :

    * `restaurant` la restreint à *cet* établissement — le cas du panier déjà
      ouvert, où en changer changerait le catalogue et les prix. Omis, le
      serveur choisit le plus proche qui dessert, ce qui évite au client de
      désigner une cuisine que la géographie détermine ;
    * `subtotal` déclenche la tarification. Sans lui, la réponse dit si
      l'adresse est desservie sans chiffrer : un montant minimum ou un franco
      ne veulent rien dire face à un panier vide.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    subtotal = MoneyField(required=False, allow_null=True)


class DeliveryCheckSerializer(serializers.Serializer[Any]):
    """Réponse de la vérification de livrabilité.

    Rendue **entière dans tous les cas** — y compris quand la réponse est non.
    L'écran a besoin de savoir *pourquoi* pour proposer le bon geste : changer
    d'adresse, ajouter un article, ou attendre l'ouverture. Un corps réduit à
    `{"available": false}` obligerait chacune des trois applications à inventer
    son message, et elles en inventeraient trois différents.
    """

    is_available = serializers.BooleanField()
    reason = serializers.CharField(allow_null=True)
    # Motif stable du refus : `no_kitchen_available`, `address_not_served`, ou
    # le code d'un refus de panier. L'application compare ce code, jamais
    # `reason`. L'état de la cuisine, lui, voyage dans `restaurant`
    # (`can_order_now`, `unavailable_code`) : être livrable et pouvoir
    # commander maintenant sont deux réponses.
    unavailable_code = serializers.CharField(allow_null=True)

    restaurant = RestaurantSerializer(allow_null=True)
    zone = DeliveryZoneSerializer(allow_null=True)

    distance_m = serializers.FloatField(allow_null=True)
    estimated_minutes = serializers.IntegerField(allow_null=True)

    delivery_fee = MoneyField(allow_null=True)
    gross_delivery_fee = MoneyField(allow_null=True)
    is_free_delivery = serializers.BooleanField(allow_null=True)


class NearbyQuerySerializer(serializers.Serializer[Any]):
    """Point de référence facultatif de `GET /restaurants/`.

    Les deux coordonnées vont ensemble : une latitude seule ne situe rien, et
    l'accepter en silence produirait un tri par proximité à une dimension —
    faux, mais plausible à la lecture.
    """

    lat = serializers.FloatField(min_value=-90, max_value=90, required=False)
    lon = serializers.FloatField(min_value=-180, max_value=180, required=False)

    def validate(self, attrs: dict[str, float]) -> dict[str, float]:
        if ("lat" in attrs) != ("lon" in attrs):
            raise serializers.ValidationError("lat et lon se fournissent ensemble.")
        return attrs


# --------------------------------------------------------------- back-office


class ManagedRestaurantSerializer(serializers.ModelSerializer[Restaurant]):
    """Établissement vu de l'exploitation.

    `is_active` et `accepts_orders` y sont tous les deux, et les confondre
    serait perdre l'information : le premier dit si l'établissement existe, le
    second s'il prend des commandes maintenant. Un coup de feu en cuisine se
    règle avec le second ; le premier ferait disparaître le restaurant de
    l'application.

    La devise et le fuseau n'y figurent pas : ils sont hérités du pays à
    travers la zone (ADR-006), et les rendre saisissables ici permettrait à
    deux établissements du même marché de facturer dans deux unités.
    """

    zone = serializers.PrimaryKeyRelatedField[DeliveryZone](queryset=DeliveryZone.objects.all())
    location = LocationField()
    currency = serializers.CharField(read_only=True)
    timezone = serializers.CharField(read_only=True)

    # Rendus pour situer l'établissement sans un second appel : l'écran qui
    # liste les établissements d'un réseau affiche « Abidjan, CI », et le
    # recomposer côté client demanderait de charger la zone, puis la ville, puis
    # le pays — trois appels pour deux mots.
    city = serializers.CharField(source="zone.city.name", read_only=True)
    city_slug = serializers.CharField(source="zone.city.slug", read_only=True)
    country = serializers.CharField(source="zone.city.country.iso_code", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True)

    configuration_gaps = serializers.SerializerMethodField()

    # **Ce que voit le client, à cet instant** — le verdict du juge que la
    # commande consulte.
    #
    # `status` dit la décision de l'exploitation ; il ne dit pas si quelqu'un
    # peut commander. Une cuisine « En service » dont la ville a été désactivée
    # est invisible de l'application cliente, et une cuisine « En service » hors
    # de ses horaires refuse toute commande. Le back-office affichait les deux
    # comme en service, sans rien de plus : configurée ici, introuvable là-bas,
    # et personne pour le voir.
    is_open = serializers.SerializerMethodField()
    can_order_now = serializers.SerializerMethodField()
    unavailable_code = serializers.SerializerMethodField()
    unavailable_reason = serializers.SerializerMethodField()
    is_temporarily_closed = serializers.SerializerMethodField()
    closure_reason = serializers.SerializerMethodField()
    reopens_at = serializers.SerializerMethodField()
    reopens_label = serializers.SerializerMethodField()

    # Plafond des pertes et corrections de stock passant sans seconde validation.
    # Nul : toutes demandent une validation — voir le modèle.
    stock_adjustment_ceiling = MoneyField(required=False, allow_null=True)

    # Compteurs d'exploitation — la ligne de tableau du back-office (§6).
    #
    # Annotés par la vue, jamais calculés ici : un `SerializerMethodField` qui
    # ferait `obj.orders.count()` déclencherait trois requêtes **par
    # établissement**, soit trente sur une liste de dix. Le défaut ne se verrait
    # pas en développement, avec un seul restaurant et une base vide.
    #
    # `default=0` couvre le cas où le sérialiseur est utilisé hors de cette vue
    # — la réponse d'une transition de statut, par exemple, qui rend l'objet tel
    # qu'il vient d'être sauvé et n'a donc pas d'annotation.
    orders_count = serializers.IntegerField(read_only=True, default=0)
    couriers_count = serializers.IntegerField(read_only=True, default=0)
    menu_items_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "zone",
            "zone_name",
            "city",
            "city_slug",
            "country",
            "address",
            "location",
            "phone",
            "email",
            "cover_image",
            "currency",
            "timezone",
            "status",
            "configuration_gaps",
            "is_active",
            "accepts_orders",
            "is_open",
            "can_order_now",
            "unavailable_code",
            "unavailable_reason",
            "is_temporarily_closed",
            "closure_reason",
            "reopens_at",
            "reopens_label",
            "default_preparation_minutes",
            "auto_dispatch_couriers",
            "stock_adjustment_ceiling",
            "orders_count",
            "couriers_count",
            "menu_items_count",
            "created_at",
            "updated_at",
        ]
        # `status` se lit ici et s'écrit sur `POST manage/{slug}/status/` : la
        # machine à états et la vérification de complétude ne peuvent pas vivre
        # dans un `PATCH` qui accepte aussi le numéro de téléphone. Un
        # établissement se publierait alors en corrigeant une faute de frappe.
        #
        # `is_active` est dérivé de `status` (voir `Restaurant.save`) : le
        # laisser inscriptible offrirait un second levier de publication, dont
        # la prochaine écriture du modèle annulerait l'effet en silence.
        read_only_fields = [
            "id",
            "zone_name",
            "city",
            "city_slug",
            "country",
            "currency",
            "timezone",
            "status",
            "configuration_gaps",
            "is_active",
            "is_open",
            "can_order_now",
            "unavailable_code",
            "unavailable_reason",
            "is_temporarily_closed",
            "closure_reason",
            "reopens_at",
            "reopens_label",
            "orders_count",
            "couriers_count",
            "menu_items_count",
            "created_at",
            "updated_at",
        ]

    def get_configuration_gaps(self, obj: Restaurant) -> list[str]:
        """Ce qui manque pour ouvrir — la liste qu'affiche l'écran de validation.

        Calculée à la lecture plutôt que stockée : elle dépend du catalogue, des
        horaires et de la flotte, qui changent sans passer par l'établissement.
        Une colonne dénormalisée serait fausse dès la première catégorie
        supprimée ailleurs.
        """
        return obj.configuration_gaps()

    def _etat(self, obj: Restaurant) -> KitchenState:
        """L'état de la cuisine, relevé **une fois** par ligne et par réponse.

        Quatre champs le lisent ; le relever quatre fois pourrait, à la seconde
        d'une ouverture, rendre `can_order_now` vrai et `unavailable_code` non
        vide — la même précaution que `RestaurantSerializer._verdict`.
        """
        cache: dict[object, KitchenState] = self.context.setdefault("kitchen_states", {})
        if obj.pk not in cache:
            instant: dt.datetime = self.context.setdefault("now", timezone.now())
            cache[obj.pk] = kitchen_state(obj, instant)
        return cache[obj.pk]

    def get_is_open(self, obj: Restaurant) -> bool:
        return self._etat(obj).is_open

    def get_can_order_now(self, obj: Restaurant) -> bool:
        return self._etat(obj).can_accept_orders

    def get_unavailable_code(self, obj: Restaurant) -> str:
        verdict = self._etat(obj).unavailability
        return str(verdict.code) if verdict is not None else ""

    def get_unavailable_reason(self, obj: Restaurant) -> str:
        verdict = self._etat(obj).unavailability
        return verdict.message if verdict is not None else ""

    def get_is_temporarily_closed(self, obj: Restaurant) -> bool:
        return self._etat(obj).is_temporarily_closed

    def get_closure_reason(self, obj: Restaurant) -> str:
        return self._etat(obj).closure_reason

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_reopens_at(self, obj: Restaurant) -> str | None:
        instant = self._etat(obj).reopens_at
        return instant.isoformat() if instant is not None else None

    def get_reopens_label(self, obj: Restaurant) -> str:
        return self._etat(obj).reopens_label

    def validate_zone(self, zone: DeliveryZone) -> DeliveryZone:
        """Une cuisine se pose sur une zone municipale, ou sur l'une des siennes.

        Refusé à la saisie plutôt que laissé à `configuration_gaps` : une zone
        propre à un autre établissement n'est pas un manque qu'on comble plus
        tard, c'est un rattachement faux dès l'écriture.
        """
        instance = self.instance if isinstance(self.instance, Restaurant) else None
        if (
            probleme := zone_anchoring_problem(zone, instance.pk if instance else None)
        ) is not None:
            raise serializers.ValidationError(probleme)
        return zone

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Le plafond se tient dans la devise de l'établissement.

        La devise vient du pays, à travers la zone — celle de la requête si elle
        change, sinon celle de l'établissement. Un plafond en euros sur une
        cuisine de Lomé se comparerait à des pertes en francs par leur seul
        nombre, et laisserait passer 655 fois trop.
        """
        plafond = attrs.get("stock_adjustment_ceiling")
        if plafond is not None:
            zone = attrs.get("zone") or (self.instance.zone if self.instance else None)
            if zone is not None and plafond.currency != zone.city.country.currency:
                raise serializers.ValidationError(
                    {
                        "stock_adjustment_ceiling": (
                            f"Le plafond se fixe en {zone.city.country.currency}, "
                            "la devise de l'établissement."
                        )
                    }
                )
            if plafond.amount_minor < 0:
                raise serializers.ValidationError(
                    {"stock_adjustment_ceiling": "Un plafond ne peut pas être négatif."}
                )
        return attrs


class RestaurantPerimeterSerializer(serializers.ModelSerializer[Restaurant]):
    """Ce qu'un membre du personnel sait de **ses** établissements.

    Distinct de `ManagedRestaurantSerializer`, et c'est le point : lire son
    propre rattachement n'est pas administrer le réseau. Le poste de cuisine a
    besoin du nom, du slug, de la devise, du fuseau, de la position et du délai
    de préparation de la cuisine où il travaille — pas des compteurs
    d'exploitation, des lacunes de configuration ni du plafond des pertes, que
    `restaurants.read` continue de réserver à qui gère.

    Sans cette forme, un « Opérateur » (sans `restaurants.read`) ouvrait le
    poste de cuisine et lisait « Aucun établissement rattaché » : son 403 sur
    la liste de gestion était pris pour une absence de rattachement.
    """

    zone_name = serializers.CharField(source="zone.name", read_only=True)
    city = serializers.CharField(source="zone.city.name", read_only=True)
    city_slug = serializers.CharField(source="zone.city.slug", read_only=True)
    country = serializers.CharField(source="zone.city.country.iso_code", read_only=True)
    location = LocationField(read_only=True)
    currency = serializers.CharField(read_only=True)
    timezone = serializers.CharField(read_only=True)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "zone",
            "zone_name",
            "city",
            "city_slug",
            "country",
            "address",
            "location",
            "phone",
            "currency",
            "timezone",
            "status",
            "is_active",
            "accepts_orders",
            "default_preparation_minutes",
        ]
        read_only_fields = fields


class RestaurantStatusTransitionSerializer(serializers.Serializer[Any]):
    """Corps de `POST /restaurants/manage/{slug}/status/`.

    Un seul champ, et c'est voulu : la cible. Les règles d'enchaînement vivent
    dans la machine (`RESTAURANT_MACHINE`) et la complétude dans le modèle, si
    bien qu'il n'y a rien à valider ici qu'une valeur du registre — le reste se
    décide sur l'objet, que ce sérialiseur ne voit pas.
    """

    status = serializers.ChoiceField(choices=RestaurantStatus.choices)


class RestaurantDuplicationSerializer(serializers.Serializer[Any]):
    """Corps de `POST /restaurants/manage/{slug}/duplicate/`.

    ## Pourquoi tant de champs obligatoires

    Un établissement dupliqué est un établissement **neuf** : il a sa propre
    adresse, ses propres coordonnées, son propre numéro. Les hériter de la
    source produirait une fiche qui semble complète et pointe sur une autre
    ville — le pire état possible, parce qu'il passe la validation et se
    découvre à la première course.

    `zone` est ce qui emporte tout le reste : la ville, le pays, la devise et
    le fuseau (ADR-006). C'est aussi pourquoi les zones ne se dupliquent pas —
    voir `apps.restaurants.duplication`.

    ## `sections` vide est une réponse valable

    Dupliquer la seule fiche, sans carte ni horaires, sert à ouvrir une
    succursale qui aura son propre menu. Ce n'est pas un cas dégénéré : c'est
    le raccourci « repartir de la même identité de marque ».
    """

    name = serializers.CharField(max_length=120)
    slug = serializers.SlugField(max_length=120)
    zone = serializers.PrimaryKeyRelatedField[DeliveryZone](queryset=DeliveryZone.objects.all())
    address = serializers.CharField()
    location = LocationField()
    phone = serializers.CharField(max_length=16)
    email = serializers.EmailField(required=False, allow_blank=True, default="")

    sections = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text=(
            "Ce qui est recopié : "
            + ", ".join(f"`{nom}`" for nom in sorted(known_sections()))
            + ". Commandes, clients, livreurs, paiements et statistiques ne "
            "sont copiables par aucune valeur — il n'existe pas de section "
            "pour eux."
        ),
    )

    def validate_zone(self, zone: DeliveryZone) -> DeliveryZone:
        """Même règle que la création : pas sur la zone propre d'une autre cuisine."""
        if (probleme := zone_anchoring_problem(zone, None)) is not None:
            raise serializers.ValidationError(probleme)
        return zone

    def validate_slug(self, value: str) -> str:
        """Un slug déjà pris est refusé ici, pas par la base.

        Sans ce contrôle, la contrainte d'unicité remonterait en 500 au milieu
        de la transaction de duplication — après la création des catégories,
        donc avec un message qui ne dit rien de ce qu'il faut corriger.
        """
        if Restaurant.objects.filter(slug=value).exists():
            raise serializers.ValidationError(
                f"Le slug « {value} » est déjà pris par un autre établissement."
            )
        return value

    def validate_sections(self, value: list[str]) -> list[str]:
        inconnues = sorted(set(value) - known_sections())
        if inconnues:
            raise serializers.ValidationError(
                "Sections inconnues : "
                + ", ".join(inconnues)
                + ". Attendues : "
                + ", ".join(sorted(known_sections()))
                + "."
            )
        return value


class ManagedKitchenClosureSerializer(serializers.ModelSerializer[KitchenClosure]):
    """Fermeture exceptionnelle d'une cuisine — `/restaurants/manage/closures/`.

    ## Le fuseau est celui de la cuisine, et c'est le serveur qui le tient

    Les horaires d'ouverture se saisissent en heure de la cuisine — `11:00`
    veut dire onze heures **là-bas**, et le serveur les compare dans le fuseau
    du pays. Les fermetures, elles, voyageaient en instants absolus que le
    back-office fabriquait à partir de l'horloge du **poste** : un siège à Lomé
    (UTC+0) qui fermait Douala (UTC+1) le 25 décembre à minuit fermait en
    réalité à une heure du matin, heure de Douala. Deux conventions pour deux
    champs voisins du même écran.

    D'où [starts_at_local] et [ends_at_local] : une heure **murale**, sans
    décalage — « 2026-12-25T00:00 » — que le serveur situe dans le fuseau de la
    cuisine concernée, parce qu'il est le seul à le connaître de façon sûre.
    Elles sont rendues en lecture sous la même forme, pour que l'écran affiche
    ce que l'exploitant a saisi sans jamais convertir.

    `starts_at`/`ends_at` restent acceptés **avec leur décalage**, pour un
    appelant qui sait ce qu'il fait ; les deux formes ne se mélangent pas.
    """

    restaurant = serializers.PrimaryKeyRelatedField[Restaurant](queryset=Restaurant.objects.all())
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True)
    is_current = serializers.SerializerMethodField()
    # Rendus par méthode, acceptés par `to_internal_value` : ce ne sont pas des
    # champs du modèle, et les déclarer en écriture les ferait entrer dans la
    # validation de `DateTimeField`, qui lit un instant **absolu** — exactement
    # ce qu'on cherche à ne plus demander à l'appelant.
    starts_at_local = serializers.SerializerMethodField()
    ends_at_local = serializers.SerializerMethodField()
    timezone_name = serializers.CharField(source="restaurant.timezone", read_only=True)

    class Meta:
        model = KitchenClosure
        fields = [
            "id",
            "restaurant",
            "restaurant_name",
            "starts_at",
            "ends_at",
            "starts_at_local",
            "ends_at_local",
            "timezone_name",
            "reason",
            "is_current",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "restaurant_name",
            "is_current",
            "created_at",
            "timezone_name",
            "starts_at_local",
            "ends_at_local",
        ]
        extra_kwargs = {
            # Facultatifs : l'appelant fournit **soit** la paire absolue, soit
            # la paire locale. `validate` refuse l'absence des deux.
            "starts_at": {"required": False},
            "ends_at": {"required": False},
        }

    def get_is_current(self, obj: KitchenClosure) -> bool:
        maintenant: dt.datetime = self.context.setdefault("now", timezone.now())
        return obj.starts_at <= maintenant < obj.ends_at

    @extend_schema_field(serializers.CharField)
    def get_starts_at_local(self, obj: KitchenClosure) -> str:
        """Le début, **en heure de la cuisine**.

        Sans lui, chaque écran refait la conversion — et le back-office la
        faisait avec l'horloge du poste, ce qui décale l'affichage d'une
        fermeture dès qu'on supervise un pays voisin.
        """
        return obj.starts_at.astimezone(ZoneInfo(obj.restaurant.timezone)).isoformat()

    @extend_schema_field(serializers.CharField)
    def get_ends_at_local(self, obj: KitchenClosure) -> str:
        return obj.ends_at.astimezone(ZoneInfo(obj.restaurant.timezone)).isoformat()

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        """Situe une heure murale dans le fuseau de la cuisine visée.

        La conversion a lieu **avant** la validation du modèle : tout ce qui
        suit — la contrainte d'ordre, le refus d'une fermeture déjà finie —
        travaille sur des instants absolus, comme auparavant.
        """
        brut = dict(data) if isinstance(data, dict) else data
        locaux: dict[str, Any] = {
            champ: brut.pop(f"{champ}_local", None)
            for champ in ("starts_at", "ends_at")
            if isinstance(brut, dict)
        }
        valeurs: dict[str, Any] = super().to_internal_value(brut)

        if not any(locaux.values()):
            return valeurs

        restaurant = valeurs.get("restaurant") or getattr(self.instance, "restaurant", None)
        if restaurant is None:
            raise serializers.ValidationError(
                {"restaurant": "Indiquez la cuisine : c'est son fuseau qui situe ces heures."}
            )

        fuseau = ZoneInfo(restaurant.timezone)
        for champ, brute in locaux.items():
            if brute is None:
                continue
            if champ in valeurs:
                raise serializers.ValidationError(
                    {
                        f"{champ}_local": (
                            f"Choisissez une seule forme : `{champ}` (avec décalage) "
                            f"ou `{champ}_local` (heure de la cuisine)."
                        )
                    }
                )
            try:
                murale = dt.datetime.fromisoformat(str(brute))
            except ValueError as invalide:
                raise serializers.ValidationError(
                    {f"{champ}_local": "Heure illisible — format attendu : 2026-12-25T00:00."}
                ) from invalide
            if murale.tzinfo is not None:
                raise serializers.ValidationError(
                    {
                        f"{champ}_local": (
                            "Cette heure est celle de la cuisine : elle s'envoie sans "
                            "décalage. Utilisez `" + champ + "` pour un instant absolu."
                        )
                    }
                )
            valeurs[champ] = murale.replace(tzinfo=fuseau)

        return valeurs

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Une fermeture a une fin postérieure à son début, et une fin à venir.

        La contrainte `CHECK` tient la première règle en base ; elle sortirait
        en 500. La seconde n'est pas une règle de base : une fermeture déjà
        terminée ne ferme rien, et l'accepter laisserait croire à l'exploitant
        qu'il vient de fermer sa cuisine.
        """
        instance = self.instance if isinstance(self.instance, KitchenClosure) else None
        debut = attrs.get("starts_at", getattr(instance, "starts_at", None))
        fin = attrs.get("ends_at", getattr(instance, "ends_at", None))
        # Les deux paires sont facultatives séparément — il en faut une.
        if debut is None or fin is None:
            raise serializers.ValidationError(
                {
                    "starts_at": (
                        "Indiquez le début et la fin — en heure de la cuisine "
                        "(`starts_at_local`) ou en instant absolu (`starts_at`)."
                    )
                }
            )
        if debut is not None and fin is not None and fin <= debut:
            raise serializers.ValidationError(
                {"ends_at": "La réouverture doit suivre le début de la fermeture."}
            )
        if fin is not None and "ends_at" in attrs and fin <= timezone.now():
            raise serializers.ValidationError(
                {"ends_at": "Cette fermeture est déjà terminée : elle ne fermerait rien."}
            )
        return attrs


class ManagedOpeningHoursSerializer(serializers.ModelSerializer[OpeningHours]):
    """Plage d'ouverture.

    Une plage qui franchit minuit (`22:00 → 02:00`) se saisit telle quelle :
    `closes_at < opens_at` est la représentation, et le service d'ouverture en
    tient compte. Obliger à saisir deux plages sur deux jours serait la source
    d'erreur classique du service de nuit du week-end.
    """

    restaurant = serializers.PrimaryKeyRelatedField[Restaurant](queryset=Restaurant.objects.all())
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = OpeningHours
        fields = ["id", "restaurant", "weekday", "opens_at", "closes_at", "crosses_midnight"]
        read_only_fields = ["id", "crosses_midnight"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Une plage vide est refusée ici plutôt qu'en base.

        La contrainte `CHECK` existe et reste la dernière ligne de défense ;
        elle sortirait en 500. `22:00 → 22:00` est une faute de saisie
        courante, qui mérite un message.
        """
        instance = self.instance
        opens_at = attrs.get("opens_at", getattr(instance, "opens_at", None))
        closes_at = attrs.get("closes_at", getattr(instance, "closes_at", None))

        if opens_at is not None and opens_at == closes_at:
            raise serializers.ValidationError(
                {"closes_at": "Une plage d'ouverture et de fermeture identiques ne couvre rien."}
            )
        return attrs


class AuditEntrySerializer(serializers.ModelSerializer[AuditEntry]):
    """Une entrée du journal des décisions, telle qu'on la relit.

    `actor_name` est lu au moment de la réponse — un compte désactivé garde son
    nom. Un acteur nul (commande de peuplement, `shell`) se rend `null`, et
    l'écran le dit plutôt que d'inventer quelqu'un.
    """

    actor_name = serializers.CharField(source="actor.full_name", read_only=True, default=None)

    class Meta:
        model = AuditEntry
        fields = [
            "id",
            "actor",
            "actor_name",
            "action",
            "target_type",
            "target_id",
            "target_label",
            "before",
            "after",
            "created_at",
        ]
        read_only_fields = fields


class StaffSerializer(serializers.ModelSerializer[User]):
    """Compte du personnel : ce qu'il sait faire et sur quoi.

    `permissions` est rendu à côté des rôles, en lecture seule : c'est leur
    union, et c'est la seule chose que le code consulte réellement. L'écran qui
    coche des rôles peut ainsi montrer immédiatement ce qu'ils accordent, sans
    recomposer côté client une union qui dériverait du jour où un rôle change.

    L'adresse électronique ne se modifie pas après création. Elle est
    l'identifiant de connexion : la changer depuis un écran d'administration
    serait un chemin de reprise de compte — on redirige les courriels de
    réinitialisation vers soi, et le compte suit.
    """

    password = serializers.CharField(
        write_only=True, required=False, min_length=8, trim_whitespace=False
    )
    roles = serializers.PrimaryKeyRelatedField[Role](
        many=True, queryset=Role.objects.all(), required=False
    )
    # Lisible et inscriptible sous le même nom : la lecture passe par
    # l'accesseur inverse de `Restaurant.staff`, l'écriture est reprise à la
    # main dans `create` / `update` — DRF refuse de poser un `.set()` sur une
    # relation qui passe par un modèle intermédiaire, et c'est heureux, puisque
    # le remplacement en bloc effacerait les dates de rattachement.
    restaurants = serializers.SlugRelatedField[Restaurant](
        many=True,
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        required=False,
        help_text="Établissements sur lesquels ce compte travaille.",
    )

    # Périmètres de marché et de ville — le palier entre « le siège » et « un
    # restaurant ». Sans eux, un directeur pays devait être rattaché à chacun
    # de ses établissements un par un, et cessait silencieusement de voir le
    # suivant qu'on ouvrait.
    #
    # Deux listes plates plutôt qu'une liste d'objets `{type, valeur}` : l'écran
    # qui les édite est un sélecteur de pays et un sélecteur de villes, pas un
    # constructeur de couples. Les clés sont celles que le back-office a déjà en
    # main — le code ISO, le slug de ville.
    countries = serializers.SlugRelatedField[Country](
        many=True,
        slug_field="iso_code",
        queryset=Country.objects.all(),
        required=False,
        source="_countries",
        help_text="Marchés dont ce compte est directeur : tous leurs établissements.",
    )
    cities = serializers.SlugRelatedField[City](
        many=True,
        slug_field="slug",
        queryset=City.objects.all(),
        required=False,
        source="_cities",
        help_text="Villes dont ce compte est responsable : tous leurs établissements.",
    )
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "phone",
            "password",
            "is_active",
            "roles",
            "restaurants",
            "countries",
            "cities",
            "permissions",
            "is_superuser",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        # `is_superuser` se lit et ne s'écrit pas ici. C'est le seul compte qui
        # voit l'enseigne entière (`is_unscoped`) : sans lui, l'écran ne peut
        # pas distinguer « rattaché à rien, donc ne voit rien » de « siège » —
        # et affiche l'un pour l'autre. L'accorder depuis un formulaire serait
        # une élévation de privilège que ni `roles.write` ni aucune garde ne
        # couvre : elle reste réservée à `createsuperuser`.
        read_only_fields = [
            "id",
            "permissions",
            "is_superuser",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]

    def get_permissions(self, obj: User) -> list[str]:
        return sorted(obj.permission_codes())

    def to_representation(self, instance: User) -> dict[str, Any]:
        """Complète la lecture des périmètres.

        Les deux champs déclarent une `source` privée parce qu'ils sont
        **écrits** vers une table intermédiaire (`AreaMembership`) que DRF ne
        sait pas remplir seul — exactement la raison qui fait reprendre
        `restaurants` à la main. Il n'existe donc pas d'accesseur à lire côté
        `User`, et la lecture est posée ici.

        Un accesseur inverse sur `User` aurait été plus court, mais `accounts`
        est le socle : lui faire connaître les pays et les villes inverserait le
        graphe de l'ADR-002.
        """
        donnees = super().to_representation(instance)
        perimetres = AreaMembership.objects.filter(user=instance).select_related("country", "city")
        # Le filtre porte sur l'**objet** et non sur `*_id` : les deux disent la
        # même chose à l'exécution — la contrainte `area_membership_exactly_one_target`
        # garantit qu'exactement l'un des deux est renseigné —, mais seul le
        # premier apprend au vérificateur que l'attribut n'est plus nul. Sur
        # `*_id`, il voyait encore `Country | None` et refusait `.iso_code`.
        #
        # Aucune requête supplémentaire : `select_related` a déjà chargé les deux.
        donnees["countries"] = sorted(
            zone.country.iso_code for zone in perimetres if zone.country is not None
        )
        donnees["cities"] = sorted(zone.city.slug for zone in perimetres if zone.city is not None)
        return donnees

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        if self.instance is not None:
            fields["email"].read_only = True
        return fields

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError(
                {"password": "Un compte du personnel se crée avec un mot de passe."}
            )
        if "password" in attrs:
            validate_password(attrs["password"])
        return attrs

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> User:
        roles = validated_data.pop("roles", [])
        restaurants = validated_data.pop("restaurants", [])
        pays = validated_data.pop("_countries", [])
        villes = validated_data.pop("_cities", [])
        password = validated_data.pop("password")

        # `user_type` n'est pas un champ d'entrée : cette ressource crée des
        # comptes du personnel et rien d'autre. L'accepter du corps de la
        # requête permettrait de fabriquer un livreur validé — ou un client —
        # depuis l'écran des rôles.
        member = User.objects.create_user(
            email=validated_data.pop("email"),
            password=password,
            user_type=UserType.STAFF,
            **validated_data,
        )
        member.roles.set(roles)
        _align_memberships(member, restaurants)
        _align_areas(member, pays, villes)
        return member

    @transaction.atomic
    def update(self, instance: User, validated_data: dict[str, Any]) -> User:
        roles = validated_data.pop("roles", None)
        restaurants = validated_data.pop("restaurants", None)
        pays = validated_data.pop("_countries", None)
        villes = validated_data.pop("_cities", None)
        password = validated_data.pop("password", None)

        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        if password:
            instance.set_password(password)
        instance.save()

        if roles is not None:
            instance.roles.set(roles)
        if restaurants is not None:
            _align_memberships(instance, restaurants)
        if pays is not None or villes is not None:
            _align_areas(instance, pays, villes)
        return instance


def _align_memberships(member: User, restaurants: list[Restaurant]) -> None:
    """Aligne les rattachements sur la liste reçue, par différence.

    Et non « tout effacer puis tout recréer » : un rattachement porte sa date
    de création, qui dit depuis quand quelqu'un travaille là. La remise à zéro
    à chaque enregistrement d'un formulaire l'effacerait sans que personne ne
    le remarque.
    """
    voulus = {etablissement.pk for etablissement in restaurants}
    actuels = set(
        StaffMembership.objects.filter(user=member).values_list("restaurant_id", flat=True)
    )

    StaffMembership.objects.filter(user=member, restaurant_id__in=actuels - voulus).delete()
    StaffMembership.objects.bulk_create(
        StaffMembership(user=member, restaurant=etablissement)
        for etablissement in restaurants
        if etablissement.pk not in actuels
    )


def _align_areas(member: User, pays: list[Country] | None, villes: list[City] | None) -> None:
    """Aligne les rattachements de périmètre, par différence comme les autres.

    `None` veut dire « ce champ n'était pas dans la requête » et laisse
    l'existant intact ; une liste vide veut dire « retire-les tous ». Confondre
    les deux ferait perdre son marché à un directeur pays chaque fois qu'on
    corrige son numéro de téléphone depuis un formulaire partiel.

    Les deux axes sont traités séparément parce qu'ils sont indépendants :
    envoyer `cities` sans `countries` ne doit pas effacer les marchés.
    """
    if pays is not None:
        _aligner(member, "country", {objet.pk for objet in pays})
    if villes is not None:
        _aligner(member, "city", {objet.pk for objet in villes})


def _aligner(member: User, champ: str, voulus: set[Any]) -> None:
    """Retire ce qui n'est plus voulu, ajoute ce qui manque.

    Et non « tout effacer puis tout recréer » : un rattachement porte sa date de
    création, qui dit depuis quand quelqu'un couvre ce marché. La remise à zéro
    à chaque enregistrement l'effacerait sans que personne ne le remarque —
    même raison que pour `_align_memberships`.
    """
    existants = AreaMembership.objects.filter(**{"user": member, f"{champ}__isnull": False})
    actuels = set(existants.values_list(f"{champ}_id", flat=True))

    existants.filter(**{f"{champ}_id__in": actuels - voulus}).delete()
    AreaMembership.objects.bulk_create(
        AreaMembership(user=member, **{f"{champ}_id": identifiant})
        for identifiant in voulus - actuels
    )


class ManagedRestaurantZoneSerializer(ManagedDeliveryZoneSerializer):
    """Zone **propre à un établissement** — l'écriture que `geography` ne peut pas porter.

    ## Pourquoi cette classe existe ici et non là-bas

    Rendre `restaurant` inscriptible depuis `ManagedDeliveryZoneSerializer`
    demanderait un jeu de requête sur `Restaurant`, donc un import
    `geography → restaurants`. L'arête inverse existe déjà — `Restaurant.zone` —
    et la refermer ferait un cycle que le test d'architecture refuse. Il l'a
    d'ailleurs refusé : c'est ainsi que ce sérialiseur a trouvé sa place.

    L'héritage va dans le sens autorisé : `restaurants` connaît `geography`,
    reprend son contrat entier — les trois modes de saisie, la validation de
    devise, le calcul du contour — et n'y ajoute que le rattachement. Écrire un
    second sérialiseur complet aurait produit deux validations de barème, qui
    auraient divergé au premier correctif.
    """

    # Redéclaration **volontaire** d'un champ hérité, et changement de nature :
    # la classe mère l'expose en `CharField` lisible seule, parce que `geography`
    # n'a pas le droit d'importer `Restaurant` (le cycle que le test
    # d'architecture refuse). Ici, du côté qui connaît les deux, il devient
    # inscriptible.
    #
    # `type: ignore[assignment]` — le vérificateur signale à juste titre que le
    # type diffère de celui de la base ; c'est précisément l'intention, et elle
    # est la seule façon d'écrire ce rattachement sans inverser le graphe.
    restaurant = serializers.SlugRelatedField[Restaurant](  # type: ignore[assignment]
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        help_text="Établissement auquel cette zone est propre.",
    )

    class Meta(ManagedDeliveryZoneSerializer.Meta):
        read_only_fields = ["id", "overlaps", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Une zone d'établissement se pose dans la ville de cet établissement.

        Sans ce contrôle, on rattacherait une zone de Douala à la cuisine de
        Lomé : elle n'aurait alors aucune chance de couvrir une adresse que
        cette cuisine dessert, et l'erreur ne se verrait qu'à la première
        commande refusée — sans que rien n'en donne la raison.
        """
        attrs = super().validate(attrs)

        etablissement = attrs.get("restaurant") or (
            self.instance.restaurant if self.instance else None
        )
        ville = attrs.get("city") or (self.instance.city if self.instance else None)
        if etablissement is None or ville is None:
            return attrs

        if etablissement.zone.city_id != ville.pk:
            raise serializers.ValidationError(
                {
                    "restaurant": (
                        f"« {etablissement.name} » est à {etablissement.zone.city.name} ; "
                        f"une zone qui lui est propre ne peut pas être posée sur "
                        f"{ville.name}."
                    )
                }
            )
        return attrs
