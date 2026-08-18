"""Registre des permissions du personnel — ADR-005.

Le code ne teste **jamais** un nom de rôle, seulement une permission. C'est ce
qui permet de créer un rôle sur mesure sans redéployer, et c'est ce qui manquait
à l'implémentation précédente : ses rôles admin n'étaient appliqués que côté
interface, si bien qu'un « Opérateur » privé du module marketing pouvait
appeler l'API marketing sans obstacle.

Le registre est exhaustif et fermé : une permission absente d'ici ne peut pas
être accordée. Une permission oubliée bloque une fonctionnalité — panne visible
et corrigible — plutôt que d'ouvrir un accès trop large, silencieux et
exploitable.
"""

from __future__ import annotations

from typing import Final

__all__ = ["PERMISSIONS", "PERMISSION_CHOICES", "SYSTEM_ROLES", "validate_permissions"]


# Vocabulaire : `domaine.action`. Le domaine correspond à une app métier, ce qui
# rend la trace d'audit lisible sans connaître le code.
PERMISSIONS: Final[dict[str, str]] = {
    # Catalogue
    "catalog.read": "Consulter le catalogue et les stocks",
    "catalog.write": "Créer et modifier articles, catégories et options",
    # Commandes
    "orders.read": "Consulter les commandes",
    "orders.update_status": "Faire avancer le statut d'une commande",
    "orders.assign_courier": "Affecter un livreur à une commande",
    "orders.cancel": "Annuler une commande",
    "orders.refund": "Rembourser tout ou partie d'une commande",
    # Flotte
    "couriers.read": "Consulter les livreurs et leur position",
    "couriers.write": "Créer un compte livreur",
    "couriers.approve": "Valider ou rejeter un dossier livreur",
    "couriers.suspend": "Suspendre un livreur",
    # Clients
    "customers.read": "Consulter les comptes clients",
    "customers.block": "Bloquer ou débloquer un compte client",
    # Commercial
    "promotions.read": "Consulter les promotions",
    "promotions.write": "Créer et modifier des promotions",
    "notifications.send": "Envoyer une notification ciblée",
    # Fidélisation — le catalogue de ce qui se gagne et de ce qui s'échange.
    # Distinct des promotions : un code promotionnel remise une commande, une
    # récompense se paie en points, qu'un client a accumulés.
    "loyalty.read": "Consulter le catalogue de récompenses",
    "loyalty.write": "Créer et modifier des récompenses",
    "gamification.read": "Consulter succès, badges et défis",
    "gamification.write": "Créer et modifier succès, badges et défis",
    # Exploitation
    "restaurants.read": "Consulter les établissements et leurs zones",
    "restaurants.write": "Créer et modifier établissements et zones",
    "analytics.read": "Consulter les statistiques et rapports",
    # Administration
    "roles.read": "Consulter les rôles, les permissions et les comptes du personnel",
    "roles.write": "Créer et modifier des rôles, et les attribuer au personnel",
}

PERMISSION_CHOICES: Final[list[tuple[str, str]]] = sorted(PERMISSIONS.items())


# Rôles fournis à l'installation. Ce ne sont que des **groupements** de
# permissions : ils sont modifiables, et le code ne les connaît pas.
SYSTEM_ROLES: Final[dict[str, tuple[str, ...]]] = {
    "Super Admin": tuple(PERMISSIONS),
    "Manager": (
        "catalog.read",
        "catalog.write",
        "orders.read",
        "orders.update_status",
        "orders.assign_courier",
        "orders.cancel",
        "orders.refund",
        "couriers.read",
        "couriers.write",
        "couriers.approve",
        "couriers.suspend",
        "customers.read",
        "promotions.read",
        "promotions.write",
        "notifications.send",
        "loyalty.read",
        "loyalty.write",
        "gamification.read",
        "gamification.write",
        "restaurants.read",
        "analytics.read",
    ),
    "Opérateur": (
        "catalog.read",
        "orders.read",
        "orders.update_status",
        "orders.assign_courier",
        "couriers.read",
        "customers.read",
    ),
}


def validate_permissions(codes: list[str]) -> None:
    """Refuse toute permission hors registre.

    Sans cette garde, une faute de frappe (`orders.refunds`) produirait un rôle
    qui semble accorder un droit mais n'en accorde aucun — le pire des deux
    mondes : ni fonctionnel, ni détecté.
    """
    unknown = sorted(set(codes) - set(PERMISSIONS))
    if unknown:
        from django.core.exceptions import ValidationError

        raise ValidationError(
            f"Permissions inconnues : {', '.join(unknown)}. "
            f"Valeurs admises : {', '.join(sorted(PERMISSIONS))}."
        )
