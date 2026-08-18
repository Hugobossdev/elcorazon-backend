"""Modèles de base.

Ces classes abstraites portent les décisions transverses (ADR-007, ADR-010) une
seule fois. Un modèle métier qui n'hérite pas de `UUIDModel` est une anomalie :
un test d'architecture le signale.
"""

from __future__ import annotations

from typing import Any, TypeVar

from django.db import models
from django.utils import timezone

from common.identifiers import uuid7
from common.state_machine import StateMachine

__all__ = [
    "PositiveAmountModel",
    "SoftDeleteManager",
    "SoftDeleteModel",
    "SoftDeleteQuerySet",
    "TimeStampedModel",
    "UUIDModel",
    "state_check_constraint",
]


class UUIDModel(models.Model):
    """Clé primaire UUIDv7 — opaque et ordonnée (ADR-007)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


_Model = TypeVar("_Model", bound=models.Model)


class SoftDeleteQuerySet(models.QuerySet[_Model]):
    """QuerySet générique : `MenuItem.objects.alive()` reste un
    `QuerySet[MenuItem]`, et non un `QuerySet[SoftDeleteModel]` sur lequel
    aucun champ concret ne serait résolvable."""

    def alive(self) -> SoftDeleteQuerySet[_Model]:
        return self.filter(deleted_at__isnull=True)

    def delete(self) -> tuple[int, dict[str, int]]:
        count = self.update(deleted_at=timezone.now())
        return count, {}


SoftDeleteManager = models.Manager.from_queryset(SoftDeleteQuerySet)


class SoftDeleteModel(models.Model):
    """Suppression logique.

    Réservée aux entités auxquelles des écritures comptables se réfèrent : un
    article de menu retiré du catalogue doit rester lisible depuis les commandes
    passées, sinon l'historique devient incohérent.

    À ne **pas** appliquer partout : une adresse supprimée par un client doit
    l'être réellement (RGPD, droit à l'effacement). Le critère est « une écriture
    financière y renvoie-t-elle ? ».
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = SoftDeleteManager()

    class Meta:
        abstract = True

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
        return 1, {}

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class PositiveAmountModel(models.Model):
    """Refuse un montant nul ou négatif **avant** de heurter la base.

    Les modèles d'encaissement portent tous une contrainte `CHECK ... > 0`
    (`transaction_amount_positive`, `share_amount_positive`,
    `refund_amount_positive`). Elle est la bonne dernière ligne de défense
    (ADR-010) mais une mauvaise *première* : quand elle se déclenche, l'appelant
    reçoit un `IntegrityError` — donc un 500 — au milieu d'un passage de
    commande, avec pour seule indication le nom d'une contrainte SQL. Pire, elle
    casse la transaction en cours, si bien que le code qui voudrait rattraper
    l'erreur ne peut plus émettre la moindre requête.

    Ce garde-fou rend la même règle lisible et rattrapable, et il la rend surtout
    **inévitable** : il vaut pour tout chemin d'écriture, y compris ceux qui ne
    passent pas par un service — back-office, commande d'exploitation, migration
    de données, `save()` appelé depuis un shell.

    Les sous-classes déclarent les champs concernés dans `POSITIVE_AMOUNTS`.
    """

    #: Noms des `MoneyField` qui doivent être strictement positifs.
    POSITIVE_AMOUNTS: tuple[str, ...] = ()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Import différé : `common.exceptions` tire DRF, et un modèle qui en
        # dépendrait à l'import rendrait le domaine inutilisable sans le
        # transport — exactement ce qu'interdit `test_un_modele_n_importe_ni_vue
        # _ni_serialiseur`. Ici la dépendance n'existe qu'au moment du refus.
        from common.exceptions import BusinessRuleViolation

        # `update_fields` restreint l'écriture : ne valider que ce qui part
        # réellement en base, sinon un `save(update_fields=["status"])` sur une
        # ligne ancienne se ferait refuser pour un montant qu'il ne touche pas.
        update_fields = kwargs.get("update_fields")
        touches = None if update_fields is None else set(update_fields)

        for field in self.POSITIVE_AMOUNTS:
            if touches is not None and not touches & {field, f"{field}_minor"}:
                continue
            amount = getattr(self, field, None)
            if amount is None:
                continue
            if not amount.is_positive:
                raise BusinessRuleViolation(
                    f"Le montant « {field} » doit être strictement positif (reçu {amount}).",
                    field=field,
                    received=str(amount.amount_minor),
                )

        super().save(*args, **kwargs)


def state_check_constraint(machine: StateMachine, field: str, name: str) -> models.CheckConstraint:
    """Contrainte `CHECK` dérivée d'une machine à états.

    Le code applicatif est la première ligne de défense, le schéma est la
    dernière (ADR-010). Générer la contrainte depuis la machine garantit que
    les deux ne peuvent pas diverger — c'est exactement la divergence qui a
    produit C4, où le code écrivait un statut absent de l'énumération SQL.
    """
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__in": sorted(machine.states)}),
        name=name,
    )
