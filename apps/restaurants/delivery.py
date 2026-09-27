"""Livrabilité d'une adresse — **le référentiel unique des trois applications**.

## La question, et pourquoi elle se pose une seule fois

« Puis-je me faire livrer ici, par qui, à quel prix, en combien de temps ? »

Le client la pose avant d'enregistrer une adresse. Le back-office la pose pour
vérifier une configuration. Dely s'appuie sur la même géographie pour situer une
course. Trois applications, une seule réponse possible — et pourtant, avant ce
module, aucune ne pouvait l'obtenir entière :

* `GET /geography/zones/resolve/` rendait **la zone seule** : ni établissement,
  ni frais, ni distance, ni délai ;
* le devis complet n'existait qu'à l'intérieur d'`OrderService`, donc seulement
  pour quelqu'un ayant déjà un panier ouvert sur un restaurant choisi.

Chaque écran recomposait donc le reste à sa façon. C'est exactement la
duplication qu'il faut éviter : trois implémentations d'une règle de tarification
finissent par donner trois prix, dont deux sont faux.

## Ce que ce module n'est pas

Il ne calcule rien lui-même. La zone vient de `apps.geography.resolution`, les
frais de `apps.geography.services.quote_delivery`, la distance de PostGIS. Il
**assemble**, et c'est sa seule raison d'être : le lieu où l'on peut à la fois
connaître les établissements et la géographie, sans que la géographie ait à
connaître les établissements (ADR-002).

## Pourquoi la route ne vit pas sous `/geography/`

Ce serait le chemin le plus lisible, et il est impossible : la vue devrait
importer `Restaurant`, ce que le graphe interdit à `geography`. La réponse porte
d'ailleurs surtout un établissement — c'est bien une question de restaurants,
posée à travers une position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.geography.models import DeliveryZone
from apps.geography.resolution import covering_zones, resolve_zone
from apps.geography.services import DeliveryQuote, quote_delivery
from apps.restaurants.availability import kitchen_unavailability
from apps.restaurants.models import Restaurant, kitchen_state_prefetches
from common.availability import AddressNotServed, UnavailabilityCode
from common.exceptions import BusinessRuleViolation
from common.money import Money

__all__ = ["DeliveryAvailability", "check_delivery", "overlapping_zones"]

logger = logging.getLogger(__name__)

#: Cuisines examinées au plus pour un point, de la plus proche à la plus loin.
#:
#: Une ville en compte quelques-unes ; la borne ne sert qu'à ce qu'une erreur de
#: saisie — une zone municipale dessinée sur tout un pays — ne fasse pas juger
#: des centaines d'établissements à chaque déplacement du repère sur la carte.
_CANDIDATS_MAX = 20


@dataclass(frozen=True, slots=True)
class DeliveryAvailability:
    """Réponse complète à « puis-je être livré ici ? ».

    `is_available` répond à la question ; `reason` dit **pourquoi** quand la
    réponse est non. Les deux sont nécessaires : un booléen seul oblige chaque
    écran à inventer un message, et il en invente un différent dans chacune des
    trois applications — ce que le refus soit « hors zone », « trop loin »,
    « panier trop léger » ou « pas de cuisine ouverte ».

    `quote` est nul quand aucun sous-total n'a été fourni : on peut vouloir
    savoir si une adresse est desservie **avant** d'avoir un panier, et un
    montant minimum ne veut rien dire face à un panier vide.
    """

    is_available: bool
    restaurant: Restaurant | None
    zone: DeliveryZone | None
    distance_m: float | None
    quote: DeliveryQuote | None
    reason: str | None = None

    #: Le refus métier tel qu'il a été levé, quand il en vient un.
    #:
    #: Conservé **entier** plutôt que réduit à sa phrase : un
    #: `BusinessRuleViolation` porte des données contextuelles que le client
    #: exploite — `min_order_amount` pour dire combien il manque,
    #: `distance_km` et `max_distance_km` pour situer l'adresse. Reconstruire
    #: l'exception à partir du seul message les perdrait, et l'écran ne pourrait
    #: plus dire que « ce n'est pas possible ».
    #:
    #: C'est exactement ce qu'un test a attrapé : `min_order_amount` avait
    #: disparu de la réponse 409 du devis.
    refusal: BusinessRuleViolation | None = None

    #: Le motif **stable** du refus, nul quand la livraison est possible.
    #:
    #: `no_kitchen_available` et `address_not_served` n'appellent pas le même
    #: geste — attendre qu'El Corazón ouvre dans le quartier, ou choisir une
    #: autre adresse —, et `reason` seule obligeait le client à comparer des
    #: phrases. Pour un refus de panier (minimum, devise), c'est le code du
    #: refus lui-même.
    unavailable_code: str | None = None

    @property
    def estimated_minutes(self) -> int | None:
        """Délai annoncé : préparation en cuisine **plus** course.

        Les deux, parce que c'est ce que le client attend réellement. La zone ne
        connaît que le trajet, et l'annoncer seul promettait un repas en
        trente minutes là où la cuisine en demande vingt de plus.
        """
        if self.zone is None:
            return None
        preparation = (
            self.restaurant.default_preparation_minutes if self.restaurant is not None else 0
        )
        return preparation + self.zone.estimated_delivery_minutes


def check_delivery(
    *,
    point: Point,
    restaurant: Restaurant | None = None,
    subtotal: Money | None = None,
) -> DeliveryAvailability:
    """Livrabilité d'un point : établissement, zone, distance, frais, délai.

    Sans `restaurant`, la fonction **choisit** : la plus proche des cuisines
    qui desservent le point **et peuvent prendre une commande maintenant**, à
    défaut la plus proche de celles qui le desservent. C'est ce que demande
    l'application cliente : obliger quelqu'un à désigner une cuisine que la
    géographie détermine est une étape sans décision.

    Avec `restaurant`, elle vérifie *celle-là* — le cas du panier déjà ouvert,
    où changer de cuisine changerait le catalogue et les prix.

    ## Ce que `is_available` dit, et ce qu'il ne dit pas

    **La livrabilité** : une cuisine dessert ce point, dans son rayon, et le
    panier en respecte le barème. Pas l'état de la cuisine : il voyage avec
    elle, `restaurant.can_order_now` et son motif. « On vous livre, mais la
    cuisine ouvre à 11 h » et « on ne vous livre pas » ne sont pas la même
    réponse, et les fondre obligerait l'écran à deviner laquelle il reçoit.

    Les refus sont ordonnés du plus général au plus précis, pour que le message
    rendu soit le plus actionnable : d'abord « personne ne dessert ici »,
    ensuite « hors zone », ensuite « trop loin », enfin « panier trop léger ».
    Inverser cet ordre ferait dire « ajoutez un article » à quelqu'un qui habite
    à trois cents kilomètres.
    """
    if restaurant is None:
        restaurant = _cuisine_pour_le_point(point)
        if restaurant is None:
            return DeliveryAvailability(
                is_available=False,
                restaurant=None,
                zone=None,
                distance_m=None,
                quote=None,
                reason="Aucune cuisine El Corazón ne dessert cette adresse pour le moment.",
                unavailable_code=str(UnavailabilityCode.NO_KITCHEN_AVAILABLE),
            )

    # La ville de la cuisine borne les zones municipales. Sans elle, une cuisine
    # désignée — le panier déjà ouvert — se voyait tarifer par la zone de la
    # ville voisine qui couvre le point, et « desservir » une adresse que le
    # choix automatique (`_desservantes`) lui refuse. Deux réponses pour une
    # seule adresse : la règle doit être la même dans les deux sens.
    zone = resolve_zone(point, restaurant_id=restaurant.pk, city_id=restaurant.zone.city_id)
    if zone is None:
        hors_zone = AddressNotServed("Cette adresse n'est couverte par aucune zone de livraison.")
        return DeliveryAvailability(
            is_available=False,
            restaurant=restaurant,
            zone=None,
            distance_m=None,
            quote=None,
            reason=hors_zone.detail,
            refusal=hors_zone,
            unavailable_code=str(UnavailabilityCode.ADDRESS_NOT_SERVED),
        )

    distance_m = _distance_metres(restaurant, point)

    # Au-delà du rayon maximal, la course est refusée **même si le point est
    # dans le contour** : un contour se dessine large, la distance réellement
    # parcourue est ce qui coûte. Le dire ici évite qu'un écran annonce
    # « desservi » et que la commande échoue trois écrans plus loin.
    if distance_m is not None and Decimal(str(distance_m)) / 1000 > zone.max_distance_km:
        trop_loin = AddressNotServed(
            f"Adresse à {distance_m / 1000:.1f} km, au-delà des "
            f"{zone.max_distance_km} km desservis depuis cette cuisine.",
            distance_km=f"{distance_m / 1000:.2f}",
            max_distance_km=str(zone.max_distance_km),
        )
        return DeliveryAvailability(
            is_available=False,
            restaurant=restaurant,
            zone=zone,
            distance_m=distance_m,
            quote=None,
            reason=trop_loin.detail,
            refusal=trop_loin,
            unavailable_code=str(UnavailabilityCode.ADDRESS_NOT_SERVED),
        )

    quote = None
    if subtotal is not None and distance_m is not None:
        try:
            quote = quote_delivery(zone=zone, distance_m=distance_m, subtotal=subtotal)
        except BusinessRuleViolation as refus:
            # Un panier trop léger ou libellé dans une autre devise ne rend pas
            # l'adresse indesservie : il rend *cette commande* impossible. La
            # nuance décide de ce que l'écran propose — ajouter un article,
            # plutôt que changer d'adresse.
            return DeliveryAvailability(
                is_available=False,
                restaurant=restaurant,
                zone=zone,
                distance_m=distance_m,
                quote=None,
                reason=str(refus),
                refusal=refus,
                unavailable_code=refus.code,
            )

    return DeliveryAvailability(
        is_available=True,
        restaurant=restaurant,
        zone=zone,
        distance_m=distance_m,
        quote=quote,
    )


def overlapping_zones(zone: DeliveryZone) -> list[DeliveryZone]:
    """Zones actives dont le contour recoupe celui-ci — l'avertissement du back-office.

    **Un chevauchement n'est pas une faute.** Une zone « Centre-ville » posée
    dans une zone « Grand Lomé » est la façon normale d'exprimer une exception
    tarifaire, et `resolve_zone` sait laquelle l'emporte. L'écran le signale
    pour que la décision soit consciente, jamais pour empêcher l'écriture.

    La comparaison est faite en base, par `intersects` sur l'index GiST : la
    charger en Python obligerait à sortir des contours de plusieurs kilo-octets
    pour n'en garder qu'un booléen.
    """
    return list(
        DeliveryZone.objects.filter(
            boundary__intersects=zone.boundary,
            is_active=True,
            city__country=zone.city.country_id,
        )
        .exclude(pk=zone.pk)
        .select_related("city")
        .order_by("name")[:20]
    )


def _cuisine_pour_le_point(point: Point) -> Restaurant | None:
    """La cuisine qui livrera ce point — la plus proche **qui peut commander**.

    ## Le défaut que ce choix ferme

    La fonction rendait la plus proche des cuisines desservant le point, sans
    regarder si elle prenait des commandes. Dans une ville à deux cuisines, un
    client plus proche de celle qui est fermée se voyait attribuer celle-là —
    et donc une carte qu'il ne pouvait pas commander —, pendant que la seconde,
    ouverte et desservant la même adresse, restait invisible.

    La plus proche commandable est donc retenue. S'il n'y en a aucune, la plus
    proche tout court : « la cuisine de votre quartier ouvre à 11 h » est une
    réponse, là où « aucune cuisine » serait faux.

    L'état de chaque candidate est lu par le juge de la cuisine — jamais
    recomposé ici (voir `tests/availability/test_juge.py`, qui interdit tout
    second lecteur de `accepts_orders`).
    """
    candidates = list(
        _desservantes(point)
        .select_related("zone__city__country")
        .prefetch_related(*kitchen_state_prefetches())
        .annotate(vers=Distance("location", point))
        .order_by("vers")[:_CANDIDATS_MAX]
    )

    if not candidates:
        logger.info("kitchen.resolution.none", extra={"candidates": 0})
        return None

    moment = timezone.now()
    rejetees: list[dict[str, str]] = []
    for cuisine in candidates:
        verdict = kitchen_unavailability(cuisine, moment)
        if verdict is None:
            logger.info(
                "kitchen.resolution.selected",
                extra={
                    "kitchen": cuisine.slug,
                    "city": cuisine.zone.city.slug,
                    "candidates": [c.slug for c in candidates],
                    "rejected": rejetees,
                },
            )
            return cuisine
        rejetees.append({"kitchen": cuisine.slug, "code": str(verdict.code)})

    plus_proche = candidates[0]
    logger.info(
        "kitchen.resolution.none_orderable",
        extra={
            "kitchen": plus_proche.slug,
            "city": plus_proche.zone.city.slug,
            "candidates": [c.slug for c in candidates],
            "rejected": rejetees,
        },
    )
    return plus_proche


def _desservantes(point: Point) -> QuerySet[Restaurant]:
    """Établissements en service dont une zone couvre ce point.

    La **couverture est vérifiée avant la proximité**, et l'ordre compte : le
    restaurant le plus proche à vol d'oiseau n'est pas nécessairement celui qui
    dessert l'adresse — un fleuve, une limite de zone ou un marché voisin
    peuvent l'en séparer. Trier d'abord par distance puis filtrer donnerait
    « aucun » là où un établissement un peu plus loin dessert parfaitement.
    """
    servants = Restaurant.objects.filter(
        is_active=True,
        zone__is_active=True,
        zone__city__is_active=True,
        zone__city__country__is_active=True,
    )

    # Deux familles de zones couvrent le point, et il faut les deux.
    #
    # **Les municipales désignent une ville, pas un établissement.** Une zone
    # appartient à une ville et décrit *où l'on livre* ; ce sont donc les
    # cuisines de cette ville qui la servent, et non les seules dont c'est aussi
    # la zone de rattachement. Confondre les deux — filtrer sur
    # `Restaurant.zone`, qui dit où l'établissement est **posé** — rendait
    # « personne ne dessert ici » dès que le client habitait une zone autre que
    # celle du restaurant, ce qui est le cas normal d'une ville à plusieurs
    # zones.
    villes = set(
        covering_zones(point).filter(restaurant__isnull=True).values_list("city_id", flat=True)
    )
    proprietaires = set(
        covering_zones(point)
        .filter(restaurant__isnull=False)
        .values_list("restaurant_id", flat=True)
    )

    return servants.filter(Q(zone__city_id__in=villes) | Q(pk__in=proprietaires))


def _distance_metres(restaurant: Restaurant, point: Point) -> float | None:
    """Distance à vol d'oiseau, mesurée par PostGIS sur l'ellipsoïde.

    En mètres et sans projection à choisir : c'est ce que donne le type
    `geography`, et c'est ce qui rend la mesure juste à toute latitude. La
    calculer en Python demanderait de choisir une formule et une sphère, et de
    la maintenir cohérente avec celle que la base utilise déjà pour trier.
    """
    distance = (
        Restaurant.objects.filter(pk=restaurant.pk)
        .annotate(vers=Distance("location", point))
        .values_list("vers", flat=True)
        .first()
    )
    return distance.m if distance is not None else None
