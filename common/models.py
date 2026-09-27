"""Modèles de base.

Ces classes abstraites portent les décisions transverses (ADR-007, ADR-010) une
seule fois. Un modèle métier qui n'hérite pas de `UUIDModel` est une anomalie :
un test d'architecture le signale.
"""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

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


class AuditEntry(UUIDModel, TimeStampedModel):
    """Une décision d'exploitation, avec sa valeur d'avant.

    **Le seul modèle concret de `common`.** Il y vit parce qu'il est
    véritablement transverse — un pays, une zone et un établissement s'y
    consignent de la même façon — et parce qu'aucune autre application ne peut
    l'héberger sans que les deux autres aient à en dépendre.

    Voir `common.audit` pour ce qui est journalisé, ce qui ne l'est pas, et
    pourquoi la cible n'est pas une clé étrangère.
    """

    # `SET_NULL` : un compte du personnel se désactive mais ne se supprime pas,
    # et si l'exception arrivait, le journal doit survivre à son auteur — c'est
    # précisément dans ce cas qu'on l'ouvre.
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, related_name="audit_entries"
    )

    action = models.CharField(max_length=64, help_text="Par exemple `zone.tariff`.")

    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=64)

    #: Libellé figé au moment du changement — « zone Cocody », « El Corazón
    #: Lomé ». Recopié plutôt que joint : il doit rester lisible même si la
    #: cible a depuis été renommée ou retirée.
    target_label = models.CharField(max_length=200)

    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)

    #: L'établissement auquel la décision se rattache, quand elle en a un.
    #:
    #: ## Pourquoi il est porté par l'entrée, et non déduit de la cible
    #:
    #: Le journal est cloisonné comme le reste (`AuditEntryViewSet`), et pour
    #: les trois premières cibles — établissement, zone, compte du personnel —
    #: le rattachement se déduisait de l'identifiant. Un retrait livreur, un
    #: remboursement, une réclamation tranchée n'ont pas cette propriété : leur
    #: établissement se lit sur `payments` et `support`, que `restaurants` —
    #: où vit le périmètre — n'a pas le droit de connaître (ADR-002).
    #:
    #: Déduire aurait donc demandé d'inverser le graphe, ou de recopier trois
    #: requêtes dans la vue. Écrire le périmètre **au moment de la décision**
    #: évite les deux, et a une seconde vertu : comme `target_label`, il fige
    #: ce qui était vrai alors. Un livreur muté ailleurs ne déplace pas
    #: l'historique de ses versements.
    #:
    #: Nul pour ce qui ne relève d'aucun établissement — un rôle, un pays, un
    #: compte client — qui ne se lit qu'au siège, le défaut sûr.
    scope_restaurant_id = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "entrée de journal"
        verbose_name_plural = "entrées de journal"
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # La lecture courante est « l'historique de cet objet » : on ouvre
            # le journal depuis une fiche, jamais en balayant la table.
            models.Index(fields=["target_type", "target_id", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} — {self.target_label}"
