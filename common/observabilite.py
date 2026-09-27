"""Corrélation des journaux — un identifiant par requête.

## Le problème que ce module résout

Les journaux sortent déjà en JSON structuré (`config/logging.py`), ce qui les
rend interrogeables par champ. Mais rien ne reliait entre elles les lignes
d'une **même** requête : sous quelques requêtes par seconde, les traces d'un
incident se mêlent à celles de tout le monde, et reconstituer ce qui s'est
passé pour *un* client demande de deviner.

C'est particulièrement coûteux là où ça compte le plus. Un paiement traverse la
vue, le service, le connecteur du prestataire et la machine à états ; un
incident y produit quatre lignes que rien ne rapproche.

## Ce que le module fait, et ce qu'il ne fait pas

Il pose un identifiant par requête, l'ajoute à **toute** ligne journalisée
pendant son traitement, et le rend au client dans un en-tête. Il ne trace pas
les temps de réponse et n'échantillonne rien : ce sont des métriques, elles
appartiennent à un autre outil.

## Pourquoi un `ContextVar` et non un attribut de requête

Les appelants qui journalisent — un service, un connecteur — n'ont pas la
requête sous la main, et la leur faire passer contaminerait leur signature pour
un besoin d'observabilité. Un `ContextVar` est visible de tout le pile d'appels
sans être un argument, et il est propre à la tâche asynchrone courante : deux
requêtes traitées en parallèle par le même processus ne mélangent pas leurs
identifiants, ce qu'une variable globale ferait.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from django.http import HttpRequest, HttpResponse

__all__ = ["ENTETE", "ID_REQUETE", "FiltreDeCorrelation", "MiddlewareDIdentifiant"]

#: En-tête lu à l'entrée et rendu à la sortie.
#:
#: Lu à l'entrée parce qu'un proxy ou une application cliente peut déjà en
#: poser un : le reprendre fait que la trace traverse la frontière au lieu de
#: recommencer à chaque saut.
ENTETE = "X-Request-ID"

#: L'identifiant de la requête en cours de traitement, ou une chaîne vide hors
#: requête — une tâche Celery, une commande d'administration.
ID_REQUETE: ContextVar[str] = ContextVar("id_requete", default="")


class FiltreDeCorrelation(logging.Filter):
    """Ajoute l'identifiant courant à chaque enregistrement.

    Un filtre et non un adaptateur : il s'applique à **tous** les journaux, y
    compris ceux de Django et des bibliothèques tierces, sans que personne ait
    à changer sa façon d'appeler `logger`.

    `JSONFormatter` recopie tout attribut non réservé du `LogRecord` dans sa
    sortie : poser l'attribut suffit à le faire apparaître, il n'y a pas de
    format à tenir à jour en parallèle.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        identifiant = ID_REQUETE.get()
        if identifiant:
            record.request_id = identifiant
        # Toujours vrai : ce filtre enrichit, il ne filtre rien. C'est
        # l'interface que Django impose pour se greffer sur la chaîne.
        return True


class MiddlewareDIdentifiant:
    """Pose l'identifiant à l'entrée, le rend à la sortie.

    Placé **en tête** de la chaîne dans `base.py` : une exception levée par un
    middleware situé plus bas doit encore porter l'identifiant, faute de quoi
    les incidents les plus graves seraient précisément ceux qu'on ne saurait
    pas relier.

    L'identifiant est rendu au client dans l'en-tête de réponse. C'est ce qui
    permet à quelqu'un qui signale un problème de citer une valeur qu'on
    retrouve dans les journaux — sans elle, un rapport se réduit à « ça n'a pas
    marché tout à l'heure ».
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Une valeur reçue est reprise, mais **bornée** : elle vient du client,
        # elle finit dans les journaux, et un en-tête de plusieurs kilo-octets
        # y serait recopié à chaque ligne.
        recu = request.headers.get(ENTETE, "").strip()[:64]
        identifiant = recu or uuid.uuid4().hex

        jeton = ID_REQUETE.set(identifiant)
        try:
            response = self.get_response(request)
        finally:
            # Restauré même sur exception : sans cela, le contexte fuirait vers
            # la requête suivante servie par le même fil.
            ID_REQUETE.reset(jeton)

        response.headers[ENTETE] = identifiant
        return response
