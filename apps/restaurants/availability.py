"""La cuisine peut-elle prendre une commande maintenant ?

C'est le premier des trois niveaux composés par `apps.availability` — voir
`common.availability` pour la forme de la réponse, et pourquoi elle est
unique.

## Le défaut que ce module ferme

Jusqu'ici, la règle n'existait **qu'en affichage** : `RestaurantSerializer`
calculait `can_order_now = is_active and accepts_orders and is_open_at(now)`
pour l'application cliente, et rien d'autre ne la vérifiait. La création de
commande ne regardait que la publication (`is_active`, par le `queryset` du
sérialiseur).

Une commande envoyée directement à l'API — ou par une application restée
ouverte sur un écran de panier pendant la fermeture — était donc acceptée par
une cuisine fermée, ou qui venait de suspendre ses commandes pour un coup de
feu. L'écran disait « Fermé » ; le serveur encaissait.

La règle vit désormais ici, et les deux usages — l'affichage et le refus — la
lisent au même endroit.

## Quatre questions, jamais confondues

Une cuisine de livraison peut être :

* **active** mais fermée — en service, hors de ses horaires ;
* **ouverte** mais ne plus **accepter** de commandes — un coup de feu ;
* ouverte et acceptant, mais **ne pas desservir** le client — une question de
  géographie, que ce module ne peut pas poser (elle dépend de l'adresse) et qui
  vit dans `apps.restaurants.delivery`.

`KitchenState` porte les trois premières séparément, et le verdict en dérive.
Les écrans du personnel ont besoin des drapeaux (« en service, mais fermée »),
le client et la commande du seul verdict.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.db import connection
from django.utils import timezone

from apps.restaurants.models import Restaurant, kitchen_state_prefetches
from apps.restaurants.states import RestaurantStatus
from common.availability import Unavailability, UnavailabilityCode

__all__ = [
    "KitchenState",
    "kitchen_state",
    "kitchen_unavailability",
    "lock_kitchen_for_order",
    "next_opening",
    "reopening_label",
]

_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def reopening_label(reopens_at: dt.datetime, *, now: dt.datetime, timezone_name: str) -> str:
    """« aujourd'hui à 18 h 00 », « demain à 11 h 00 », « lundi à 11 h 00 ».

    Composée **ici**, dans le fuseau du pays de la cuisine, et non sur le
    téléphone : l'appareil d'un client en voyage, ou simplement mal réglé,
    annoncerait une heure qui n'est pas celle de la cuisine. C'est la même
    raison qui fait comparer les horaires dans ce fuseau (`is_open_at`).
    """
    fuseau = ZoneInfo(timezone_name)
    local, aujourdhui = reopens_at.astimezone(fuseau), now.astimezone(fuseau).date()
    heure = f"{local.hour} h {local.minute:02d}"
    ecart = (local.date() - aujourdhui).days
    if ecart <= 0:
        return f"aujourd'hui à {heure}"
    if ecart == 1:
        return f"demain à {heure}"
    if ecart < 7:
        return f"{_JOURS[local.weekday()]} à {heure}"
    return f"le {local:%d/%m} à {heure}"


@dataclass(frozen=True, slots=True)
class KitchenState:
    """L'état d'une cuisine à un instant — les drapeaux, puis le verdict.

    Chaque drapeau répond à **une** question. Les recomposer ailleurs est
    précisément le défaut que ce module ferme : `unavailability` est la seule
    composition autorisée.
    """

    name: str
    #: En service (`status = active`) — la décision de l'exploitation.
    is_published: bool
    #: Retirée du service après y avoir été (`status = inactive`).
    is_suspended: bool
    #: Sa zone, sa ville et son pays sont ouverts.
    is_market_open: bool
    #: Dans une plage d'ouverture, **horaires seulement**.
    is_open: bool
    #: Le drapeau du coup de feu : prend-elle des commandes ?
    is_accepting_orders: bool
    #: Dans une fermeture exceptionnelle datée (`KitchenClosure`).
    is_temporarily_closed: bool = False
    #: Le motif de cette fermeture, montré au client — vide sans fermeture.
    closure_reason: str = ""
    #: Premier instant où elle rouvre, quand elle est fermée (horaires ou
    #: fermeture) et que l'horizon le connaît — voir `next_opening`.
    reopens_at: dt.datetime | None = None
    #: Cette réouverture, dite dans le fuseau du pays — vide sans réouverture.
    reopens_label: str = ""

    @property
    def is_active(self) -> bool:
        """Visible des clients : en service, sur un marché ouvert."""
        return self.is_published and self.is_market_open

    @property
    def unavailability(self) -> Unavailability | None:
        """Ce qui empêche de commander — `None` si rien.

        ## L'ordre des questions

        Du plus structurel au plus conjoncturel : marché, puis service, puis
        horaires, puis prise de commande. Une cuisine fermée **et** en pause se
        dit « fermée » : c'est l'état qui dure, et « réessayez dans quelques
        minutes » serait faux jusqu'à l'ouverture.
        """
        if not self.is_market_open or not (self.is_published or self.is_suspended):
            return Unavailability(
                code=UnavailabilityCode.KITCHEN_UNPUBLISHED,
                message="Cette cuisine n'est pas ouverte au public.",
            )

        if self.is_suspended:
            return Unavailability(
                code=UnavailabilityCode.KITCHEN_SUSPENDED,
                message=(f"La cuisine {self.name} est suspendue et ne livre plus pour le moment."),
            )

        # Une fermeture datée passe avant les horaires : « fermée
        # exceptionnellement, réouverture lundi » dit plus que « fermée », et
        # c'est l'état qui dure.
        if self.is_temporarily_closed:
            motif = f" ({self.closure_reason})" if self.closure_reason else ""
            return Unavailability(
                code=UnavailabilityCode.KITCHEN_TEMPORARILY_CLOSED,
                message=f"La cuisine {self.name} est fermée exceptionnellement{motif}."
                + self._reouverture,
                details=self._details_reouverture,
            )

        if not self.is_open:
            return Unavailability(
                code=UnavailabilityCode.KITCHEN_CLOSED,
                message=f"La cuisine {self.name} est fermée pour le moment." + self._reouverture,
                details=self._details_reouverture,
            )

        if not self.is_accepting_orders:
            return Unavailability(
                code=UnavailabilityCode.KITCHEN_PAUSED,
                message=(
                    f"La cuisine {self.name} ne prend pas de commandes pour le moment. "
                    "Réessayez dans quelques minutes."
                ),
            )

        return None

    @property
    def can_accept_orders(self) -> bool:
        return self.unavailability is None

    @property
    def _reouverture(self) -> str:
        return f" Réouverture {self.reopens_label}." if self.reopens_label else ""

    @property
    def _details_reouverture(self) -> dict[str, str]:
        """La réouverture, lisible par une machine — l'instant en ISO 8601.

        Vide quand elle n'est pas connue : le client ne doit pas avoir à
        distinguer une clé absente d'une date nulle.
        """
        if self.reopens_at is None:
            return {}
        return {"reopens_at": self.reopens_at.isoformat(), "reopens_label": self.reopens_label}


def next_opening(
    restaurant: Restaurant, moment: dt.datetime, *, horizon_days: int = 8
) -> dt.datetime | None:
    """Premier instant, à partir de `moment`, où la cuisine est ouverte.

    « Ouverte » veut dire **dans ses horaires et hors de toute fermeture
    exceptionnelle** — la prise de commande et la publication ne sont pas
    regardées : ce sont des décisions, pas un calendrier.

    Vit ici, dans le juge, et non sur le modèle : l'ouverture ne se lit qu'en
    un lieu (`tests/availability/test_juge.py` interdit tout autre lecteur de
    `is_open_at`), et « quand rouvre-t-elle ? » est la même question posée à
    l'instant suivant.

    ## Pourquoi il suffit d'essayer trois sortes d'instants

    L'ensemble des instants ouverts est une union de plages d'où l'on retire des
    fermetures. Son premier point après `moment` est donc forcément `moment`
    lui-même, **le début d'une plage**, ou **la fin d'une fermeture** : il n'y a
    pas d'autre bord. Les essayer dans l'ordre donne la réponse exacte sans
    découper le temps en minutes.

    Rend `None` au-delà de l'horizon : une cuisine sans aucune plage, ou fermée
    pour trois semaines, n'a pas de « réouverture à 18 h » à annoncer, et
    inventer une date serait pire que se taire.
    """
    fuseau = ZoneInfo(restaurant.timezone)
    limite = moment + dt.timedelta(days=horizon_days)
    fermetures = restaurant.closures_after(moment)
    local = moment.astimezone(fuseau)

    candidats = {moment, *(fermeture.ends_at for fermeture in fermetures)}
    plages = list(restaurant.opening_hours.all())
    for decalage in range(-1, horizon_days + 1):
        jour = local.date() + dt.timedelta(days=decalage)
        for plage in plages:
            if plage.weekday == jour.weekday():
                candidats.add(dt.datetime.combine(jour, plage.opens_at, tzinfo=fuseau))

    for candidat in sorted(c for c in candidats if moment <= c <= limite):
        if not restaurant.is_open_at(candidat):
            continue
        if any(f.starts_at <= candidat < f.ends_at for f in fermetures):
            continue
        return candidat
    return None


def kitchen_state(restaurant: Restaurant, at: dt.datetime | None = None) -> KitchenState:
    """Relève l'état de la cuisine, sans rien composer.

    ## Une cuisine sans aucune plage d'ouverture est fermée

    Et non ouverte en permanence. C'est ce que `Restaurant.is_open_at` répond
    déjà, ce que l'application affiche déjà, et ce que la mise en service exige
    déjà (`configuration_gaps` refuse d'ouvrir sans horaires). Lire l'absence
    d'horaires comme « toujours ouvert » aurait donné au serveur une règle
    contraire à celle de l'écran — le défaut même que ce module corrige.
    """
    moment = at if at is not None else timezone.now()
    zone = restaurant.zone
    fermeture = restaurant.closure_at(moment)
    ouverte = restaurant.is_open_at(moment)

    # La réouverture ne se cherche que pour une cuisine fermée : ouverte, elle
    # n'a rien à annoncer, et la chercher coûterait pour rien sur chaque ligne
    # d'une liste.
    reouverture = None
    if fermeture is not None or not ouverte:
        reouverture = next_opening(restaurant, moment)

    return KitchenState(
        name=restaurant.name,
        # `is_active` est la projection de `status == active` (`Restaurant.save`),
        # lue telle quelle : la même colonne que filtrent l'annuaire et le panier.
        is_published=restaurant.is_active,
        is_suspended=restaurant.status == RestaurantStatus.INACTIVE,
        # La même cascade que la liste publique (`RestaurantViewSet.get_queryset`) :
        # un établissement que la liste masque parce que sa zone, sa ville ou son
        # pays est fermé ne peut pas être déclaré commandable ici.
        is_market_open=zone.is_active and zone.city.is_active and zone.city.country.is_active,
        is_open=ouverte,
        is_accepting_orders=restaurant.accepts_orders,
        is_temporarily_closed=fermeture is not None,
        closure_reason=fermeture.reason if fermeture is not None else "",
        reopens_at=reouverture,
        reopens_label=(
            reopening_label(reouverture, now=moment, timezone_name=restaurant.timezone)
            if reouverture is not None
            else ""
        ),
    )


def kitchen_unavailability(
    restaurant: Restaurant, at: dt.datetime | None = None
) -> Unavailability | None:
    """Ce qui empêche la cuisine de prendre une commande — `None` si rien.

    Conséquence à vérifier **avant déploiement** : un établissement passé en
    service par la reprise de données de `0005_restaurant_lifecycle`, qui n'a
    pas contrôlé les horaires, cesserait de prendre des commandes s'il n'en a
    pas.
    """
    return kitchen_state(restaurant, at).unavailability


def lock_kitchen_for_order(restaurant: Restaurant) -> Restaurant:
    """Relit la cuisine **sous verrou partagé**, pour la juger au moment d'écrire.

    ## Le défaut que ce verrou ferme

    La commande jugeait l'instance qu'on lui passait. Cette instance est lue au
    début de la requête — et, pour un panier collaboratif, bien plus tôt. Entre
    cette lecture et l'écriture de la commande, la cuisine peut fermer ou se
    mettre en pause : le client a ouvert la carte cuisine ouverte, et le coup de
    feu est déclaré pendant qu'il paie. Juger l'instance périmée laissait passer
    la commande.

    ## Pourquoi `FOR SHARE`, et pas `FOR UPDATE`

    `FOR SHARE` bloque l'écriture concurrente de la ligne — la mise en pause, la
    suspension — jusqu'à la fin de la transaction de commande, **sans bloquer
    les autres commandes**, qui prennent le même verrou partagé. Deux issues
    seulement, toutes deux justes :

    * la pause est validée avant : la relecture la voit, la commande est refusée ;
    * la commande tient le verrou : la pause attend quelques millisecondes, et
      la commande a légitimement été prise avant elle.

    `FOR UPDATE` donnerait la même garantie en faisant passer les commandes
    d'une même cuisine **une par une** — un goulot au coup de feu, c'est-à-dire
    au moment exact où la cuisine en reçoit le plus.

    Les horaires vivent dans une autre table et ne sont pas verrouillés : leur
    modification est rare, et c'est l'instant de la relecture qui fait foi.

    Doit être appelée dans une transaction — le verrou n'existe pas en dehors.
    """
    # L'ORM ne sait exprimer que `FOR UPDATE`. Nom de table issu du modèle,
    # jamais d'une entrée : aucune injection possible ; la clé passe en paramètre.
    table = connection.ops.quote_name(Restaurant._meta.db_table)
    requete = f"SELECT 1 FROM {table} WHERE id = %s FOR SHARE"  # noqa: S608
    with connection.cursor() as curseur:
        curseur.execute(requete, [restaurant.pk])

    return (
        Restaurant.objects.select_related("zone__city__country")
        .prefetch_related(*kitchen_state_prefetches())
        .get(pk=restaurant.pk)
    )
