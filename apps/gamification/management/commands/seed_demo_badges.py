"""Peuple un catalogue de badges de démo.

Sert à valider la migration Flutter de la gamification (Phase 6, `fastfood`) :
sans données, l'écran badges resterait vide. Catalogue global, pas de
restaurant (voir `Badge`, adossé à `PointsAccount.lifetime_earned`).
Idempotente (`get_or_create` sur `title`, unique en base).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.gamification.models import Badge

# (titre, description, icône, seuil en points gagnés à vie)
DEMO_BADGES: list[tuple[str, str, str, int]] = [
    ("Premier pas", "Gagnez vos 50 premiers points de fidélité.", "🌱", 50),
    ("Habitué", "Gagnez 200 points de fidélité au total.", "🔥", 200),
    ("Fidèle parmi les fidèles", "Gagnez 500 points de fidélité au total.", "👑", 500),
]


class Command(BaseCommand):
    help = "Crée un catalogue de badges de démonstration (seuils sur les points à vie)."

    def handle(self, *args: Any, **options: Any) -> None:
        created_count = 0
        for title, description, icon, points_required in DEMO_BADGES:
            _, created = Badge.objects.get_or_create(
                title=title,
                defaults={
                    "description": description,
                    "icon": icon,
                    "points_required": points_required,
                },
            )
            created_count += int(created)

        self.stdout.write(
            self.style.SUCCESS(f"{created_count} badge(s) créé(s) (déjà présents ignorés).")
        )
