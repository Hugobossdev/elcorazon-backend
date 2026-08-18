"""Recherche transverse du back-office.

Un seul champ, en haut de l'écran : on y tape une référence de commande, un
numéro de téléphone, un nom de plat, et l'on veut la bonne fiche. Le
back-office avait la même intention, mais l'implémentait en quatre requêtes
lancées depuis le navigateur sur quatre tables, **sans aucune vérification de
droit ni de périmètre** : un opérateur de Kara y trouvait les commandes de
Lomé, et un compte privé de `customers.read` y lisait des numéros de téléphone
de clients.

Ce module ne fait donc pas que regrouper des requêtes. Il rétablit, pour chaque
famille de résultats, les deux étages que l'ADR-005 impose :

* **la permission** — `orders.read` pour les commandes, `customers.read` pour
  les clients, `couriers.read` pour la flotte, `catalog.read` pour la carte.
  Une famille dont on n'a pas le droit **n'est pas cherchée** : elle ne rend pas
  une liste vide qu'on pourrait confondre avec « rien trouvé », elle est absente
  de la réponse ;
* **le périmètre** — le même filtre que le `get_queryset` du domaine. Une
  commande hors périmètre est introuvable, pas interdite.

Le module lit et n'écrit nulle part. C'est ce qui rend acceptable qu'il
connaisse quatre domaines : les flèches vont toutes vers lui, aucune n'en part.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from apps.accounts.models import User, UserType
from apps.catalog.models import MenuItem
from apps.delivery.models import CourierProfile
from apps.orders.models import Order
from apps.restaurants.scoping import is_unscoped, staff_restaurant_ids

__all__ = ["FAMILIES", "SearchHit", "SearchService"]

#: Familles interrogeables, et la permission qui ouvre chacune.
#:
#: Fermé et déclaré ici plutôt que déduit d'un paramètre : une famille que le
#: client pourrait nommer librement serait une table qu'il choisit d'interroger.
FAMILIES: dict[str, str] = {
    "order": "orders.read",
    "customer": "customers.read",
    "courier": "couriers.read",
    "menu_item": "catalog.read",
}


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Un résultat, sous une forme identique quelle que soit sa famille.

    `title` et `subtitle` sont composés ici et non côté client : c'est le
    serveur qui sait ce qui identifie une commande (sa référence) ou un livreur
    (son nom et son véhicule), et quatre mises en forme recopiées dans l'écran
    divergeraient à la première évolution.
    """

    kind: str
    id: str
    title: str
    subtitle: str


class SearchService:
    #: Au-delà, ce n'est plus une recherche : c'est une liste, et chaque domaine
    #: a la sienne, filtrable et paginée.
    MAX_PER_FAMILY = 10

    @staticmethod
    def search(*, user: User, query: str, limit: int = 5) -> list[SearchHit]:
        """Cherche `query` dans les familles que `user` a le droit de lire."""
        query = query.strip()
        # Deux caractères ramèneraient une part notable de chaque table, sans
        # rien désigner. Le seuil est un refus, pas une optimisation.
        if len(query) < 3:
            return []

        limite = min(max(limit, 1), SearchService.MAX_PER_FAMILY)
        resultats: list[SearchHit] = []

        if user.has_permission(FAMILIES["order"]):
            resultats += SearchService._orders(user, query, limite)
        if user.has_permission(FAMILIES["customer"]):
            resultats += SearchService._customers(query, limite)
        if user.has_permission(FAMILIES["courier"]):
            resultats += SearchService._couriers(user, query, limite)
        if user.has_permission(FAMILIES["menu_item"]):
            resultats += SearchService._menu_items(user, query, limite)

        return resultats

    # ------------------------------------------------------------ familles

    @staticmethod
    def _orders(user: User, query: str, limite: int) -> list[SearchHit]:
        queryset = Order.objects.select_related("restaurant", "customer")
        if not is_unscoped(user):
            queryset = queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

        lignes = queryset.filter(
            Q(reference__icontains=query)
            | Q(recipient_name__icontains=query)
            | Q(recipient_phone__icontains=query)
        ).order_by("-placed_at")[:limite]

        return [
            SearchHit(
                kind="order",
                id=str(commande.pk),
                title=commande.reference,
                subtitle=f"{commande.recipient_name} — {commande.get_status_display()}",
            )
            for commande in lignes
        ]

    @staticmethod
    def _customers(query: str, limite: int) -> list[SearchHit]:
        # Pas de filtre d'établissement : un client n'appartient à aucun
        # restaurant, il commande où il veut. C'est la permission seule qui
        # garde cette famille.
        lignes = User.objects.filter(user_type=UserType.CUSTOMER).filter(
            Q(full_name__icontains=query) | Q(email__icontains=query) | Q(phone__icontains=query)
        )[:limite]

        return [
            SearchHit(
                kind="customer",
                id=str(client.pk),
                title=client.full_name,
                subtitle=client.email,
            )
            for client in lignes
        ]

    @staticmethod
    def _couriers(user: User, query: str, limite: int) -> list[SearchHit]:
        queryset = CourierProfile.objects.select_related("user", "restaurant")
        if not is_unscoped(user):
            queryset = queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

        lignes = queryset.filter(
            Q(user__full_name__icontains=query)
            | Q(user__email__icontains=query)
            | Q(vehicle_plate__icontains=query)
        )[:limite]

        return [
            SearchHit(
                kind="courier",
                id=str(livreur.pk),
                title=livreur.user.full_name,
                subtitle=f"{livreur.get_vehicle_type_display()} — "
                f"{livreur.get_verification_status_display()}",
            )
            for livreur in lignes
        ]

    @staticmethod
    def _menu_items(user: User, query: str, limite: int) -> list[SearchHit]:
        queryset = MenuItem.objects.alive().select_related("category", "restaurant")
        if not is_unscoped(user):
            queryset = queryset.filter(restaurant_id__in=staff_restaurant_ids(user))

        lignes = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))[
            :limite
        ]

        return [
            SearchHit(
                kind="menu_item",
                id=str(article.pk),
                title=article.name,
                subtitle=f"{article.category.name} — {article.price}",
            )
            for article in lignes
        ]
