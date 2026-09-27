"""Peuple un catalogue de démo pour un restaurant existant.

Sert à valider la migration Flutter du catalogue (Phase 6, `fastfood`) : sans
données, l'écran menu de l'app resterait vide même une fois branché sur
Django. Idempotente (`get_or_create` sur `(restaurant, slug)`) — un second
appel ne duplique rien, donc rejouable sans risque après un premier essai.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.catalog.models import Category, MenuItem
from apps.restaurants.models import Restaurant
from common.money import Money

# (slug, nom, emoji, [(slug, nom, prix XOF, is_popular), ...])
DEMO_CATEGORIES: list[tuple[str, str, str, list[tuple[str, str, int, bool]]]] = [
    (
        "burgers",
        "Burgers",
        "🍔",
        [
            ("cheeseburger", "Cheeseburger", 2500, True),
            ("double-cheese", "Double Cheese", 3500, True),
            ("burger-poulet", "Burger Poulet Croustillant", 3000, False),
        ],
    ),
    (
        "accompagnements",
        "Accompagnements",
        "🍟",
        [
            ("frites", "Frites Maison", 1000, True),
            ("onion-rings", "Onion Rings", 1500, False),
            ("nuggets", "Nuggets de Poulet (6 pièces)", 2000, False),
        ],
    ),
    (
        "boissons",
        "Boissons",
        "🥤",
        [
            ("coca-cola", "Coca-Cola 33cl", 500, False),
            ("jus-bissap", "Jus de Bissap", 750, True),
            ("eau-minerale", "Eau Minérale", 300, False),
        ],
    ),
    (
        "desserts",
        "Desserts",
        "🍰",
        [
            ("glace-vanille", "Glace Vanille", 1200, False),
            ("brownie", "Brownie Chocolat", 1500, True),
            ("tarte-citron", "Tarte au Citron", 1500, False),
        ],
    ),
]


class Command(BaseCommand):
    help = "Crée un jeu de catégories/articles de démonstration pour un restaurant."

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
                "Créer le restaurant avant de peupler son catalogue."
            ) from exc

        categories_created = 0
        items_created = 0

        for sort_order, (cat_slug, cat_name, emoji, items) in enumerate(DEMO_CATEGORIES):
            category, created = Category.objects.get_or_create(
                restaurant=restaurant,
                slug=cat_slug,
                defaults={"name": cat_name, "emoji": emoji, "sort_order": sort_order},
            )
            categories_created += int(created)

            for item_sort_order, (item_slug, item_name, price_xof, is_popular) in enumerate(items):
                _, item_was_created = MenuItem.objects.get_or_create(
                    restaurant=restaurant,
                    slug=item_slug,
                    defaults={
                        "category": category,
                        "name": item_name,
                        "description": f"{item_name} — El Corazón",
                        "price": Money(price_xof, "XOF"),
                        "is_popular": is_popular,
                        "sort_order": item_sort_order,
                    },
                )
                items_created += int(item_was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Restaurant {slug!r} : {categories_created} catégorie(s) et "
                f"{items_created} article(s) créé(s) (déjà présents ignorés)."
            )
        )
