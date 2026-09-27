"""Périmètre d'un membre du personnel — ADR-005, troisième étage.

Le modèle d'autorisation a trois étages : le type de compte, la permission
nommée, et **l'appartenance de la ressource**. Les deux premiers vivent dans
`common.permissions` ; le troisième est ici, et il s'applique dans les
`get_queryset` — pas dans une permission d'objet, sinon la ressource interdite
serait d'abord chargée puis refusée, ce qui trahit son existence par le code de
statut.

Sans ce filtre, « personnel » désigne une population indistincte : un opérateur
du restaurant de Kara lit et fait avancer les commandes de Lomé. La permission
dit ce qu'on a le droit de faire ; ce module dit sur quoi.
"""

from __future__ import annotations

import uuid

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.geography.models import DeliveryZone
from apps.restaurants.models import AreaMembership, Restaurant, StaffMembership
from common.permissions import is_unscoped

__all__ = [
    "assert_can_manage",
    "assert_can_open_in_zone",
    "assert_in_scope",
    "is_in_scope",
    "is_unscoped",
    "staff_restaurant_ids",
    "staff_user_ids_for",
]

# `is_unscoped` vit dans le socle depuis que la géographie en a eu besoin : un
# pays n'appartient à aucun établissement, et `geography` ne connaît pas
# `restaurants` (ADR-002). Il reste exporté ici, où les appelants le
# cherchent — le périmètre du personnel est le sujet de ce module.


def staff_restaurant_ids(user: User) -> set[uuid.UUID]:
    """Établissements sur lesquels ce compte a un rattachement.

    **Le point de passage unique du cloisonnement.** Commandes, catalogue,
    flotte, promotions, fidélité, paiements et rapports le consultent tous ;
    c'est pourquoi le palier pays/ville s'ajoute ici et nulle part ailleurs —
    un directeur pays gagne d'un coup les huit écrans, et un neuvième écrit
    demain en héritera sans qu'on y pense.

    L'union est calculée à la lecture plutôt que recopiée dans une table de
    rattachements : un établissement ouvert ce matin dans le pays d'un
    directeur doit être dans son périmètre cet après-midi, sans qu'on ait à
    repasser sur les comptes. C'était le défaut exact du palier manquant — on
    rattachait le directeur à chacun de ses restaurants, et le suivant lui
    échappait en silence.

    Ensemble vide pour un membre du personnel non rattaché : il ne verra rien,
    et c'est le bon défaut. Une panne visible se corrige en une ligne de
    back-office ; un accès trop large, silencieux, ne se découvre pas.
    """
    directs = set(StaffMembership.objects.filter(user=user).values_list("restaurant_id", flat=True))
    return directs | _restaurants_des_perimetres(user)


def _restaurants_des_perimetres(user: User) -> set[uuid.UUID]:
    """Établissements couverts par les rattachements de marché ou de ville.

    Une seule requête, en `OR` : deux requêtes — l'une par pays, l'autre par
    ville — feraient deux allers-retours à chaque contrôle de périmètre, c'est-
    à-dire à chaque requête de back-office d'un compte cloisonné.

    Sortie immédiate quand le compte n'a aucun rattachement de périmètre : le
    cas est de loin le plus fréquent, et il ne doit rien coûter.
    """
    perimetres = list(AreaMembership.objects.filter(user=user).values_list("country_id", "city_id"))
    if not perimetres:
        return set()

    pays = [country_id for country_id, _ in perimetres if country_id is not None]
    villes = [city_id for _, city_id in perimetres if city_id is not None]

    condition = Q()
    if pays:
        condition |= Q(zone__city__country_id__in=pays)
    if villes:
        condition |= Q(zone__city_id__in=villes)

    return set(Restaurant.objects.filter(condition).values_list("pk", flat=True))


def is_in_scope(user: User, restaurant_id: uuid.UUID) -> bool:
    """Ce compte a-t-il ce restaurant dans son périmètre ?"""
    return is_unscoped(user) or restaurant_id in staff_restaurant_ids(user)


def assert_in_scope(user: User, restaurant_id: uuid.UUID) -> None:
    """Refuse une **écriture** hors périmètre.

    Le filtre de `get_queryset` suffit à cacher ce qu'on n'a pas le droit de
    lire ; il ne peut rien contre une création, qui désigne son établissement
    dans le corps de la requête. Sans cette garde, un opérateur de Kara
    ajouterait un article à la carte de Lomé — l'objet n'existe pas encore, il
    n'y a donc aucun `get_object` pour le refuser.

    Le refus est explicite (403) et non un « introuvable » : contrairement à la
    lecture, il n'y a ici aucune existence à trahir, et un message clair évite
    de faire chercher une panne de configuration là où il y a un droit
    manquant.
    """
    if not is_in_scope(user, restaurant_id):
        raise PermissionDenied(
            "Cet établissement n'est pas dans votre périmètre : "
            "un rattachement est nécessaire pour y écrire."
        )


def assert_can_open_in_zone(
    user: User, zone: DeliveryZone, quoi: str = "L'ouverture d'un établissement"
) -> None:
    """Refuse d'ouvrir un établissement hors de son marché.

    Ouvrir relevait du siège **et de lui seul**, et c'était la bonne règle tant
    que le seul autre palier était l'établissement : une création par un compte
    rattaché à un restaurant lui aurait attribué un second périmètre en une
    requête, ce qui vide le cloisonnement de son sens.

    Un rattachement de marché change la donne, parce qu'il ne s'élargit pas :
    un directeur du Togo qui ouvre à Kara reste dans le périmètre qu'on lui a
    donné — l'établissement neuf y tombe **parce qu'il est au Togo**, pas parce
    qu'il vient de le créer. Ouvrir en Côte d'Ivoire, en revanche, lui est
    refusé exactement comme avant.

    Le contrôle porte sur la **zone visée** et non sur l'utilisateur seul : la
    zone emporte la ville, donc le pays. C'est aussi ce qui rend le changement
    de zone d'un établissement existant sûr — déplacer un restaurant vers un
    autre marché est la même écriture qu'une ouverture dans ce marché.
    """
    if is_unscoped(user):
        return

    perimetres = AreaMembership.objects.filter(user=user)
    autorise = perimetres.filter(
        Q(country_id=zone.city.country_id) | Q(city_id=zone.city_id)
    ).exists()

    if not autorise:
        raise PermissionDenied(
            f"{quoi} à {zone.city.name} ({zone.city.country.iso_code}) sort de votre périmètre."
        )


def staff_user_ids_for(restaurant_id: uuid.UUID) -> set[uuid.UUID]:
    """Comptes du personnel dont le périmètre couvre cet établissement.

    **Le miroir de `staff_restaurant_ids`.** Celui-ci répond « quels
    établissements pour ce compte », celui-là « quels comptes pour cet
    établissement ». La même relation, lue dans l'autre sens — et il faut les
    deux, parce que les vues partent du compte tandis que les notifications
    partent de l'établissement.

    Écrire la seconde en oubliant les rattachements de marché est le défaut que
    l'ajout du palier pays/ville rendait possible : un directeur pays aurait vu
    les commandes de son marché dans le back-office sans jamais être prévenu
    qu'il en arrivait une. Les deux lectures doivent désigner la même
    population, sans quoi la notification renvoie vers un écran qui refuse, ou
    se tait sur un écran qui accepte.
    """
    directs = set(
        StaffMembership.objects.filter(restaurant_id=restaurant_id).values_list(
            "user_id", flat=True
        )
    )

    situation = (
        Restaurant.objects.filter(pk=restaurant_id)
        .values_list("zone__city_id", "zone__city__country_id")
        .first()
    )
    if situation is None:
        return directs

    city_id, country_id = situation
    par_perimetre = set(
        AreaMembership.objects.filter(Q(city_id=city_id) | Q(country_id=country_id)).values_list(
            "user_id", flat=True
        )
    )
    return directs | par_perimetre


def assert_can_manage(actor: User, target: User) -> None:
    """Refuse de toucher à un compte du personnel qui **dépasse** l'acteur.

    `StaffViewSet` montrait à un gérant muni de `roles.write` tout collègue
    rattaché à l'un de ses établissements — et le laissait tout modifier. Or
    voir un collègue n'est pas en répondre : remplacer le mot de passe d'un
    compte, c'est se connecter à sa place, donc hériter de ses droits et de son
    périmètre. Un superutilisateur rattaché à Lomé, un caissier qui détient
    `orders.refund`, un directeur pays : chacun se reprenait en une requête.

    Un compte est du ressort de l'acteur quand il ne le dépasse sur aucun axe :

    * **le statut** — un superutilisateur ne relève que du siège ;
    * **les droits** — ses permissions sont incluses dans celles de l'acteur ;
    * **le périmètre** — ses établissements, et ses rattachements de marché ou
      de ville, sont couverts par ceux de l'acteur.

    Le périmètre compare aussi les rattachements eux-mêmes, et pas seulement
    les établissements qu'ils couvrent : un directeur d'un pays où rien n'est
    encore ouvert ne couvre aucun établissement, et paraîtrait sinon plus
    étroit que le gérant qu'il supervise.

    Le siège n'est pas concerné. Le refus est explicite (403) : le compte est
    visible, il n'y a pas d'existence à taire.
    """
    if is_unscoped(actor):
        return

    motifs: list[str] = []
    if target.is_superuser:
        motifs.append("c'est un compte du siège")
    if not target.permission_codes() <= actor.permission_codes():
        motifs.append("il détient des permissions que vous n'avez pas")

    hors_etablissements = staff_restaurant_ids(target) - staff_restaurant_ids(actor)
    couverts = list(AreaMembership.objects.filter(user=actor).values_list("country_id", "city_id"))
    pays_couverts = {pays for pays, _ in couverts if pays is not None}
    villes_couvertes = {ville for _, ville in couverts if ville is not None}
    hors_marches = [
        (pays, ville)
        for pays, ville, pays_de_la_ville in AreaMembership.objects.filter(user=target).values_list(
            "country_id", "city_id", "city__country_id"
        )
        if (pays is not None and pays not in pays_couverts)
        or (
            ville is not None
            and ville not in villes_couvertes
            and pays_de_la_ville not in pays_couverts
        )
    ]
    if hors_etablissements or hors_marches:
        motifs.append("son périmètre dépasse le vôtre")

    if motifs:
        raise PermissionDenied(
            "Ce compte n'est pas de votre ressort : "
            + " ; ".join(motifs)
            + ". Sa gestion relève du siège, ou d'un responsable qui le couvre."
        )
