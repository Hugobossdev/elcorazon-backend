"""Résolution de zone — **la règle unique du produit**, et le seul endroit où elle vit.

## Le défaut que ce module ferme

La question « quelle zone s'applique à ce point ? » était résolue à **deux
endroits, par deux règles différentes** :

* `ZoneResolutionView` — le devis affiché au client avant qu'il commande —
  retenait la zone de **plus petite surface** ;
* `OrderService._quote_for` — le montant réellement facturé — retenait la zone
  de plus petit `max_distance_km`.

Les deux coïncident sur un réseau à une zone par ville, ce qui est l'état
actuel, et divergent dès qu'une zone « Centre-ville » est posée à l'intérieur
d'une zone « Grand Lomé » : l'écran annonce un tarif, la commande en applique un
autre. Personne ne le verrait avant la première réclamation, et le journal ne
dirait rien — les deux réponses sont individuellement cohérentes.

Il n'y avait donc pas à choisir arbitrairement entre les deux implémentations :
il fallait établir la règle métier une fois, ici, et la faire appeler par les
deux appelants.

## La règle : la plus spécifique gagne

Une zone incluse dans une autre est une **exception tarifaire** : on la dessine
précisément parce que son barème diffère de celui qui l'entoure. La plus petite
surface est donc la bonne réponse, et `max_distance_km` n'en était qu'un
approximant — deux zones peuvent partager un rayon maximal et couvrir des
surfaces sans rapport.

`priority` la précède, pour les cas que la géométrie ne tranche pas : deux zones
de surface voisine dont l'une doit l'emporter par décision commerciale. Elle est
explicite et rare ; la surface reste le départage ordinaire.

## Pourquoi ce module ne connaît pas les établissements

`geography` est près de la racine du graphe et ne dépend que d'`accounts`
(ADR-002) ; `restaurants` dépend de lui. Importer `Restaurant` ici créerait un
cycle que le test d'architecture refuse — à raison : la géographie doit pouvoir
répondre « quelle zone couvre ce point » sans savoir qu'il existe des cuisines.

Le rattachement d'un établissement voyage donc en **identifiant**, jamais en
objet. La question complète — « qui me livre, à quel prix, à quelle distance » —
se pose un étage plus haut, dans `apps.restaurants.delivery`.
"""

from __future__ import annotations

import uuid

from django.contrib.gis.db.models.functions import Area
from django.contrib.gis.geos import Point
from django.db.models import BooleanField, Case, Q, QuerySet, Value, When

from apps.geography.models import DeliveryZone

__all__ = ["covering_zones", "resolve_zone"]


def covering_zones(point: Point) -> QuerySet[DeliveryZone]:
    """Zones actives dont le contour couvre ce point, marché ouvert compris.

    La cascade sur la ville et le pays n'est pas décorative : fermer un marché
    doit retirer ses zones du calcul, sans quoi une adresse resterait
    « livrable » dans un pays où l'enseigne n'opère plus.

    Exposée parce que l'avertissement de chevauchement du back-office en a
    besoin : il montre **toutes** les zones qui se recouvrent, là où
    `resolve_zone` n'en retient qu'une.
    """
    return DeliveryZone.objects.filter(
        boundary__covers=point,
        is_active=True,
        city__is_active=True,
        city__country__is_active=True,
    ).select_related("city__country")


def resolve_zone(
    point: Point,
    *,
    restaurant_id: uuid.UUID | None = None,
    city_id: uuid.UUID | None = None,
) -> DeliveryZone | None:
    """La zone qui s'applique à ce point — l'unique règle du produit.

    L'ordre de départage, du plus fort au plus faible :

    1. les zones **de l'établissement concerné** passent avant les zones
       municipales, quand un établissement est désigné ;
    2. `priority` décroissante — la décision commerciale explicite ;
    3. surface croissante — la plus spécifique l'emporte.

    Sans `restaurant_id`, seules les zones municipales concourent : une zone
    propre à une cuisine ne doit pas tarifer une question posée sans elle.

    `city_id` restreint les zones **municipales** à cette ville. C'est la ville
    de la cuisine qu'on interroge : une zone municipale décrit où livrent les
    cuisines *de sa ville*, et celle de la ville voisine ne tarife pas une
    course partie d'ici — voir `apps.restaurants.delivery.check_delivery`.

    Rend `None` quand aucune zone ne couvre le point. **Ce n'est pas une
    erreur** : « je viens d'emménager hors zone » est une réponse légitime à une
    question légitime, et la traiter en exception obligerait chaque appelant à
    la ranger dans sa branche d'échec.
    """
    zones = covering_zones(point)

    municipales = Q(restaurant__isnull=True)
    if city_id is not None:
        municipales &= Q(city_id=city_id)

    if restaurant_id is None:
        zones = zones.filter(municipales)
    else:
        # Les zones d'un *autre* établissement sont écartées : elles ne
        # concernent pas cette course, et les laisser concourir ferait payer au
        # client le barème d'une cuisine où il ne commande pas.
        zones = zones.filter(municipales | Q(restaurant_id=restaurant_id))

    return (
        zones.annotate(surface=Area("boundary"))
        .annotate(
            # « Rattachée à un établissement » exprimé en booléen plutôt qu'en
            # tri sur la clé : `ORDER BY restaurant_id DESC` ferait dépendre le
            # résultat de la valeur des UUID, ce qui marcherait presque toujours
            # — la pire des propriétés pour une règle de facturation.
            propre=Case(
                When(restaurant__isnull=False, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        .order_by("-propre", "-priority", "surface")
        .first()
    )
