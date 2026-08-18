"""Peuple un catalogue de récompenses de démo pour un restaurant existant.

Sert à valider la migration Flutter de la fidélité (Phase 6, `fastfood`) :
sans données, l'échange ne peut pas être vérifié de bout en bout. Idempotente
(`get_or_create` sur `(restaurant, name)`) — rejouable sans dupliquer.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.loyalty.models import Reward, RewardKind
from apps.restaurants.models import Restaurant

# (nom, description, genre, coût en points, remise en F CFA)
DEMO_REWARDS: list[tuple[str, str, str, int, int]] = [
    (
        "500 F de réduction",
        "500 F CFA de réduction sur votre prochaine commande.",
        RewardKind.DISCOUNT,
        100,
        500,
    ),
    (
        "1000 F de réduction",
        "1000 F CFA de réduction sur votre prochaine commande.",
        RewardKind.DISCOUNT,
        180,
        1000,
    ),
    (
        "Livraison offerte",
        "Livraison gratuite sur votre prochaine commande.",
        RewardKind.FREE_DELIVERY,
        50,
        0,
    ),
]


class Command(BaseCommand):
    help = "Crée un catalogue de récompenses de démonstration pour un restaurant."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--restaurant",
            default="el-corazon-lome",
            help="Slug du restaurant à peupler (défaut : el-corazon-lome).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = options["restaurant"]
        try:
            restaurant = Restaurant.objects.get(slug=slug)
        except Restaurant.DoesNotExist as exc:
            raise CommandError(
                f"Aucun restaurant avec le slug {slug!r}. "
                "Créer le restaurant avant de peupler ses récompenses."
            ) from exc

        created_count = 0
        for name, description, kind, points_cost, discount_minor in DEMO_REWARDS:
            _, created = Reward.objects.get_or_create(
                restaurant=restaurant,
                name=name,
                defaults={
                    "description": description,
                    "kind": kind,
                    "points_cost": points_cost,
                    "discount_minor": discount_minor,
                    "discount_currency": "XOF",
                },
            )
            created_count += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Restaurant {slug!r} : {created_count} récompense(s) créée(s) "
                "(déjà présentes ignorées)."
            )
        )
