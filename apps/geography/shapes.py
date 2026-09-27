"""Du disque au contour — la conversion qui rend le mode « cercle » possible.

## Pourquoi un cercle devient un polygone

PostGIS n'a pas de type « disque ». Un rayon autour d'un point se stocke donc
soit comme deux colonnes qu'il faut interpréter à chaque requête, soit comme le
polygone qui l'approche. Le second gagne pour une raison décisive : **toutes les
requêtes existantes continuent de marcher**. `boundary__covers`, l'index GiST, le
tri par surface, la résolution de zone — rien n'a à connaître le mode de saisie.

Le centre et le rayon sont conservés **en plus** (`DeliveryZone.center`,
`radius_meters`) parce qu'ils portent l'intention. Le contour dit où l'on livre ;
eux disent ce que l'administrateur a voulu, et c'est ce qu'il faut lui
réafficher pour qu'il puisse changer 5 km en 7.

## Pourquoi 64 côtés

Un polygone régulier inscrit dans un cercle est toujours **plus petit** que lui :
il manque, entre deux sommets, une zone que le cercle couvrait. Avec 64 côtés,
cet écart vaut 0,12 % du rayon — 6 mètres sur 5 km, soit moins que l'imprécision
d'un relevé GPS de téléphone. Avec 16, il vaudrait 2 % : 100 mètres, assez pour
qu'une adresse en bordure soit refusée alors que l'écran la montrait dedans.

Au-delà de 64, le gain devient invisible et le contour pèse pour rien — il
voyage dans chaque réponse du back-office et s'indexe à chaque écriture.

## Pourquoi la conversion est géodésique

Un degré de longitude ne vaut pas un degré de latitude, et leur rapport dépend de
la latitude où l'on se trouve. Tracer un cercle en ajoutant `rayon / 111_320` aux
deux coordonnées — l'approximation courante — produit un disque à l'équateur et
une ellipse de plus en plus aplatie en s'en éloignant. À Lomé (6°) l'erreur est
de 0,5 %, à Paris (49°) de 35 % : la même saisie donnerait deux zones très
différentes selon le marché, ce qui est exactement ce qu'un produit multi-pays ne
peut pas se permettre.
"""

from __future__ import annotations

import math

from django.contrib.gis.geos import LinearRing, MultiPolygon, Point, Polygon

__all__ = [
    "CIRCLE_SEGMENTS",
    "MAX_POLYGON_VERTICES",
    "MAX_ZONE_AREA_KM2",
    "check_boundary",
    "circle_to_boundary",
    "polygon_to_boundary",
]

#: Nombre de côtés du polygone qui approche un disque. Voir l'en-tête du module.
CIRCLE_SEGMENTS = 64

#: Rayon terrestre moyen, en mètres (sphère de référence de l'IUGG).
#:
#: La sphère suffit ici : l'écart avec l'ellipsoïde WGS84 est de l'ordre de
#: 0,3 %, très en deçà de la précision d'un contour de livraison tracé à la main.
#: Les distances qui *facturent*, elles, sont mesurées par PostGIS sur
#: l'ellipsoïde — ce module ne sert qu'à dessiner.
EARTH_RADIUS_M = 6_371_008.8

#: Sommets au-delà desquels un contour tracé est refusé.
#:
#: Un quartier se dessine en quelques dizaines de points ; cinq cents laissent de
#: la marge à un contour minutieux, et arrêtent un collage accidentel de trace
#: GPS qui ferait de chaque test d'appartenance une requête lourde.
MAX_POLYGON_VERTICES = 500

#: Surface au-delà de laquelle une zone de livraison n'en est plus une.
#:
#: Cinq mille kilomètres carrés dépassent le Grand Abidjan tout entier. Au-delà,
#: c'est une erreur de saisie — un sommet posé dans le mauvais pays, un rayon
#: tapé en mètres au lieu de kilomètres — que rien d'autre n'attraperait : la
#: zone serait valide, et desservirait la moitié d'un pays.
MAX_ZONE_AREA_KM2 = 5_000

#: Projection de surface égale (cylindrique de Lambert, mondiale) : une aire en
#: mètres carrés juste à toutes les latitudes, ce que ne donne pas Mercator.
_EQUAL_AREA_SRID = 6933


def check_boundary(boundary: MultiPolygon) -> None:
    """Refuse un contour que PostGIS jugerait faux, ou démesuré.

    Deux fautes que la construction laisse passer :

    * **un contour qui se croise** — quatre sommets saisis dans le mauvais
      ordre dessinent un « nœud papillon ». GEOS le construit sans broncher ;
      c'est au premier test d'appartenance qu'il rendrait des réponses
      incohérentes ;
    * **une surface démesurée** — voir [MAX_ZONE_AREA_KM2].

    Lève `ValueError` avec la phrase à montrer ; l'appelant choisit le champ.
    """
    if not boundary.valid:
        raise ValueError(
            "Le contour se croise lui-même : reprenez l'ordre des sommets pour qu'il "
            "fasse le tour de la zone sans se recouper."
        )
    surface_km2 = boundary.transform(_EQUAL_AREA_SRID, clone=True).area / 1_000_000
    if surface_km2 > MAX_ZONE_AREA_KM2:
        raise ValueError(
            f"La zone couvre {surface_km2:,.0f} km², au-delà des "
            f"{MAX_ZONE_AREA_KM2:,} km² admis : vérifiez les sommets ou le rayon."
        )


def circle_to_boundary(
    center: Point, radius_meters: float, *, segments: int = CIRCLE_SEGMENTS
) -> MultiPolygon:
    """Contour d'un disque géodésique, en `MultiPolygon` prêt pour PostGIS.

    Le calcul projette `segments` points à distance constante du centre, dans
    toutes les directions, par la formule de destination sur une sphère. Chaque
    sommet est donc **réellement** à `radius_meters` du centre, quelle que soit
    la latitude.

    Un `MultiPolygon` d'un seul anneau, et non un `Polygon` : c'est le type de
    la colonne, choisi parce qu'une zone réelle est fréquemment discontinue.
    Rendre un `Polygon` ferait échouer l'affectation, et l'envelopper au point
    d'appel disperserait la même ligne partout.
    """
    if radius_meters <= 0:
        raise ValueError("Le rayon d'une zone circulaire doit être strictement positif.")
    if segments < 8:
        raise ValueError("Un cercle approché par moins de huit côtés n'est plus un cercle.")

    lat = math.radians(center.y)
    lon = math.radians(center.x)
    angulaire = radius_meters / EARTH_RADIUS_M

    sommets: list[tuple[float, float]] = []
    for index in range(segments):
        cap = 2 * math.pi * index / segments

        lat_point = math.asin(
            math.sin(lat) * math.cos(angulaire)
            + math.cos(lat) * math.sin(angulaire) * math.cos(cap)
        )
        lon_point = lon + math.atan2(
            math.sin(cap) * math.sin(angulaire) * math.cos(lat),
            math.cos(angulaire) - math.sin(lat) * math.sin(lat_point),
        )

        # Ramené dans [-180, 180] : un contour qui franchit l'antiméridien
        # sortirait sinon des bornes admises et serait rejeté à l'écriture.
        degres_lon = (math.degrees(lon_point) + 540) % 360 - 180
        sommets.append((degres_lon, math.degrees(lat_point)))

    # Un anneau se ferme sur son premier point : sans cette répétition, GEOS
    # refuse la géométrie.
    sommets.append(sommets[0])

    return MultiPolygon(Polygon(LinearRing(sommets, srid=4326), srid=4326), srid=4326)


def polygon_to_boundary(coordinates: list[list[float]]) -> MultiPolygon:
    """Contour tracé à la main, depuis la liste de sommets qu'envoie la carte.

    Attend `[[lon, lat], …]` — l'ordre GeoJSON, celui que produisent les outils
    de cartographie. Il est l'inverse de l'ordre parlé (« latitude, longitude »),
    et c'est le piège classique : un contour saisi à l'envers place une zone de
    Lomé au large de la Somalie, ce que rien ne signale puisque la géométrie
    reste valide.

    L'anneau est fermé ici quand l'appelant ne l'a pas fait : une carte rend
    généralement les sommets sans répéter le premier, et exiger cette répétition
    de chaque client ferait échouer la saisie sur un détail de format.
    """
    if len(coordinates) < 3:
        raise ValueError("Un contour demande au moins trois sommets.")
    if len(coordinates) > MAX_POLYGON_VERTICES:
        raise ValueError(
            f"Un contour compte au plus {MAX_POLYGON_VERTICES} sommets ({len(coordinates)} reçus)."
        )

    sommets = [(float(point[0]), float(point[1])) for point in coordinates]
    for longitude, latitude in sommets:
        # Hors du globe, c'est presque toujours une inversion latitude/longitude
        # que la géométrie accepterait sans rien dire.
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError(
                f"Le sommet [{longitude}, {latitude}] est hors du globe : les sommets "
                "s'écrivent [longitude, latitude]."
            )
    if sommets[0] != sommets[-1]:
        sommets.append(sommets[0])

    if len(sommets) < 4:
        raise ValueError("Un contour demande au moins trois sommets distincts.")

    contour = MultiPolygon(Polygon(LinearRing(sommets, srid=4326), srid=4326), srid=4326)
    check_boundary(contour)
    return contour
