"""Crée ou met à jour les rôles système.

Idempotente : rejouable à chaque déploiement. C'est ce qui permet d'ajouter une
permission au registre et de la propager aux rôles livrés sans intervention
manuelle en base — et sans écraser les rôles sur mesure créés par le client.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Role
from apps.accounts.permissions import SYSTEM_ROLES


class Command(BaseCommand):
    help = "Crée ou met à jour les rôles système (Super Admin, Manager, Opérateur)."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for name, permissions in SYSTEM_ROLES.items():
            role, created = Role.objects.get_or_create(
                name=name,
                defaults={"permissions": list(permissions), "is_system": True},
            )
            if not created:
                # Les permissions des rôles système sont réalignées sur le
                # registre : elles font partie du code, pas de la configuration
                # client. Un rôle personnalisé, lui, n'est jamais touché — il
                # n'est pas dans SYSTEM_ROLES.
                role.permissions = list(permissions)
                role.is_system = True
                role.save(update_fields=["permissions", "is_system"])

            verbe = "créé" if created else "mis à jour"
            self.stdout.write(
                self.style.SUCCESS(f"Rôle « {name} » {verbe} ({len(permissions)} permissions)")
            )
