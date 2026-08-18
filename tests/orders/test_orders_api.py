"""API des commandes — invariants C1 à C5, ADR-009 et ADR-010.

Trois tests portent cette suite :

* `test_un_rejeu_ne_cree_pas_une_seconde_commande` — l'idempotence. Sans elle,
  un mobile qui perd le réseau après l'envoi commande deux repas, et le
  problème se découvre à la livraison.
* `test_le_total_est_recalcule_serveur` — C2, corollaire direct de C1.
* `TestTransitions` — C3 et C4 : le retour arrière est inexprimable, pas
  seulement interdit.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.carts.services import CartService
from apps.catalog.models import MenuItem, VerifiedPurchase
from apps.geography.models import DeliveryZone
from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.orders.views import CREATE_ENDPOINT
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    """Client HTTP **distinct** de la fixture `client`.

    Les réutiliser ferait qu'un `force_authenticate` plus loin dans le test
    changerait aussi l'identité de celui-ci — deux acteurs qui n'en font qu'un,
    et un test qui ne vérifie plus ce qu'il annonce.
    """
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def staff(restaurant: Restaurant) -> User:
    """Membre du personnel muni des permissions d'exploitation."""
    member = User.objects.create_user(
        "gerant@elcorazon.test", "motdepasse", full_name="Kofi Gérant", user_type=UserType.STAFF
    )
    role = Role.objects.create(
        name="Manager test", permissions=["orders.update_status", "orders.cancel", "orders.refund"]
    )
    member.roles.add(role)
    # La permission dit ce qu'on sait faire, le rattachement dit sur quoi : un
    # membre du personnel sans établissement ne voit aucune commande.
    StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def garni(customer: User, restaurant: Restaurant, menu_item: MenuItem) -> None:
    """Panier d'un burger à 3 500 F."""
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])


def commander(
    client: APIClient,
    restaurant: Restaurant,
    address: Address,
    key: str | None = None,
    **extra: object,
) -> object:
    return client.post(
        reverse("v1:orders:order-list"),
        {
            "restaurant": restaurant.slug,
            "address": str(address.pk),
            "payment_method": PaymentMethod.MOBILE_MONEY,
            **extra,
        },
        format="json",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


class TestCreation:
    def test_le_panier_devient_une_commande(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == OrderStatus.PENDING
        assert response.data["reference"].startswith("EC")
        assert len(response.data["lines"]) == 1

    def test_le_total_est_recalcule_serveur(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """C2 — sous-total 3 500, frais 500 + 100 F/km sur ~1,1 km, remise 0.
        Aucune de ces valeurs n'a traversé le réseau."""
        data = commander(as_customer, restaurant, address).data

        assert data["subtotal"] == {"amount": "3500", "currency": XOF}
        assert data["delivery_fee"]["currency"] == XOF
        frais = int(data["delivery_fee"]["amount"])
        assert 600 <= frais <= 620
        assert int(data["total"]["amount"]) == 3_500 + frais

    def test_le_panier_est_vide_apres_coup(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        commander(as_customer, restaurant, address)

        panier = as_customer.get(reverse("v1:carts:cart-detail", args=[restaurant.slug]))
        assert panier.data["lines"] == []

    def test_la_ligne_est_une_copie_figee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        garni: None,
    ) -> None:
        """Un article renommé ou repricé après coup ne réécrit pas
        l'histoire : la commande porte son propre exemplaire."""
        data = commander(as_customer, restaurant, address).data

        menu_item.name = "Burger Corazón XXL"
        menu_item.price = Money(9_999, XOF)
        menu_item.save()

        fiche = as_customer.get(reverse("v1:orders:order-detail", args=[data["id"]])).data
        assert fiche["lines"][0]["item_name"] == "Burger Corazón"
        assert fiche["lines"][0]["unit_price"] == {"amount": "3500", "currency": XOF}

    def test_l_adresse_est_recopiee_et_survit_a_son_effacement(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Le RGPD impose d'honorer l'effacement ; la commande doit rester
        lisible. D'où la copie plutôt qu'une clé étrangère."""
        data = commander(as_customer, restaurant, address).data
        address.delete()

        fiche = as_customer.get(reverse("v1:orders:order-detail", args=[data["id"]])).data
        assert fiche["delivery_address_line"] == "Rue du Commerce"
        assert fiche["delivery_location"] == {"lat": 6.1319, "lon": 1.2355}

    def test_un_panier_vide_ne_commande_rien(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address
    ) -> None:
        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Order.objects.count() == 0

    def test_un_article_devenu_indisponible_bloque_la_commande(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        garni: None,
    ) -> None:
        """Le panier n'est pas commandé partiellement : décider à la place du
        client produirait une commande qu'il n'a pas relue."""
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "Burger Corazón" in response.data["detail"]

    def test_l_adresse_d_autrui_est_invalide(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        courier_user: User,
        city,
        address: Address,
        garni: None,
    ) -> None:
        """Sans ce cloisonnement, un identifiant deviné ferait livrer la
        commande chez quelqu'un d'autre."""
        autre = Address.objects.create(
            user=courier_user, label="Ailleurs", line1="X", city=city, location=address.location
        )

        response = commander(as_customer, restaurant, autre)

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestNumeroJoignable:
    def test_une_commande_sans_numero_est_refusee(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        customer: User,
        garni: None,
    ) -> None:
        """À Lomé, le livreur appelle pour trouver la porte : une course sans
        numéro est une course perdue. Mieux vaut le dire au passage de commande
        qu'au pied de l'immeuble."""
        Address.objects.filter(pk=address.pk).update(recipient_phone="")
        User.objects.filter(pk=customer.pk).update(phone=None)

        response = commander(as_customer, restaurant, address)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "joignable" in response.data["detail"]


class TestDevis:
    def test_un_panier_vide_ne_se_voit_pas_reprocher_le_minimum(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        zone: DeliveryZone,
    ) -> None:
        """Le devis d'un panier vide **répond**, même une adresse choisie.

        Il refusait, et pour un motif faux : le barème de zone comparait un
        sous-total de zéro au minimum de commande et renvoyait « commande
        minimum de 1 000 XOF dans cette zone » — un reproche adressé au client
        alors que son panier serveur était vide. Le diagnostic partait dans la
        mauvaise direction : c'est la synchronisation du panier qu'il fallait
        regarder, pas le montant.
        """
        zone.min_order_amount = Money(1_000, XOF)
        zone.save(update_fields=["min_order_amount_minor", "min_order_amount_currency"])

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"restaurant": restaurant.slug, "address": str(address.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["subtotal"] == {"amount": "0", "currency": XOF}
        assert response.data["is_orderable"] is False

    def test_un_panier_garni_reste_soumis_au_minimum(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        zone: DeliveryZone,
        garni: None,
    ) -> None:
        """La règle de zone n'est pas levée pour autant : elle s'applique dès
        qu'il y a une course à faire."""
        zone.min_order_amount = Money(50_000, XOF)
        zone.save(update_fields=["min_order_amount_minor", "min_order_amount_currency"])

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"restaurant": restaurant.slug, "address": str(address.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["min_order_amount"] == "50000"


class TestIdempotence:
    def test_l_en_tete_est_obligatoire(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Rendue facultative, elle serait omise le jour où le réseau coupe —
        le seul jour où elle sert."""
        response = as_customer.post(
            reverse("v1:orders:order-list"),
            {
                "restaurant": restaurant.slug,
                "address": str(address.pk),
                "payment_method": PaymentMethod.CASH,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_rejeu_ne_cree_pas_une_seconde_commande(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        cle = str(uuid.uuid4())
        premiere = commander(as_customer, restaurant, address, key=cle)
        rejeu = commander(as_customer, restaurant, address, key=cle)

        assert rejeu.status_code == status.HTTP_201_CREATED
        assert rejeu.data["id"] == premiere.data["id"]
        assert Order.objects.count() == 1

    def test_le_rejeu_rend_exactement_la_reponse_d_origine(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Une réponse seulement « équivalente » ferait retenter le client,
        faute de reconnaître ce qu'il reçoit."""
        cle = str(uuid.uuid4())
        premiere = commander(as_customer, restaurant, address, key=cle)

        assert commander(as_customer, restaurant, address, key=cle).data == premiere.data

    def test_deux_clients_peuvent_tirer_la_meme_cle(
        self,
        client: APIClient,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        courier_user: User,
        city,
        garni: None,
    ) -> None:
        """La clé est portée à l'utilisateur : personne ne lit la réponse
        d'autrui en devinant la sienne."""
        cle = "cle-partagee"
        commander(as_customer, restaurant, address, key=cle)

        autre_client = User.objects.create_user("bea@elcorazon.test", "motdepasse", full_name="Béa")
        son_adresse = Address.objects.create(
            user=autre_client,
            label="Chez Béa",
            line1="Y",
            city=city,
            location=address.location,
            recipient_phone="+22890222222",
        )
        cart = CartService.cart_for(autre_client, restaurant)
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])

        client.force_authenticate(autre_client)
        response = commander(client, restaurant, son_adresse, key=cle)

        assert response.status_code == status.HTTP_201_CREATED
        assert Order.objects.count() == 2


class TestIdempotenceConcurrente:
    """Le cas que la première rédaction laissait passer.

    Le rejeu séquentiel — le client retente après coupure — était couvert. Le
    rejeu **concurrent** ne l'était pas : deux requêtes en vol créaient chacune
    une commande, et la perdante abandonnait la sienne en base.
    """

    def test_une_cle_deja_prise_ne_cree_pas_de_seconde_commande(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        garni: None,
        customer: User,
    ) -> None:
        from apps.orders.idempotency import reserve

        # Simule la requête concurrente : elle détient la clé et n'a pas fini.
        assert reserve(user=customer, endpoint=CREATE_ENDPOINT, key="cle-en-vol") is None

        response = commander(as_customer, restaurant, address, key="cle-en-vol")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "request_in_progress"
        assert Order.objects.count() == 0

    def test_la_cle_est_prise_avant_toute_ecriture(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        garni: None,
        customer: User,
    ) -> None:
        """L'ordre est ce qui corrige le défaut : réserver d'abord, écrire
        ensuite. Après une commande réussie, la clé porte sa réponse."""
        from apps.orders.models import IdempotencyKey

        commander(as_customer, restaurant, address, key="cle-terminee")

        cle = IdempotencyKey.objects.get(key="cle-terminee")
        assert cle.completed_at is not None
        assert cle.response_status == status.HTTP_201_CREATED
        assert cle.order_id is not None

    def test_un_echec_libere_la_cle(
        self, as_customer: APIClient, restaurant: Restaurant, address: Address, customer: User
    ) -> None:
        """Panier vide : la clé n'a rien produit. La garder bloquerait le
        client qui corrige son panier et réessaie avec la même clé."""
        from apps.orders.models import IdempotencyKey

        refus = commander(as_customer, restaurant, address, key="cle-liberee")

        assert refus.status_code == status.HTTP_409_CONFLICT
        assert not IdempotencyKey.objects.filter(key="cle-liberee").exists()

    def test_apres_liberation_la_meme_cle_est_reutilisable(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        customer: User,
        menu_item: MenuItem,
    ) -> None:
        commander(as_customer, restaurant, address, key="cle-reprise")

        cart = CartService.cart_for(customer, restaurant)
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])
        seconde = commander(as_customer, restaurant, address, key="cle-reprise")

        assert seconde.status_code == status.HTTP_201_CREATED
        assert Order.objects.count() == 1


class TestCloisonnement:
    def test_le_client_ne_voit_que_ses_commandes(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        garni: None,
        order: Order,
    ) -> None:
        commander(as_customer, restaurant, address)

        response = as_customer.get(reverse("v1:orders:order-list"))

        assert {o["customer"] if "customer" in o else None for o in response.data["results"]} == {
            None
        }
        assert response.data["count"] == 2  # la sienne, plus celle de la fixture

    def test_la_commande_d_autrui_est_introuvable(
        self, client: APIClient, courier_user: User, order: Order
    ) -> None:
        client.force_authenticate(courier_user)

        response = client.get(reverse("v1:orders:order-detail", args=[order.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAnnulation:
    def test_le_client_annule_une_commande_en_attente(
        self, as_customer: APIClient, order: Order
    ) -> None:
        response = as_customer.post(
            reverse("v1:orders:order-cancel", args=[order.pk]),
            {"reason": "Erreur d'adresse"},
            format="json",
        )

        assert response.data["status"] == OrderStatus.CANCELLED
        assert response.data["cancellation_reason"] == "Erreur d'adresse"
        assert response.data["cancelled_at"] is not None

    def test_le_client_n_annule_plus_une_fois_la_cuisine_lancee(
        self, as_customer: APIClient, order: Order, staff: User
    ) -> None:
        """Décision commerciale et non technique : la machine autorise encore
        l'annulation, la politique client non."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PREPARING)

        response = as_customer.post(
            reverse("v1:orders:order-cancel", args=[order.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["current_status"] == OrderStatus.PREPARING

    def test_le_journal_garde_la_trace(self, as_customer: APIClient, order: Order) -> None:
        as_customer.post(
            reverse("v1:orders:order-cancel", args=[order.pk]),
            {"reason": "Trop long"},
            format="json",
        )

        fiche = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk])).data
        assert fiche["status_events"] == [
            {
                "id": fiche["status_events"][0]["id"],
                "from_status": OrderStatus.PENDING,
                "to_status": OrderStatus.CANCELLED,
                "reason": "Trop long",
                "created_at": fiche["status_events"][0]["created_at"],
            }
        ]


class TestTransitions:
    def _avance(self, client: APIClient, order: Order, cible: str) -> object:
        return client.post(
            reverse("v1:orders:managed-order-status", args=[order.pk]),
            {"status": cible},
            format="json",
        )

    def test_le_personnel_fait_avancer_la_commande(
        self, client: APIClient, staff: User, order: Order
    ) -> None:
        client.force_authenticate(staff)

        response = self._avance(client, order, OrderStatus.CONFIRMED)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == OrderStatus.CONFIRMED

    def test_un_retour_arriere_est_refuse(
        self, client: APIClient, staff: User, order: Order
    ) -> None:
        """C3 — rejouer une transition descendante réincrémentait les
        compteurs du livreur. Le graphe acyclique la rend inexprimable."""
        client.force_authenticate(staff)
        self._avance(client, order, OrderStatus.CONFIRMED)

        response = self._avance(client, order, OrderStatus.PENDING)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "illegal_transition"
        assert response.data["allowed_transitions"] == ["cancelled", "preparing"]

    def test_un_saut_d_etape_est_refuse(self, client: APIClient, staff: User, order: Order) -> None:
        client.force_authenticate(staff)

        response = self._avance(client, order, OrderStatus.DELIVERED)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_statut_hors_enumeration_est_refuse(
        self, client: APIClient, staff: User, order: Order
    ) -> None:
        """C4 — l'implémentation précédente écrivait `accepted`, absent de
        l'énumération SQL."""
        client.force_authenticate(staff)

        response = client.post(
            reverse("v1:orders:managed-order-status", args=[order.pk]),
            {"status": "accepted"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_rejouer_le_statut_courant_ne_fait_rien(
        self, client: APIClient, staff: User, order: Order
    ) -> None:
        """Un client qui tapote deux fois ne doit pas recevoir une erreur pour
        une action déjà accomplie."""
        client.force_authenticate(staff)
        self._avance(client, order, OrderStatus.CONFIRMED)

        response = self._avance(client, order, OrderStatus.CONFIRMED)

        assert response.status_code == status.HTTP_200_OK
        assert order.status_events.count() == 1

    def test_un_client_ne_fait_pas_avancer_sa_commande(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """Sans cette garde, un client se déclarerait livré."""
        response = self._avance(as_customer, order, OrderStatus.CONFIRMED)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_les_transitions_possibles_sont_annoncees(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """La table est déclarée une fois (ADR-010) ; la recopier dans trois
        applications Flutter garantirait qu'elles divergent."""
        response = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))

        assert response.data["allowed_transitions"] == ["cancelled", "confirmed"]


class TestLivraison:
    def test_la_livraison_marque_les_achats_verifies(
        self,
        client: APIClient,
        as_customer: APIClient,
        staff: User,
        restaurant: Restaurant,
        address: Address,
        customer: User,
        menu_item: MenuItem,
        garni: None,
    ) -> None:
        """S1 — `orders` informe `catalog`, le sens autorisé par le graphe de
        dépendances. C'est ce qui permettra à l'avis d'être marqué.
        """
        commande = commander(as_customer, restaurant, address).data
        order = Order.objects.get(pk=commande["id"])

        client.force_authenticate(staff)
        for cible in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
            OrderStatus.DELIVERED,
        ):
            client.post(
                reverse("v1:orders:managed-order-status", args=[order.pk]),
                {"status": cible},
                format="json",
            )

        order.refresh_from_db()
        assert order.status == OrderStatus.DELIVERED
        assert order.delivered_at is not None
        assert VerifiedPurchase.objects.filter(user=customer, menu_item=menu_item).exists()

    def test_l_avis_devient_verifie_apres_livraison(
        self,
        client: APIClient,
        as_customer: APIClient,
        staff: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        garni: None,
    ) -> None:
        """Le bout-en-bout de S1, de la commande à la mention « achat
        vérifié » — ce que la tranche 4b ne pouvait pas encore prouver."""
        commande = commander(as_customer, restaurant, address).data
        client.force_authenticate(staff)
        for cible in (
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.PICKED_UP,
            OrderStatus.ON_THE_WAY,
            OrderStatus.DELIVERED,
        ):
            client.post(
                reverse("v1:orders:managed-order-status", args=[commande["id"]]),
                {"status": cible},
                format="json",
            )

        avis = as_customer.post(
            reverse("v1:catalog:review-list"),
            {"menu_item": str(menu_item.pk), "rating": 5},
            format="json",
        )

        assert avis.data["is_verified_purchase"] is True


class TestReferences:
    def test_deux_commandes_ont_deux_references(
        self,
        as_customer: APIClient,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        garni: None,
    ) -> None:
        """La séquence PostgreSQL les distingue sans verrou : un compteur
        applicatif ferait échouer une commande sur deux aux heures de pointe."""
        premiere = commander(as_customer, restaurant, address).data

        cart = CartService.cart_for(User.objects.get(email="cliente@elcorazon.test"), restaurant)
        CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])
        seconde = commander(as_customer, restaurant, address).data

        assert premiere["reference"] != seconde["reference"]
