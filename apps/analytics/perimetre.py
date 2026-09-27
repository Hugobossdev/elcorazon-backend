"""Périmètre d'un rapport — le filtre pays / ville / établissement.

## Ce que ce module corrige

Les rapports agrégeaient **toute l'enseigne**, sans exception. Un gérant
rattaché au seul établissement de Lomé, muni de `analytics.read`, lisait le
chiffre d'affaires d'Abidjan, ses articles les plus vendus et la rémunération
de ses livreurs. Le cloisonnement de l'ADR-005 s'appliquait partout — commandes,
catalogue, personnel — sauf ici, à l'endroit précis où la donnée est agrégée et
donc la plus parlante.

Le défaut ne se voyait pas : un rapport rendait des chiffres justes, simplement
pas les siens. Rien dans la réponse ne disait sur quoi elle portait.

## Deux filtres qui n'ont pas le même statut

* **Le périmètre du compte** est une contrainte. Il n'est pas négociable et ne
  s'annonce pas dans la requête ; il vient de `staff_restaurant_ids`, le même
  point de passage que les autres écrans, si bien qu'un rattachement ajouté
  demain élargira les rapports sans qu'on y revienne.
* **Le filtre demandé** (`?country=`, `?city=`, `?restaurant=`) est un
  affinage. Il restreint, jamais il n'élargit : les deux se composent par
  **intersection**, et un compte cloisonné qui demande le pays entier obtient
  ce qu'il a le droit de voir de ce pays, pas le pays.

C'est cette composition qui permet de rendre le filtre au client sans le
transformer en levier : `?restaurant=el-corazon-abidjan` demandé par le gérant
de Lomé rend un périmètre vide, pas les chiffres d'Abidjan.

## Pourquoi un ensemble d'identifiants, et non un `Q`

Les six rapports partent de tables différentes — commandes, lignes de commande,
courses, articles — et le chemin vers l'établissement diffère à chaque fois
(`restaurant_id`, `order__restaurant_id`, `menu_item__restaurant_id`). Un
`Q` tout fait obligerait chaque appelant à connaître le sien ; un ensemble
d'identifiants se compose avec n'importe quel chemin par un simple `__in`.

L'ensemble est résolu en une requête, avant les agrégations : les rapports
n'ont alors plus de jointure à faire vers la géographie.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from rest_framework import serializers

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from apps.restaurants.scoping import staff_restaurant_ids
from common.permissions import is_unscoped

__all__ = [
    "Perimetre",
    "PerimetreQuerySerializer",
    "aujourd_hui_chez",
    "fenetre_metier",
    "resolve_perimetre",
]


def fenetre_metier(
    *, start: dt.date, end: dt.date, timezone_name: str
) -> tuple[dt.datetime, dt.datetime]:
    """Les deux instants qui bornent des journées d'exploitation.

    `start` et `end` sont des dates **murales**, celles du calendrier de la
    cuisine : « le 25 » veut dire le 25 chez elle. Cette fonction les rend en
    instants, de minuit local inclus à minuit local exclu le lendemain de
    `end`.

    ## Pourquoi pas `__date__range`

    C'est ce qu'employaient les six rapports, et `__date` extrait la date dans
    le fuseau actif du serveur — UTC, figé. La borne tombait donc à minuit UTC,
    et une cuisine de Douala voyait sa première heure d'activité comptée la
    veille. La comparaison sur des instants, elle, ne dépend d'aucun réglage
    global : la conversion est faite ici, une fois, avec le fuseau du
    périmètre.

    La borne haute est **exclusive** (`__lt`), et non « le dernier instant du
    jour » : un `__lte` sur 23:59:59 laisse filer la dernière seconde, et sur
    23:59:59.999999 dépend de la précision de la colonne.
    """
    zone = ZoneInfo(timezone_name)
    debut = dt.datetime.combine(start, dt.time.min, tzinfo=zone)
    fin = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return debut, fin


def aujourd_hui_chez(timezone_name: str) -> dt.date:
    """La date du jour **là où l'activité a lieu**.

    C'est la valeur par défaut des fenêtres de rapport. Elle était calculée par
    le poste du back-office (`DateTime.now()` côté Flutter) : un siège qui
    consulte à minuit et demi voyait le lendemain, et demandait donc les
    chiffres d'une journée qui n'avait pas commencé chez la cuisine.
    """
    return dt.datetime.now(ZoneInfo(timezone_name)).date()


class PerimetreQuerySerializer(serializers.Serializer[Any]):
    """Les trois clés de lecture du réseau, toutes facultatives.

    Des **slugs** et un code ISO, pas des identifiants techniques : ce sont les
    valeurs que le back-office a déjà en main quand il affiche un filtre, et
    elles restent lisibles dans un journal ou une URL partagée.

    Aucune validation d'existence ici. Un pays inconnu doit rendre un rapport
    vide, pas une erreur : le filtre est un affinage, et une ville fermée hier
    est une réponse vide parfaitement correcte — la traiter en 400 ferait
    échouer un tableau de bord dont l'utilisateur n'a rien fait de mal.
    """

    country = serializers.CharField(required=False, help_text="Code ISO 3166-1 alpha-2, ex. TG.")
    city = serializers.CharField(required=False, help_text="Slug de la ville, ex. lome.")
    restaurant = serializers.CharField(
        required=False, help_text="Slug de l'établissement, ex. el-corazon-lome."
    )


@dataclass(frozen=True, slots=True)
class Perimetre:
    """Les établissements sur lesquels porte un rapport.

    `restaurant_ids` à `None` signifie « toute l'enseigne » et n'arrive que pour
    un compte non cloisonné qui n'a demandé aucun filtre. C'est délibérément
    distinct de l'ensemble vide, qui veut dire « rien à montrer » : confondre
    les deux ferait rendre les chiffres de l'enseigne à un compte dont le
    périmètre est vide — exactement l'élargissement silencieux que l'ADR-005
    cherche à empêcher.
    """

    restaurant_ids: frozenset[uuid.UUID] | None

    #: Le fuseau dans lequel se découpent les journées de ce rapport.
    #:
    #: ## Pourquoi il appartient au périmètre
    #:
    #: Les rapports bornaient leurs fenêtres par `delivered_at__date__range`, et
    #: `__date` extrait la date dans le fuseau **actif du serveur** — `UTC`,
    #: figé (`settings.TIME_ZONE`). Une journée de rapport commençait donc à
    #: minuit UTC, quand la journée d'exploitation d'une cuisine de Douala
    #: commence à 23 h UTC la veille : tout ce qui se livrait entre minuit et
    #: une heure du matin sur place était compté la veille.
    #:
    #: Personne ne l'avait vu parce que l'établissement d'origine est à Lomé,
    #: où UTC+0 fait coïncider les deux — le défaut naît avec le deuxième pays.
    #:
    #: Le fuseau est celui du périmètre quand il n'y en a qu'un. Pour un
    #: périmètre qui en traverse plusieurs, « la journée » n'a pas de sens
    #: unique : on retient UTC, et [timezone_est_certain] le dit à la réponse
    #: plutôt que de laisser croire à une précision qu'on n'a pas.
    timezone_name: str

    #: Vrai quand le périmètre tient dans un seul fuseau.
    timezone_est_certain: bool

    @property
    def is_global(self) -> bool:
        return self.restaurant_ids is None

    @property
    def is_empty(self) -> bool:
        """Rien à montrer — un filtre hors périmètre, ou un compte non rattaché."""
        return self.restaurant_ids is not None and not self.restaurant_ids

    def filtre(self, chemin: str) -> dict[str, Any]:
        """Clause `filter()` pour un chemin donné vers l'établissement.

        Rend un dictionnaire vide quand le périmètre est global : le rapport
        n'ajoute alors aucune condition, et ne paie pas la clause `IN` inutile
        qu'un `__in` sur tous les établissements coûterait.

        Exemple : `Order.objects.filter(**perimetre.filtre("restaurant_id"))`.
        """
        if self.restaurant_ids is None:
            return {}
        return {f"{chemin}__in": self.restaurant_ids}


def resolve_perimetre(*, user: User, params: dict[str, Any]) -> Perimetre:
    """Compose le périmètre du compte et le filtre demandé.

    L'ordre est ce qui compte : on part de ce que le compte a le droit de voir,
    **puis** on restreint. L'inverse — partir du filtre et vérifier ensuite —
    laisserait la fenêtre entre les deux, et c'est dans cette fenêtre que les
    fuites s'écrivent.
    """
    demande = _restaurants_demandes(params)
    autorise = None if is_unscoped(user) else frozenset(staff_restaurant_ids(user))

    if demande is None:
        retenus = autorise
    elif autorise is None:
        retenus = demande
    else:
        retenus = demande & autorise

    fuseau, certain = _fuseau_du_perimetre(retenus)
    return Perimetre(restaurant_ids=retenus, timezone_name=fuseau, timezone_est_certain=certain)


def _fuseau_du_perimetre(restaurant_ids: frozenset[uuid.UUID] | None) -> tuple[str, bool]:
    """Le fuseau commun aux établissements retenus, s'il y en a un.

    Une requête, sur des identifiants déjà résolus — la même que celle qui
    servait à les trouver, prolongée d'une colonne. Un périmètre global ou
    à cheval sur plusieurs pays n'a pas de fuseau propre : on rend UTC en le
    disant, ce que la réponse republie.

    Un périmètre **vide** rend UTC lui aussi, et c'est sans conséquence : il
    n'y a rien à agréger.
    """
    if restaurant_ids is None:
        return "UTC", False

    # Un fuseau vide ou absent est écarté plutôt que retenu comme une valeur :
    # `ZoneInfo("")` lèverait, et une cuisine mal configurée ne doit pas rendre
    # tout le rapport indisponible — elle rend seulement le fuseau incertain.
    fuseaux = {
        nom
        for nom in Restaurant.objects.filter(pk__in=restaurant_ids)
        .values_list("zone__city__country__timezone", flat=True)
        .distinct()
        if nom
    }

    if len(fuseaux) == 1:
        return next(iter(fuseaux)), True
    return "UTC", False


def _restaurants_demandes(params: dict[str, Any]) -> frozenset[uuid.UUID] | None:
    """Établissements désignés par les filtres, ou `None` si aucun n'est posé.

    Les trois filtres se cumulent au lieu de se remplacer : `?country=TG&
    city=lome` est « Lomé, si Lomé est au Togo », et non « Lomé ». Un filtre qui
    en écraserait un autre rendrait des chiffres justes pour une question que
    personne n'a posée.
    """
    conditions: dict[str, Any] = {}
    if pays := params.get("country"):
        # Les codes ISO sont stockés en majuscules ; un filtre saisi en
        # minuscules dans une URL ne doit pas rendre un rapport vide.
        conditions["zone__city__country__iso_code__iexact"] = pays
    if ville := params.get("city"):
        conditions["zone__city__slug"] = ville
    if etablissement := params.get("restaurant"):
        conditions["slug"] = etablissement

    if not conditions:
        return None
    return frozenset(Restaurant.objects.filter(**conditions).values_list("pk", flat=True))
