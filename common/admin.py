"""Socle du back-office.

Le back-office est un outil d'**exploitation**, pas une seconde API. Il sert à
valider un dossier livreur, retrouver une transaction, corriger une faute de
frappe sur une adresse — pas à faire avancer une commande à la main.

Cette distinction commande tout ce qui suit. Django admin propose par défaut un
formulaire d'édition sur chaque champ, y compris `status` : une liste
déroulante suffirait alors à écrire `delivered` sur une commande jamais partie,
sans passer par la machine à états, sans journal, sans les effets de bord qui
vont avec. Ce serait rouvrir C3 et C4 par la porte de service, après les avoir
fermés partout ailleurs.

D'où trois règles, portées par les classes de ce module :

* **les statuts ne sont jamais des champs** — ils sont en lecture seule, et les
  faire avancer passe par une action qui appelle le service ;
* **les montants ne s'éditent pas** — ils sont calculés (C2), et un total saisi
  à la main serait un total faux qui a l'air juste ;
* **les écritures comptables ne se suppriment pas** — commandes, lignes,
  transactions, remboursements et journaux d'événements sont conservés.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest
from django.utils.html import format_html

__all__ = [
    "AccountingAdmin",
    "ReadOnlyAdmin",
    "money_display",
]


class ReadOnlyAdmin(admin.ModelAdmin):
    """Consultation seule — ni création, ni modification, ni suppression.

    Pour tout ce que le système produit lui-même : un relevé de position, un
    événement de webhook, une notification envoyée. Les modifier n'aurait pas
    de sens, et pouvoir le faire créerait un doute sur ce qu'on lit.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class AccountingAdmin(admin.ModelAdmin):
    """Écriture comptable : consultable, jamais supprimable, jamais créable ici.

    Une commande naît d'un panier validé, une transaction d'un encaissement.
    Les créer à la main produirait des lignes sans contrepartie — une commande
    sans panier, un encaissement sans prestataire — que le rapprochement
    comptable ne saurait pas expliquer.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def money_display(field: str, label: str) -> Any:
    """Colonne d'affichage d'un `MoneyField`.

    `MoneyField` n'est pas un champ Django mais deux colonnes derrière une
    propriété : l'admin ne sait pas l'afficher seul. Sans ce descripteur, la
    liste montrerait `total_minor` et `total_currency` en deux colonnes
    séparées — lisibles par personne.
    """

    @admin.display(description=label, ordering=f"{field}_minor")
    def column(_self: Any, obj: models.Model) -> str:
        amount = getattr(obj, field, None)
        return "—" if amount is None else str(amount)

    return column


def file_link(field: str, label: str) -> Any:
    """Lien vers une pièce jointe, plutôt que son chemin brut.

    Les pièces d'identité sont sur un stockage privé dont les URL signées
    expirent : afficher le chemin ne servirait à rien, et le lien est régénéré
    à chaque affichage.
    """

    @admin.display(description=label)
    def column(_self: Any, obj: models.Model) -> str:
        document = getattr(obj, field, None)
        if not document:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener">Ouvrir</a>', document.url)

    return column
