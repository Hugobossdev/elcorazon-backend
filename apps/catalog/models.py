"""Catalogue : catégories, articles, personnalisation, avis.

Deux invariants de la Phase 1 sont défendus par la structure même :

* **C1** — le prix vit **ici seulement**. Ni le panier ni la commande ne le
  reçoivent du client : le panier référence l'article et relit son prix, la
  commande en fige une copie au moment de sa création. L'implémentation
  précédente acceptait le prix envoyé par le client.
* **S1** — `is_verified_purchase` n'est pas un champ que le client renseigne :
  il est calculé serveur à l'écriture de l'avis. Dans l'existant, la colonne
  était prévue au schéma et n'était jamais remplie.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from common.fields import MoneyField
from common.models import SoftDeleteModel, TimeStampedModel, UUIDModel
from common.storage import product_images

__all__ = [
    "Category",
    "MenuItem",
    "Option",
    "OptionGroup",
    "OptionTemplate",
    "Review",
    "VerifiedPurchase",
]


class Category(UUIDModel, TimeStampedModel):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    emoji = models.CharField(max_length=8, blank=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "catégorie"
        verbose_name_plural = "catégories"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "slug"], name="category_slug_unique_per_restaurant"
            )
        ]

    def __str__(self) -> str:
        return f"{self.emoji} {self.name}".strip()


class MenuItem(UUIDModel, TimeStampedModel, SoftDeleteModel):
    """Article du menu.

    Suppression **logique** : des commandes passées y renvoient, et un retrait
    du catalogue ne doit pas rendre un historique illisible. C'est le critère
    de `SoftDeleteModel` — une écriture comptable pointe dessus.
    """

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="items")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    description = models.TextField(blank=True)
    # Compartiment public : une carte se regarde. Signer ces URL les ferait
    # expirer en quinze minutes, donc aucun cache ni CDN ne pourrait s'y
    # accrocher — et un menu mis en favori afficherait des cadres vides le
    # lendemain (ADR-011).
    image = models.ImageField(upload_to="menu/", storage=product_images, null=True, blank=True)

    # Seule source de vérité du prix (C1).
    price = MoneyField()

    preparation_minutes = models.PositiveSmallIntegerField(default=15)
    calories = models.PositiveIntegerField(null=True, blank=True)
    ingredients = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    allergens = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    dietary_tags = ArrayField(
        models.CharField(max_length=32),
        default=list,
        blank=True,
        help_text="Ex. vegetarian, vegan, halal.",
    )

    is_available = models.BooleanField(
        default=True, help_text="Disponibilité du jour, pilotée par la cuisine."
    )
    is_popular = models.BooleanField(default=False)
    vip_exclusive = models.BooleanField(default=False)

    # Stock — relié au cycle de commande, contrairement à
    # `menu_items.available_quantity` de l'implémentation précédente, qui
    # n'était décrémenté nulle part : un chiffre décoratif, que l'exploitation
    # croyait pourtant lire.
    #
    # Le suivi est **facultatif par article** parce que les deux cas coexistent
    # dans la même carte : un burger se prépare à la demande et n'a pas de
    # stock, une bouteille importée en a un. Un compteur imposé aux deux
    # obligerait à réapprovisionner chaque soir des plats qui n'en ont pas
    # besoin, et le premier oubli fermerait la moitié du menu.
    tracks_stock = models.BooleanField(
        default=False,
        help_text="Décompter les commandes du stock. Faux pour un plat préparé à la demande.",
    )
    # `PositiveIntegerField` pose la contrainte `>= 0` **en base**. C'est F3
    # transposé au catalogue : le stock ne peut pas devenir négatif par une
    # garde applicative qu'un nouveau chemin d'écriture oublierait, mais parce
    # que PostgreSQL refuse la ligne.
    stock_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Unités restantes. Ignoré tant que le suivi de stock est désactivé.",
    )

    # Dénormalisation assumée : la note moyenne est lue à chaque affichage de
    # liste et recalculée à chaque avis — soit un ratio de lecture sur écriture
    # de plusieurs milliers pour un. La recalculer en agrégat à la volée
    # imposerait une jointure sur `Review` à chaque page de menu.
    rating_average = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    rating_count = models.PositiveIntegerField(default=0)

    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "article"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "slug"], name="item_slug_unique_per_restaurant"
            ),
        ]
        indexes = [
            # La requête de la page d'accueil : les articles disponibles d'un
            # restaurant, dans l'ordre d'affichage.
            models.Index(fields=["restaurant", "is_available", "sort_order"]),
            models.Index(fields=["category", "is_available"]),
        ]

    def __str__(self) -> str:
        return self.name


class OptionGroup(UUIDModel):
    """Groupe d'options : « Cuisson », « Taille », « Suppléments ».

    `min_select` et `max_select` portent la règle de validation. Les exprimer
    en données plutôt qu'en code permet à l'exploitation de créer un groupe
    « choisir 2 accompagnements parmi 5 » sans développement.
    """

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="option_groups")
    name = models.CharField(max_length=80)
    min_select = models.PositiveSmallIntegerField(default=0)
    max_select = models.PositiveSmallIntegerField(default=1)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "groupe d'options"
        verbose_name_plural = "groupes d'options"
        ordering = ["sort_order", "name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(min_select__lte=models.F("max_select")),
                name="option_group_bounds_coherent",
            ),
            models.CheckConstraint(
                condition=models.Q(max_select__gte=1),
                name="option_group_allows_one_choice",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.min_select}–{self.max_select})"

    @property
    def is_required(self) -> bool:
        return self.min_select > 0


class Option(UUIDModel):
    group = models.ForeignKey(OptionGroup, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=80)

    # Écart de prix, potentiellement négatif — « sans fromage, −200 F ».
    price_delta = MoneyField()

    is_default = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "option"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["group", "name"], name="option_name_unique_per_group")
        ]

    def __str__(self) -> str:
        return self.name


class VerifiedPurchase(UUIDModel):
    """Trace « cet utilisateur a bien reçu cet article ».

    C'est la source de `Review.is_verified_purchase` (S1). Elle existe parce
    que le graphe de dépendances de l'ADR-002 est acyclique : `orders` connaît
    `catalog`, jamais l'inverse. Le catalogue ne peut donc pas interroger les
    commandes pour savoir qui a acheté quoi — c'est `orders` qui l'en informe,
    à la livraison, via `record_purchase()`.

    La table est plus qu'un contournement de dépendance : elle survit à la
    purge des vieilles commandes et à toute évolution du schéma de `orders`,
    dont la logique d'avis devient indépendante.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verified_purchases")
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.CASCADE, related_name="verified_purchases"
    )
    last_purchased_at = models.DateTimeField()

    class Meta:
        verbose_name = "achat vérifié"
        verbose_name_plural = "achats vérifiés"
        constraints = [
            # Un client qui commande dix fois le même burger n'ajoute pas dix
            # lignes : seule la dernière date compte.
            models.UniqueConstraint(
                fields=["user", "menu_item"], name="one_verified_purchase_per_item_and_user"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.menu_item.name}"


class Review(UUIDModel, TimeStampedModel):
    """Avis client sur un article.

    Un avis **sans achat reste autorisé** — simplement non marqué « vérifié ».
    C'est un choix produit hérité, conservé pour ne pas casser un usage
    existant ; le durcir est une décision à prendre, pas un correctif.
    """

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    title = models.CharField(max_length=120, blank=True)
    comment = models.TextField(blank=True)

    # S1 — calculé serveur, jamais accepté du client. `editable=False` retire
    # le champ des formulaires et des sérialiseurs générés, ce qui rend
    # l'oubli impossible plutôt qu'improbable.
    is_verified_purchase = models.BooleanField(default=False, editable=False)

    helpful_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "avis"
        verbose_name_plural = "avis"
        ordering = ["-created_at"]
        constraints = [
            # S5 — un avis par article et par utilisateur.
            models.UniqueConstraint(
                fields=["menu_item", "user"], name="one_review_per_item_and_user"
            ),
        ]
        indexes = [models.Index(fields=["menu_item", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.rating}/5 — {self.menu_item.name}"


class OptionTemplate(UUIDModel, TimeStampedModel):
    """Option réutilisable de l'établissement — « Sans oignon », « Extra fromage ».

    Une **bibliothèque**, pas une option en service : appliquer un modèle à un
    article y **copie** une `Option`, il ne l'y référence pas. Le prix facturé
    reste donc celui porté par l'option de l'article (C1), et modifier un modèle
    ne repricera jamais en silence les articles qui l'ont utilisé — ce qui
    arriverait avec une simple clé étrangère, y compris sur des commandes en
    cours de composition.

    `group_name` est le groupe suggéré (« Cuisson »). Il évite de redemander à
    chaque application dans quel groupe ranger l'option, sans l'imposer :
    l'appel peut viser un autre groupe.
    """

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name="option_templates"
    )
    name = models.CharField(max_length=80)
    group_name = models.CharField(
        max_length=80,
        blank=True,
        help_text="Groupe suggéré à l'application — « Cuisson », « Suppléments ».",
    )
    price_delta = MoneyField()
    is_default = models.BooleanField(
        default=False,
        help_text="Présélectionnée dans son groupe une fois appliquée — « à point ».",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "modèle d'option"
        verbose_name_plural = "modèles d'options"
        ordering = ["sort_order", "name"]
        constraints = [
            # Deux modèles homonymes dans le même groupe d'un même établissement
            # rendraient la bibliothèque inutilisable : l'opérateur ne saurait
            # pas lequel il applique.
            models.UniqueConstraint(
                fields=["restaurant", "group_name", "name"],
                name="option_template_unique_per_group",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.price_delta})"
