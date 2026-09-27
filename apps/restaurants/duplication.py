"""Duplication d'un établissement — ADR-002, ADR-006.

Ouvrir El Corazón Abidjan en repartant d'El Corazón Lomé, c'est recopier une
carte de quarante articles, leurs groupes d'options et sept plages horaires. Fait
à la main dans le back-office, c'est une demi-journée et des oublis ; fait ici,
c'est une requête.

## Ce qui ne se duplique **jamais**

Commandes, clients, livreurs, paiements, historiques, statistiques. Ce n'est pas
une option qu'on aurait laissée décochée par défaut : ces objets ne sont pas
dans le registre, et il n'existe donc aucun chemin de code pour les copier. Une
option, même par défaut à `false`, finirait un jour cochée par erreur — et un
établissement neuf naîtrait avec le chiffre d'affaires d'un autre.

La raison est plus forte qu'une préférence : une commande porte un client, une
adresse de livraison, un encaissement et un livreur. Recopiée ailleurs, elle
duplique une écriture comptable.

## Ce qui ne se duplique pas non plus, pour une autre raison

**Les zones de livraison.** Elles n'appartiennent pas à l'établissement mais à
la ville (ADR-006), et leur contour est un polygone géographique réel. Copier le
contour de Lomé vers Abidjan poserait un périmètre de livraison à 900 km de
l'établissement — dans le golfe de Guinée. La zone de la cible est donc
**choisie**, jamais copiée : c'est le champ `zone` de la requête, et c'est lui
qui emporte la ville, le pays, la devise et le fuseau.

## Pourquoi un registre plutôt qu'un import

`restaurants` n'a pas le droit de connaître `catalog` : son graphe autorisé
s'arrête à `accounts` et `geography`, et l'arête inverse existe déjà
(`catalog → restaurants`). L'importer ici créerait un cycle, que le test
d'architecture refuse.

C'est exactement le problème qu'a résolu `apps.restaurants.readiness`, et la
solution est la même : `catalog` **s'abonne** au moment de son `ready()`.
L'arête va de l'abonné vers l'émetteur, elle est déclarée dans le graphe, et
elle est vérifiée en CI.

Une application non chargée retire une section de la liste au lieu de lever :
le sens de défaillance correct ici est « on a copié moins », jamais « on a
copié autre chose ».
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - uniquement pour l'annotation
    from apps.restaurants.models import OpeningHours, Restaurant

__all__ = [
    "SECTION_GENERAL",
    "SECTION_OPENING_HOURS",
    "SectionCopier",
    "copy_sections",
    "known_sections",
    "register_section",
]

#: Un copieur reçoit la source et la cible, et rend le nombre d'objets créés.
#:
#: Le décompte n'est pas décoratif : c'est ce que la réponse rend à l'écran, et
#: c'est la seule façon pour l'exploitation de voir qu'une carte de quarante
#: articles est bien arrivée entière avant de publier l'établissement.
SectionCopier = Callable[["Restaurant", "Restaurant"], int]

#: Description, délai de préparation, courriel — portés par `restaurants`
#: lui-même, donc appliqués à la création plutôt que par un copieur.
SECTION_GENERAL = "general"

#: Plages d'ouverture. Portées par `restaurants`, mais copiées **après** la
#: création : ce sont des lignes d'une table liée, pas des champs de la fiche.
SECTION_OPENING_HOURS = "opening_hours"

_COPIEURS: dict[str, SectionCopier] = {}


def register_section(nom: str, copieur: SectionCopier) -> None:
    """Abonne une section duplicable, depuis le `ready()` d'une application.

    Idempotent : `ready()` peut être appelé deux fois dans une même session de
    test. Réenregistrer la même section écraserait simplement l'entrée, ce qui
    est sans effet — mais une section enregistrée deux fois sous deux noms
    copierait deux fois le même catalogue, et c'est ce qu'on veut rendre
    impossible à écrire par accident.
    """
    _COPIEURS[nom] = copieur


def known_sections() -> set[str]:
    """Toutes les sections copiables, y compris celles que `restaurants` porte.

    Sert à valider la requête : une section inconnue est refusée à la saisie
    plutôt qu'ignorée en silence. Une faute de frappe dans `["catalogue"]` —
    au lieu de `["catalog"]` — produirait sinon un établissement vide dont
    personne ne comprendrait pourquoi la carte n'a pas suivi.
    """
    return {SECTION_GENERAL, SECTION_OPENING_HOURS, *_COPIEURS}


def copy_sections(*, source: Restaurant, cible: Restaurant, sections: set[str]) -> dict[str, int]:
    """Applique les copieurs demandés, et rend ce que chacun a créé.

    `SECTION_GENERAL` n'apparaît pas ici : ses champs sont posés au moment de
    la création de la cible, avant que celle-ci existe en base. Les copieurs,
    eux, ont besoin d'une cible enregistrée pour y rattacher des lignes.
    """
    from apps.restaurants.models import OpeningHours

    resultat: dict[str, int] = {}

    if SECTION_OPENING_HOURS in sections:
        resultat[SECTION_OPENING_HOURS] = _copier_les_horaires(source, cible, OpeningHours)

    for nom, copieur in _COPIEURS.items():
        if nom in sections:
            resultat[nom] = copieur(source, cible)

    return resultat


def _copier_les_horaires(source: Restaurant, cible: Restaurant, modele: type[OpeningHours]) -> int:
    """Recopie les plages d'ouverture, telles quelles.

    Les heures ne sont pas converties d'un fuseau à l'autre, et c'est
    volontaire : « ouvert de 11 h à 23 h » est une décision d'exploitation
    locale, pas un instant absolu. Un restaurant d'Abidjan ouvre à 11 h
    d'Abidjan, non à 10 h parce que Lomé ouvrait à 11 h.
    """
    plages = [
        modele(
            restaurant=cible,
            weekday=plage.weekday,
            opens_at=plage.opens_at,
            closes_at=plage.closes_at,
        )
        for plage in source.opening_hours.all()
    ]
    modele.objects.bulk_create(plages)
    return len(plages)
