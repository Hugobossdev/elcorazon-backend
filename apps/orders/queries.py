"""Fragments de requête partagés par les deux publics des commandes.

`views.py` sert le client et le livreur, `backoffice.py` le personnel. Les deux
rendent `OrderSerializer`, donc les deux doivent poser les mêmes annotations —
et aucun des deux n'a de raison d'importer l'autre pour cela.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Max,
    Min,
    Q,
    QuerySet,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.orders.models import Order
from apps.orders.states import OrderStatus

__all__ = ["avec_compteurs", "statistiques_de_commandes"]

#: Profondeur de la série quotidienne quand la sélection n'a pas de début :
#: « toutes les commandes » ne doit pas rendre une ligne par jour depuis
#: l'ouverture de la plateforme.
PROFONDEUR_PAR_JOUR = dt.timedelta(days=30)


def avec_compteurs(queryset: QuerySet[Order]) -> QuerySet[Order]:
    """Annote le nombre de lignes et le nombre d'articles d'une commande.

    **En base et en une seule requête**, pour toute la page.

    C'est ce qui permet à la forme de liste d'annoncer « 6 articles » sans
    porter les lignes. Sans ces compteurs, le back-office affichait « 0 article »
    sur *toutes* les commandes, suivi d'un bandeau « aucun article trouvé dans
    cette commande » — sur des commandes qui en contenaient. La carte lisait
    `lines`, que `OrderSerializer` ne rend pas et ne doit pas rendre : renvoyer
    les lignes de vingt commandes pour n'en afficher que le nombre multiplierait
    par dix le poids de chaque page.

    Deux détails qui comptent :

    * `distinct=True` sur `Count` — sans lui, la jointure des lignes se
      multiplie par toute autre jointure présente dans la requête, et un simple
      `select_related` suffit à fausser le compte ;
    * `Coalesce` — une commande sans ligne sortirait `null` plutôt que zéro, et
      le client aurait à distinguer les deux pour rien.

    `Sum` n'a pas besoin de `distinct` : il porte sur `lines__quantity`, dont
    chaque ligne est déjà unique par construction.
    """
    return queryset.annotate(
        lines_count=Coalesce(Count("lines", distinct=True), Value(0)),
        items_count=Coalesce(Sum("lines__quantity"), Value(0)),
    )


def _minutes(duree: dt.timedelta | None) -> float | None:
    return None if duree is None else round(duree.total_seconds() / 60, 1)


def statistiques_de_commandes(
    commandes: QuerySet[Order], *, depuis: dt.datetime | None = None
) -> dict[str, Any]:
    """Ce que l'onglet « Statistiques » et la vue d'ensemble affichent — agrégé en SQL.

    Le back-office téléchargeait **un an** de commandes, page de vingt par page
    de vingt, pour calculer ces chiffres dans le navigateur ; la carte temps
    réel relançait ce téléchargement toutes les dix secondes. Chaque chiffre
    est ici une agrégation sur la sélection que la liste affiche — mêmes
    filtres, même cloisonnement — et reprend les règles que fixait le client
    (`statistiques_livraison_test.dart`) :

    * la durée de livraison est `delivered_at − placed_at` — le réel, jamais la
      promesse ; une livraison sans horodatage n'entre dans aucun calcul ;
    * la ponctualité ne se juge que sur les commandes qui portaient une heure
      annoncée, et arriver **à** l'heure annoncée, c'est la tenir ;
    * le chiffre d'affaires est **par devise** : les commandes livrées de Lomé
      (XOF) et de Douala (XAF) ne s'additionnent pas.

    La série quotidienne est découpée dans le fuseau des cuisines quand la
    sélection n'en couvre qu'un, en UTC sinon — et la réponse dit lequel.
    """
    base = commandes.order_by()
    comptes = {
        ligne["status"]: ligne["nombre"]
        for ligne in base.values("status").annotate(nombre=Count("id"))
    }
    par_statut = {statut: comptes.get(statut, 0) for statut in OrderStatus.values}
    total = sum(par_statut.values())

    livrees = base.filter(status=OrderStatus.DELIVERED)
    revenus = [
        {
            "currency": ligne["total_currency"],
            "orders_delivered": ligne["livrees"],
            "revenue_minor": ligne["chiffre"] or 0,
            "average_basket_minor": (ligne["chiffre"] or 0) // ligne["livrees"],
        }
        for ligne in livrees.values("total_currency")
        .annotate(livrees=Count("id"), chiffre=Sum("total_minor"))
        .order_by("-chiffre", "total_currency")
    ]

    mesurees = livrees.filter(delivered_at__isnull=False).annotate(
        duree=ExpressionWrapper(F("delivered_at") - F("placed_at"), output_field=DurationField())
    )
    durees = mesurees.aggregate(
        nombre=Count("id"),
        moyenne=Avg("duree"),
        plus_rapide=Min("duree"),
        plus_lente=Max("duree"),
        promises=Count("id", filter=Q(estimated_delivery_at__isnull=False)),
        a_l_heure=Count(
            "id",
            filter=Q(
                estimated_delivery_at__isnull=False,
                delivered_at__lte=F("estimated_delivery_at"),
            ),
        ),
    )

    fuseaux = list(
        base.values_list("restaurant__zone__city__country__timezone", flat=True)
        .distinct()
        .order_by()[:2]
    )
    fuseau = fuseaux[0] if len(fuseaux) == 1 and fuseaux[0] else "UTC"
    debut_serie = depuis or (timezone.now() - PROFONDEUR_PAR_JOUR)
    par_jour = [
        {"day": ligne["jour"], "orders_count": ligne["nombre"]}
        for ligne in base.filter(placed_at__gte=debut_serie)
        .annotate(jour=TruncDate("placed_at", tzinfo=ZoneInfo(fuseau)))
        .values("jour")
        .annotate(nombre=Count("id"))
        .order_by("jour")
    ]

    return {
        "orders_count": total,
        "by_status": par_statut,
        "revenues": revenus,
        "delivery": {
            "measured_orders": durees["nombre"],
            "average_minutes": _minutes(durees["moyenne"]),
            "fastest_minutes": _minutes(durees["plus_rapide"]),
            "slowest_minutes": _minutes(durees["plus_lente"]),
            "on_time_measured": durees["promises"],
            "on_time_rate": (
                round(durees["a_l_heure"] * 100 / durees["promises"], 1)
                if durees["promises"]
                else None
            ),
        },
        "cancellation_rate": (
            round(par_statut[OrderStatus.CANCELLED] * 100 / total, 1) if total else 0.0
        ),
        "per_day": par_jour,
        "per_day_from": debut_serie.date(),
        "timezone_name": fuseau,
    }
