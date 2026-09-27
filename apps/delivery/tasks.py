"""Tâches asynchrones de la livraison."""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.delivery.dispatch import DispatchService
from apps.delivery.models import CourierProfile, DocumentReminder
from apps.delivery.services import CourierService
from apps.delivery.signals import document_expiring
from apps.delivery.states import VerificationStatus

__all__ = ["expire_stale_offers", "remind_document_expiry"]

#: Jours avant l'échéance où l'on prévient. Un mois pour renouveler un permis,
#: une semaine pour relancer, la veille et le jour même pour ne pas l'oublier.
#: Des seuils plutôt qu'un rappel quotidien : prévenir chaque jour pendant un
#: mois, c'est apprendre à tout le monde à ignorer l'avis.
RAPPELS_EN_JOURS = (30, 7, 1, 0)


@shared_task
def expire_stale_offers() -> dict[str, int]:
    """Clôt les propositions sans réponse, puis propose au suivant.

    Planifiée chaque minute : une proposition sans réponse bloque un repas
    prêt.
    """
    closes = DispatchService.expire_stale_offers()
    proposees = DispatchService.dispatch_waiting()
    return {"expired": closes, "offered": proposees}


@shared_task
def remind_document_expiry() -> dict[str, int]:
    """Prévient livreurs et équipe des pièces qui arrivent à échéance.

    Seuls les dossiers **validés** sont concernés : un dossier en attente ou
    refusé n'est pas en service, et son instruction dira ce qui manque.

    ## Un seuil atteint, pas un jour exact

    La première écriture comparait l'échéance à `aujourd'hui + 30` (puis 7, 1,
    0) : un rappel ne partait que si la tâche tournait **ce jour-là**. Deux
    situations ordinaires le mettaient en défaut — `beat` redémarré (son
    dernier passage vit dans un fichier que le redéploiement efface) et worker
    indisponible quelques heures — et le rappel manqué ne revenait jamais.

    Chaque pièce est donc comparée aux seuils : le plus petit seuil atteint et
    **non encore envoyé** part, et une seule fois, l'envoi étant consigné
    (`DocumentReminder`). La tâche peut tourner cent fois par jour, être
    relancée à la main ou rattraper trois jours de retard — elle ne dira jamais
    deux fois la même chose, ni ne sautera une échéance.
    """
    aujourd_hui = timezone.localdate()
    horizon = aujourd_hui + dt.timedelta(days=max(RAPPELS_EN_JOURS))

    concernes = CourierProfile.objects.filter(
        _au_moins_une_echeance_avant(horizon), verification_status=VerificationStatus.APPROVED
    ).select_related("user")

    envoyes = 0
    for courier in concernes:
        for piece, champ in CourierService.PIECES_DATEES.items():
            echeance = getattr(courier, champ)
            if echeance is None:
                continue
            seuil = _seuil_atteint(echeance, aujourd_hui)
            if seuil is None or not _consigner(courier, piece, echeance, seuil):
                continue
            document_expiring.send(
                sender=CourierProfile,
                courier=courier,
                piece=piece,
                expires_on=echeance,
                days_left=(echeance - aujourd_hui).days,
            )
            envoyes += 1
    return {"reminders": envoyes}


def _au_moins_une_echeance_avant(horizon: dt.date) -> Q:
    """Les dossiers dont une pièce au moins tombe dans la fenêtre des rappels.

    Filtrer en base plutôt que balayer la flotte : à deux cents livreurs, la
    quasi-totalité des dossiers n'a rien à dire un jour donné.
    """
    filtre = Q()
    for champ in CourierService.PIECES_DATEES.values():
        filtre |= Q(**{f"{champ}__lte": horizon})
    return filtre


def _seuil_atteint(echeance: dt.date, aujourd_hui: dt.date) -> int | None:
    """Le plus petit seuil que cette échéance a franchi, ou `None`.

    À sept jours de l'échéance, le seuil atteint est 7 ; à cinq jours — parce
    que la tâche n'a pas tourné depuis six — c'est encore 7, et le rappel de
    J-7 part avec cinq jours de retard plutôt que pas du tout. Le lendemain, le
    seuil 7 est déjà consigné et rien ne repart.

    Une pièce **déjà expirée** rend 0 : elle relève du dernier rappel, celui du
    jour même, qui n'a peut-être jamais été envoyé.
    """
    restants = (echeance - aujourd_hui).days
    atteints = [seuil for seuil in RAPPELS_EN_JOURS if restants <= seuil]
    return min(atteints) if atteints else None


def _consigner(courier: CourierProfile, piece: str, echeance: dt.date, seuil: int) -> bool:
    """Écrit le rappel, et dit s'il faut l'envoyer.

    L'unicité est **en base** : deux exécutions concurrentes — un second `beat`
    resté debout après un redéploiement — liraient toutes deux « pas encore
    envoyé », et le livreur recevrait l'avis en double. Le conflit d'insertion
    tranche, ce qu'aucune lecture préalable ne saurait faire.
    """
    try:
        with transaction.atomic():
            DocumentReminder.objects.create(
                courier=courier, piece=piece, expires_on=echeance, threshold_days=seuil
            )
    except IntegrityError:
        return False
    return True
