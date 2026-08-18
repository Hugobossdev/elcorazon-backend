"""Panier serveur.

**Aucun montant n'est stocké ici.** C'est la mise en œuvre structurelle de C1 :
le panier ne retient que *ce que le client a choisi* — un article, des options,
une quantité — et le prix est relu depuis le catalogue à chaque lecture puis à
la validation de la commande.

L'implémentation précédente stockait `price` et `name` sur la ligne de panier
et les acceptait du client. Deux conséquences : un client pouvait se fixer son
propre prix, et un panier oublié une semaine facturait le tarif de la semaine
précédente. Ne pas avoir la colonne est plus solide que de la valider.

Le panier vit dans `carts` et non dans `orders` : c'est un état éphémère,
réécrit en permanence, quand la commande est une écriture comptable définitive.
Les héberger ensemble mélangerait deux cycles de vie opposés.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from apps.catalog.models import MenuItem, Option
from apps.restaurants.models import Restaurant
from common.models import TimeStampedModel, UUIDModel

__all__ = ["Cart", "CartLine", "CartLineOption"]


class Cart(UUIDModel, TimeStampedModel):
    """Un panier par client et par restaurant.

    Le rattachement au restaurant est nécessaire dès maintenant : une commande
    ne peut pas mélanger deux établissements, puisqu'elle est préparée à un
    endroit et enlevée par un livreur en un seul point.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="carts")
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="carts")

    class Meta:
        verbose_name = "panier"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "restaurant"], name="one_cart_per_user_and_restaurant"
            )
        ]
        indexes = [models.Index(fields=["user", "-updated_at"])]

    def __str__(self) -> str:
        return f"Panier de {self.user.email} — {self.restaurant.name}"


class CartLine(UUIDModel, TimeStampedModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="cart_lines")
    quantity = models.PositiveSmallIntegerField(default=1)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "ligne de panier"
        verbose_name_plural = "lignes de panier"
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="cart_line_quantity_positive"
            ),
            # Volontairement **pas** d'unicité sur (cart, menu_item) : deux
            # lignes du même burger avec des cuissons différentes sont deux
            # lignes distinctes. La fusion des lignes réellement identiques
            # relève du service, qui compare aussi les options.
        ]
        indexes = [models.Index(fields=["cart"])]

    def __str__(self) -> str:
        return f"{self.quantity} × {self.menu_item.name}"

    def selected_options(self) -> list[Option]:
        """Options retenues, dans l'ordre d'affichage de leur groupe.

        Sur le modèle et non dans le service de tarification : le panier
        collaboratif porte ses propres lignes, et c'est cette méthode qui permet
        aux deux d'être valorisées par le même code (`price_selection`) au lieu
        d'une seconde boucle de calcul qui divergerait.
        """
        return sorted(
            (selection.option for selection in self.options.all()),
            key=lambda option: (option.group.sort_order, option.group_id, option.sort_order),
        )


class CartLineOption(UUIDModel):
    """Option retenue sur une ligne de panier.

    Une table de liaison plutôt qu'un champ JSON : les options doivent être
    revalidées à la commande — existent-elles encore, sont-elles disponibles,
    respectent-elles les bornes de leur groupe ? Une clé étrangère rend cette
    vérification triviale, là où un JSON obligerait à la refaire à la main.

    À l'inverse, `OrderLine.options` **est** du JSON : à ce stade il s'agit
    d'une copie figée, qui ne doit plus rien à l'état du catalogue.
    """

    line = models.ForeignKey(CartLine, on_delete=models.CASCADE, related_name="options")
    option = models.ForeignKey(Option, on_delete=models.CASCADE, related_name="cart_selections")

    class Meta:
        verbose_name = "option de ligne de panier"
        verbose_name_plural = "options de ligne de panier"
        constraints = [
            models.UniqueConstraint(
                fields=["line", "option"], name="one_selection_per_line_and_option"
            )
        ]

    def __str__(self) -> str:
        return self.option.name
