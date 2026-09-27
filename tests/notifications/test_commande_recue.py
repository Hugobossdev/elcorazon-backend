"""Ce que l'établissement apprend quand une commande arrive.

Le trou que cette suite ferme
-----------------------------

Le personnel n'était prévenu que sur des **transitions** de statut
(`STAFF_ANNOUNCEMENTS`), et la seule voie automatique vers `confirmed` est
l'encaissement par webhook du prestataire. Or le règlement **en espèces à la
livraison** est aujourd'hui le seul moyen de paiement actif dans l'application
cliente (`checkout_screen.dart` : « mobile money, credit card et debit card
désactivés »). Aucun webhook ne partait donc jamais.

Conséquence : toute commande réellement passée naissait en `pending` et
n'était annoncée à personne — ni notification, ni événement sur le tableau de
bord temps réel, qui ne diffusait que `order.status`. Le repas n'était préparé
que si un membre du personnel rafraîchissait la liste et remarquait la ligne.

Le test décisif est `test_une_commande_en_especes_previent_le_personnel` : il
suit exactement le chemin de l'application cliente, sans aucune transition.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import Role, User, UserType
from apps.carts.services import CartService
from apps.catalog.models import MenuItem
from apps.notifications.models import Notification, NotificationKind
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def operateur(restaurant: Restaurant) -> User:
    """Personnel rattaché à l'établissement, habilité à lire les commandes."""
    membre = User.objects.create_user(
        "operateur@elcorazon.test",
        "motdepasse",
        full_name="Afi Opératrice",
        user_type=UserType.STAFF,
    )
    membre.roles.add(Role.objects.create(name="Opérateur commandes", permissions=["orders.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


def _commander(
    customer: User,
    restaurant: Restaurant,
    item: MenuItem,
    address: Address,
    payment_method: str = "cash",
) -> Order:
    """Passe une commande par le chemin de l'application cliente."""
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=item, quantity=1, options=[])
    return OrderService.create_from_cart(
        user=customer,
        cart=CartService.cart_for(customer, restaurant),
        address=address,
        payment_method=payment_method,
    )


class TestLArriveeEstAnnoncee:
    def test_une_commande_en_especes_previent_le_personnel(
        self,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
        address: Address,
        operateur: User,
    ) -> None:
        """Aucune transition n'a lieu, et le personnel est prévenu quand même.

        C'est tout l'objet du signal `order_created` : sans lui, cette commande
        — la forme la plus courante qu'en produise l'application — n'existait
        pour l'exploitation qu'au prochain rafraîchissement manuel.
        """
        commande = _commander(customer, restaurant, menu_item, address)

        assert commande.status == OrderStatus.PENDING

        alertes = Notification.objects.filter(user=operateur, kind=NotificationKind.ORDER_STATUS)
        assert alertes.count() == 1
        assert alertes.get().title == "Nouvelle commande"
        assert commande.reference in alertes.get().body

    def test_l_alerte_porte_la_commande_pour_que_l_ecran_l_ouvre(
        self,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
        address: Address,
        operateur: User,
    ) -> None:
        """Une notification sur laquelle on ne peut pas cliquer oblige à
        retrouver la commande à la main, ce qui est précisément ce qu'on
        voulait éviter."""
        commande = _commander(customer, restaurant, menu_item, address)

        alerte = Notification.objects.get(user=operateur, kind=NotificationKind.ORDER_STATUS)

        assert alerte.data["order"] == str(commande.pk)
        assert alerte.data["status"] == OrderStatus.PENDING

    def test_le_client_n_est_pas_notifie_de_sa_propre_commande(
        self,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
        address: Address,
        operateur: User,
    ) -> None:
        """Il vient de valider : il a l'écran de confirmation sous les yeux.
        Le notifier pour le geste qu'il achève est le genre d'envoi qui fait
        couper les notifications."""
        _commander(customer, restaurant, menu_item, address)

        assert not Notification.objects.filter(user=customer).exists()

    def test_seul_le_personnel_de_l_etablissement_est_prevenu(
        self,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
        address: Address,
        operateur: User,
        zone: Any,
    ) -> None:
        """Le cloisonnement de l'arrivée vaut celui des transitions : un
        opérateur de Kara n'a pas à être réveillé par les commandes de Lomé."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000001",
        )
        etranger = User.objects.create_user(
            "kara@elcorazon.test",
            "motdepasse",
            full_name="Kodjo Kara",
            user_type=UserType.STAFF,
        )
        etranger.roles.add(Role.objects.create(name="Opérateur Kara", permissions=["orders.read"]))
        StaffMembership.objects.create(user=etranger, restaurant=ailleurs)

        _commander(customer, restaurant, menu_item, address)

        assert Notification.objects.filter(user=operateur).exists()
        assert not Notification.objects.filter(user=etranger).exists()

    def test_l_arrivee_et_la_confirmation_ne_disent_pas_la_meme_chose(
        self,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
        address: Address,
        operateur: User,
    ) -> None:
        """Les deux moments sont distincts — séparés, pour une commande en
        espèces, par le geste du personnel lui-même. Deux notifications au même
        titre les rendraient indiscernables dans la liste."""
        commande = _commander(customer, restaurant, menu_item, address)
        OrderService.transition_to(order=commande, target=OrderStatus.CONFIRMED)

        titres = list(
            Notification.objects.filter(user=operateur)
            .order_by("created_at")
            .values_list("title", flat=True)
        )

        assert titres == ["Nouvelle commande", "Commande confirmée"]
