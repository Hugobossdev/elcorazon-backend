"""Stock relié au cycle de commande.

Le §9 de l'analyse le relevait : `menu_items.available_quantity` existait au
schéma et **n'était décrémenté nulle part**. L'exploitation lisait donc un
chiffre décoratif, ce qui est pire que pas de stock du tout — on croit savoir
ce qu'il reste.

Le test qui porte ce module est
`test_deux_commandes_concurrentes_n_emportent_pas_la_meme_unite` : c'est F1
transposé au catalogue, et c'est la seule vérification qui distingue un retrait
conditionnel en une instruction d'un « lire puis écrire » qui passe deux fois.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connections

from apps.accounts.models import User
from apps.carts.services import CartService, price_cart
from apps.catalog.models import MenuItem
from apps.catalog.services import StockService
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def suivi(menu_item: MenuItem) -> MenuItem:
    """Article dont le stock est suivi, avec deux unités en réserve."""
    MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=2)
    menu_item.refresh_from_db()
    return menu_item


def panier(customer: User, restaurant: Restaurant, item: MenuItem, quantity: int = 1) -> None:
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=item, quantity=quantity, options=[])


class TestRetrait:
    def test_une_commande_decompte_le_stock(
        self, customer: User, restaurant: Restaurant, suivi: MenuItem, address: Address
    ) -> None:
        panier(customer, restaurant, suivi)

        OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
        )

        suivi.refresh_from_db()
        assert suivi.stock_quantity == 1

    def test_un_article_sans_suivi_ne_decompte_rien(
        self, customer: User, restaurant: Restaurant, menu_item: MenuItem, address: Address
    ) -> None:
        """Le cas courant : un plat préparé à la demande n'a pas de stock, et
        imposer un compteur fermerait la moitié du menu au premier oubli de
        réapprovisionnement."""
        panier(customer, restaurant, menu_item, quantity=99)

        OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
        )

        menu_item.refresh_from_db()
        assert menu_item.stock_quantity == 0

    def test_deux_lignes_du_meme_article_se_totalisent(self, suivi: MenuItem) -> None:
        """Un burger saignant et un burger à point font deux lignes et un seul
        stock : les décompter séparément passerait deux fois la vérification sur
        un reliquat d'une unité."""
        with pytest.raises(BusinessRuleViolation):
            StockService.consume({suivi.pk: 3})

        suivi.refresh_from_db()
        assert suivi.stock_quantity == 2

    def test_le_refus_dit_ce_qu_il_reste(self, suivi: MenuItem) -> None:
        with pytest.raises(BusinessRuleViolation) as refus:
            StockService.consume({suivi.pk: 5})

        assert "2" in str(refus.value)


class TestRetour:
    def test_une_annulation_rend_les_unites(
        self, customer: User, restaurant: Restaurant, suivi: MenuItem, address: Address
    ) -> None:
        """Sans ce retour, chaque annulation retirerait définitivement des
        unités jamais servies, et le stock dériverait à la baisse jusqu'à
        fermer un article encore disponible en réserve."""
        panier(customer, restaurant, suivi)
        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
        )

        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="test")

        suivi.refresh_from_db()
        assert suivi.stock_quantity == 2

    def test_une_annulation_rejouee_ne_rend_pas_deux_fois(
        self, customer: User, restaurant: Restaurant, suivi: MenuItem, address: Address
    ) -> None:
        """C3 transposé au stock : la transition vers un statut déjà atteint ne
        fait rien, donc rien n'est recrédité deux fois."""
        panier(customer, restaurant, suivi)
        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
        )

        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="test")
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="test")

        suivi.refresh_from_db()
        assert suivi.stock_quantity == 2

    def test_un_suivi_active_apres_coup_ne_credite_rien(self, menu_item: MenuItem) -> None:
        """La commande n'avait rien décompté ; le créditer inventerait des
        unités qu'aucune livraison n'a jamais rendues."""
        StockService.restore({menu_item.pk: 5})

        menu_item.refresh_from_db()
        assert menu_item.stock_quantity == 0


class TestPanier:
    def test_le_panier_annonce_la_rupture_avant_le_paiement(
        self, customer: User, restaurant: Restaurant, suivi: MenuItem
    ) -> None:
        """Le refus ferme est celui de la commande, qui décompte sous verrou ;
        ce que le panier apporte, c'est de le faire savoir avant le paiement."""
        panier(customer, restaurant, suivi, quantity=5)

        priced = price_cart(CartService.load(CartService.cart_for(customer, restaurant)))

        assert not priced.is_orderable
        assert "2" in priced.lines[0].unavailable_reason

    def test_une_commande_en_rupture_est_refusee(
        self, customer: User, restaurant: Restaurant, suivi: MenuItem, address: Address
    ) -> None:
        panier(customer, restaurant, suivi, quantity=5)

        with pytest.raises(BusinessRuleViolation):
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method="cash",
            )

        suivi.refresh_from_db()
        assert suivi.stock_quantity == 2


@pytest.mark.django_db(transaction=True)
class TestConcurrence:
    """**La course prouvée**, comme pour les points de fidélité (F1).

    `transaction=True` n'est pas un détail : sous le `django_db` ordinaire, les
    données du test ne sont jamais validées et le second fil — qui a sa propre
    connexion — ne les voit pas.
    """

    def test_deux_commandes_concurrentes_n_emportent_pas_la_meme_unite(
        self, menu_item: MenuItem
    ) -> None:
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=1)

        def emporter() -> str:
            try:
                StockService.consume({menu_item.pk: 1})
                return "ok"
            except BusinessRuleViolation:
                return "refusé"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = sorted(f.result() for f in [pool.submit(emporter), pool.submit(emporter)])

        assert resultats == ["ok", "refusé"]
        menu_item.refresh_from_db()
        assert menu_item.stock_quantity == 0
