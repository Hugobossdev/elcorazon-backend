"""API du panier collaboratif.

Le test décisif est `test_un_participant_ne_touche_pas_la_ligne_d_un_autre` :
l'implémentation précédente faisait vivre la commande groupée dans l'application,
chaque participant écrivant par abonnement temps réel dans les mêmes lignes de
`orders`. Personne ne disait qui avait le droit de modifier quoi, et une commande
existait en base avant que quiconque ait confirmé.

Vient ensuite `test_la_confirmation_produit_une_seule_commande` : c'est la raison
d'être de la fonctionnalité, et le point où le total de tout le monde doit tomber
juste une seule fois.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.catalog.models import Category, MenuItem
from apps.geography.models import City, DeliveryZone
from apps.groupcarts.models import GroupCart, GroupCartLine
from apps.groupcarts.services import GroupCartService
from apps.groupcarts.states import GroupCartStatus
from apps.groupcarts.tasks import expire_group_carts
from apps.orders.models import Order, PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_host(customer: User) -> APIClient:
    """L'hôte : celui qui ouvre, clôt, confirme et paie."""
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


@pytest.fixture
def guest() -> User:
    return User.objects.create_user("invite@elcorazon.test", "motdepasse", full_name="Yao Adjovi")


@pytest.fixture
def as_guest(guest: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(guest)
    return separate


@pytest.fixture
def outsider() -> User:
    """Un client qui n'a pas été invité — le périmètre à ne pas franchir."""
    return User.objects.create_user(
        "etrangere@elcorazon.test", "motdepasse", full_name="Afi Étrangère"
    )


@pytest.fixture
def second_item(restaurant: Restaurant, category: Category) -> MenuItem:
    return MenuItem.objects.create(
        restaurant=restaurant,
        category=category,
        name="Jus de bissap",
        slug="jus-de-bissap",
        price=Money(1_000, XOF),
    )


@pytest.fixture
def group_cart(customer: User, restaurant: Restaurant) -> GroupCart:
    return GroupCartService.open(host=customer, restaurant=restaurant, title="Déjeuner d'équipe")


@pytest.fixture
def joined(group_cart: GroupCart, guest: User) -> GroupCart:
    """Panier dont l'invité est déjà membre — le cas nominal des tests de ligne."""
    GroupCartService.join(group_cart=group_cart, user=guest)
    return group_cart


# ---------------------------------------------------------------------- raccourcis


def detail_url(group_cart: GroupCart, suffix: str = "") -> str:
    base = reverse("v1:groupcarts:group-cart-detail", args=[group_cart.pk])
    return f"{base}{suffix}"


def add_line(client: APIClient, group_cart: GroupCart, item: MenuItem, **overrides: Any) -> Any:
    return client.post(
        detail_url(group_cart, "lines/"),
        {"menu_item": str(item.pk), "quantity": 1, **overrides},
        format="json",
    )


def confirm_payload(address: Address, **overrides: Any) -> dict[str, Any]:
    return {
        "address": str(address.pk),
        "payment_method": PaymentMethod.CASH,
        **overrides,
    }


class TestOuvertureEtAdhesion:
    def test_l_hote_ouvre_un_panier_et_en_est_membre(
        self, as_host: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Un panier dont l'hôte n'est pas membre lui interdirait d'y déposer ses
        propres plats."""
        response = as_host.post(
            reverse("v1:groupcarts:group-cart-list"),
            {"restaurant": restaurant.slug, "title": "Déjeuner d'équipe"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == GroupCartStatus.OPEN
        assert response.data["host"] == str(customer.pk)
        assert [member["id"] for member in response.data["members"]] == [str(customer.pk)]
        assert len(response.data["code"]) == 6

    def test_l_ouverture_ne_cree_aucune_commande(
        self, as_host: APIClient, restaurant: Restaurant
    ) -> None:
        """C'est le défaut central de l'ancienne version : la commande existait dès
        l'ouverture du panier partagé, donc en base, non payée et non validée."""
        as_host.post(
            reverse("v1:groupcarts:group-cart-list"),
            {"restaurant": restaurant.slug},
            format="json",
        )

        assert not Order.objects.exists()

    def test_l_echeance_est_toujours_posee(
        self, as_host: APIClient, restaurant: Restaurant
    ) -> None:
        """Sans échéance, le groupe attend un hôte qui a oublié."""
        response = as_host.post(
            reverse("v1:groupcarts:group-cart-list"),
            {"restaurant": restaurant.slug},
            format="json",
        )

        assert response.data["closes_at"] is not None
        assert response.data["accepts_contributions"] is True

    def test_une_echeance_hors_bornes_est_refusee(
        self, as_host: APIClient, restaurant: Restaurant
    ) -> None:
        """Un panier ne doit pas survivre au service qu'il prépare."""
        response = as_host.post(
            reverse("v1:groupcarts:group-cart-list"),
            {"restaurant": restaurant.slug, "window_minutes": 100_000},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "business_rule_violation"

    def test_on_rejoint_par_code(
        self, as_guest: APIClient, group_cart: GroupCart, guest: User
    ) -> None:
        response = as_guest.post(
            reverse("v1:groupcarts:group-cart-join"), {"code": group_cart.code}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert str(guest.pk) in [member["id"] for member in response.data["members"]]

    def test_le_code_est_insensible_a_la_casse(
        self, as_guest: APIClient, group_cart: GroupCart
    ) -> None:
        """Il est recopié à la main depuis une conversation : refuser `ab3k9p`
        quand la base contient `AB3K9P` serait un refus sans cause réelle."""
        response = as_guest.post(
            reverse("v1:groupcarts:group-cart-join"),
            {"code": group_cart.code.lower()},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

    def test_rejoindre_deux_fois_n_est_pas_une_erreur(
        self, as_guest: APIClient, group_cart: GroupCart
    ) -> None:
        """Le code circule dans une conversation de groupe : il sera tapoté
        plusieurs fois par la même personne."""
        url = reverse("v1:groupcarts:group-cart-join")
        as_guest.post(url, {"code": group_cart.code}, format="json")
        response = as_guest.post(url, {"code": group_cart.code}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["members"]) == 2

    def test_un_code_inconnu_est_refuse(self, as_guest: APIClient) -> None:
        response = as_guest.post(
            reverse("v1:groupcarts:group-cart-join"), {"code": "ZZZZZZ"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_un_non_membre_ne_voit_pas_le_panier(
        self, client: APIClient, group_cart: GroupCart, outsider: User
    ) -> None:
        """Introuvable plutôt qu'interdit : un 403 confirmerait l'existence du
        panier à qui essaie des identifiants."""
        client.force_authenticate(outsider)

        assert client.get(detail_url(group_cart)).status_code == status.HTTP_404_NOT_FOUND

    def test_le_personnel_n_a_pas_de_panier_de_groupe(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        staff = User.objects.create_user(
            "staff@elcorazon.test", "motdepasse", full_name="Kofi Staff", user_type=UserType.STAFF
        )
        client.force_authenticate(staff)

        response = client.post(
            reverse("v1:groupcarts:group-cart-list"),
            {"restaurant": restaurant.slug},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestContributions:
    def test_chaque_ligne_porte_son_auteur(
        self,
        as_host: APIClient,
        as_guest: APIClient,
        joined: GroupCart,
        menu_item: MenuItem,
        second_item: MenuItem,
        customer: User,
        guest: User,
    ) -> None:
        """C'est ce que l'ancienne implémentation ne savait pas dire, et la seule
        information qui rende l'écran d'une commande groupée lisible."""
        add_line(as_host, joined, menu_item)
        response = add_line(as_guest, joined, second_item)

        assert response.status_code == status.HTTP_201_CREATED
        auteurs = {ligne["name"]: ligne["member"] for ligne in response.data["lines"]}
        assert auteurs["Burger Corazón"] == str(customer.pk)
        assert auteurs["Jus de bissap"] == str(guest.pk)

    def test_le_total_par_participant_est_calcule_serveur(
        self,
        as_host: APIClient,
        as_guest: APIClient,
        joined: GroupCart,
        menu_item: MenuItem,
        second_item: MenuItem,
        customer: User,
        guest: User,
    ) -> None:
        """Un écran qui ne dit pas « tu en es à 3 500 F » laisse chacun ajouter à
        l'aveugle, et c'est l'hôte qui découvre le total."""
        add_line(as_host, joined, menu_item, quantity=2)
        response = add_line(as_guest, joined, second_item)

        par_membre = {ligne["member"]: ligne["total"] for ligne in response.data["per_member"]}
        assert par_membre[str(customer.pk)] == {"amount": "7000", "currency": XOF}
        assert par_membre[str(guest.pk)] == {"amount": "1000", "currency": XOF}
        assert response.data["subtotal"] == {"amount": "8000", "currency": XOF}

    def test_le_prix_envoye_par_le_client_est_ignore(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """La colonne n'existe pas : il n'y a rien à valider, donc rien à
        oublier (C1)."""
        response = add_line(as_host, joined, menu_item, price={"amount": "1", "currency": XOF})

        assert response.data["subtotal"] == {"amount": "3500", "currency": XOF}

    def test_deux_ajouts_identiques_du_meme_membre_se_cumulent(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """Sinon le panier se remplit de doublons à chaque tapotement."""
        add_line(as_host, joined, menu_item)
        response = add_line(as_host, joined, menu_item)

        assert len(response.data["lines"]) == 1
        assert response.data["lines"][0]["quantity"] == 2

    def test_deux_participants_gardent_deux_lignes(
        self, as_host: APIClient, as_guest: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """La fusion est **par membre** : sinon l'écran afficherait « 2 × burger »
        sous un seul nom, et l'autre ne retrouverait pas sa commande."""
        add_line(as_host, joined, menu_item)
        response = add_line(as_guest, joined, menu_item)

        assert len(response.data["lines"]) == 2
        assert {ligne["quantity"] for ligne in response.data["lines"]} == {1}

    def test_un_non_membre_n_ajoute_rien(
        self, client: APIClient, group_cart: GroupCart, outsider: User, menu_item: MenuItem
    ) -> None:
        client.force_authenticate(outsider)

        assert add_line(client, group_cart, menu_item).status_code == status.HTTP_404_NOT_FOUND

    def test_un_article_d_un_autre_restaurant_est_refuse(
        self, as_host: APIClient, joined: GroupCart, zone: DeliveryZone, restaurant: Restaurant
    ) -> None:
        """Une commande ne peut pas mélanger deux établissements : elle est
        préparée à un endroit et enlevée en un seul point."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara-groupe",
            zone=zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000021",
        )
        etranger = MenuItem.objects.create(
            restaurant=ailleurs,
            category=Category.objects.create(restaurant=ailleurs, name="Plats", slug="plats-kara"),
            name="Poulet braisé",
            slug="poulet-braise-kara",
            price=Money(4_000, XOF),
        )

        response = add_line(as_host, joined, etranger)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not GroupCartLine.objects.filter(menu_item=etranger).exists()

    def test_un_participant_ne_touche_pas_la_ligne_d_un_autre(
        self, as_host: APIClient, as_guest: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """Le trou de l'ancienne implémentation : tous les participants écrivaient
        dans les mêmes lignes de commande."""
        add_line(as_host, joined, menu_item)
        ligne = GroupCartLine.objects.get()

        modification = as_guest.patch(
            detail_url(joined, f"lines/{ligne.pk}/"), {"quantity": 9}, format="json"
        )
        suppression = as_guest.delete(detail_url(joined, f"lines/{ligne.pk}/"))

        assert modification.status_code == status.HTTP_409_CONFLICT
        assert suppression.status_code == status.HTTP_409_CONFLICT
        ligne.refresh_from_db()
        assert ligne.quantity == 1

    def test_un_participant_modifie_sa_propre_ligne(
        self, as_guest: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        add_line(as_guest, joined, menu_item)
        ligne = GroupCartLine.objects.get()

        response = as_guest.patch(
            detail_url(joined, f"lines/{ligne.pk}/"), {"quantity": 3}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["lines"][0]["quantity"] == 3

    def test_l_hote_retire_la_ligne_d_un_participant(
        self, as_host: APIClient, as_guest: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """L'hôte paie et assume le total : il doit pouvoir retirer le plat d'un
        participant parti."""
        add_line(as_guest, joined, menu_item)
        ligne = GroupCartLine.objects.get()

        response = as_host.delete(detail_url(joined, f"lines/{ligne.pk}/"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["lines"] == []

    def test_une_ligne_d_un_autre_panier_est_introuvable(
        self,
        as_host: APIClient,
        joined: GroupCart,
        customer: User,
        restaurant: Restaurant,
        menu_item: MenuItem,
    ) -> None:
        """Sans le filtre sur le panier, le contrôle d'auteur porterait sur le
        mauvais panier."""
        autre = GroupCartService.open(host=customer, restaurant=restaurant)
        ligne = GroupCartLine.objects.create(
            group_cart=autre, member=customer, menu_item=menu_item, quantity=1
        )

        response = as_host.patch(
            detail_url(joined, f"lines/{ligne.pk}/"), {"quantity": 2}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestEcheanceEtCloture:
    def test_un_panier_echu_refuse_les_ajouts_avant_la_tache(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """L'échéance est opposée à la contribution, sans attendre la tâche
        planifiée : entre deux tours, un panier échu continuerait sinon d'accepter
        des plats que l'hôte croyait ne plus pouvoir arriver."""
        GroupCart.objects.filter(pk=joined.pk).update(
            closes_at=timezone.now() - dt.timedelta(minutes=1)
        )

        response = add_line(as_host, joined, menu_item)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert joined.status == GroupCartStatus.OPEN  # la tâche n'est pas passée

    def test_la_tache_referme_les_paniers_echus(self, joined: GroupCart) -> None:
        GroupCart.objects.filter(pk=joined.pk).update(
            closes_at=timezone.now() - dt.timedelta(minutes=1)
        )

        assert expire_group_carts() == 1
        joined.refresh_from_db()
        assert joined.status == GroupCartStatus.EXPIRED

    def test_la_tache_epargne_un_panier_encore_a_l_heure(self, joined: GroupCart) -> None:
        assert expire_group_carts() == 0
        joined.refresh_from_db()
        assert joined.status == GroupCartStatus.OPEN

    def test_la_cloture_est_reservee_a_l_hote(self, as_guest: APIClient, joined: GroupCart) -> None:
        response = as_guest.post(detail_url(joined, "lock/"))

        assert response.status_code == status.HTTP_409_CONFLICT
        joined.refresh_from_db()
        assert joined.status == GroupCartStatus.OPEN

    def test_apres_la_cloture_plus_aucun_ajout(
        self, as_host: APIClient, as_guest: APIClient, joined: GroupCart, menu_item: MenuItem
    ) -> None:
        """C'est tout l'intérêt du geste : le total cesse de bouger pendant que
        l'hôte choisit l'adresse et le moyen de paiement."""
        as_host.post(detail_url(joined, "lock/"))

        response = add_line(as_guest, joined, menu_item)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_clore_deux_fois_n_est_pas_une_erreur(
        self, as_host: APIClient, joined: GroupCart
    ) -> None:
        """P1 transposé : une application qui retente ne doit pas voir d'erreur
        pour une action déjà accomplie."""
        as_host.post(detail_url(joined, "lock/"))
        response = as_host.post(detail_url(joined, "lock/"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == GroupCartStatus.LOCKED

    def test_l_hote_annule(self, as_host: APIClient, joined: GroupCart) -> None:
        response = as_host.post(
            detail_url(joined, "cancel/"), {"reason": "Réunion annulée"}, format="json"
        )

        assert response.data["status"] == GroupCartStatus.CANCELLED
        assert not Order.objects.exists()


class TestConfirmation:
    def test_la_confirmation_produit_une_seule_commande(
        self,
        as_host: APIClient,
        as_guest: APIClient,
        joined: GroupCart,
        menu_item: MenuItem,
        second_item: MenuItem,
        address: Address,
        customer: User,
    ) -> None:
        """La raison d'être de la fonctionnalité : les choix de tout le monde,
        un seul passage en cuisine, un seul livreur."""
        add_line(as_host, joined, menu_item, quantity=2)
        add_line(as_guest, joined, second_item)

        response = as_host.post(
            detail_url(joined, "confirm/"), confirm_payload(address), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Order.objects.count() == 1
        order = Order.objects.get()
        assert order.customer_id == customer.pk
        assert {ligne.item_name for ligne in order.lines.all()} == {
            "Burger Corazón",
            "Jus de bissap",
        }
        assert order.subtotal == Money(8_000, XOF)

    def test_le_panier_confirme_porte_sa_commande(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem, address: Address
    ) -> None:
        add_line(as_host, joined, menu_item)

        as_host.post(detail_url(joined, "confirm/"), confirm_payload(address), format="json")

        joined.refresh_from_db()
        assert joined.status == GroupCartStatus.CONFIRMED
        assert joined.order_id == Order.objects.get().pk

    def test_la_confirmation_clot_les_ajouts_d_elle_meme(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem, address: Address
    ) -> None:
        """Sans clôture explicite préalable : ce qui compte est qu'aucune ligne ne
        s'ajoute entre la valorisation et la création de la commande, et c'est le
        verrou de ligne qui le garantit."""
        add_line(as_host, joined, menu_item)

        response = as_host.post(
            detail_url(joined, "confirm/"), confirm_payload(address), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_une_seconde_confirmation_ne_produit_pas_de_seconde_commande(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem, address: Address
    ) -> None:
        """Le panier **est** la clé d'idempotence : un second appel trouve un
        panier déjà `confirmed` et la machine refuse la transition. Aucune clé
        fournie par le client n'était nécessaire."""
        add_line(as_host, joined, menu_item)
        url, payload = detail_url(joined, "confirm/"), confirm_payload(address)

        premiere = as_host.post(url, payload, format="json")
        seconde = as_host.post(url, payload, format="json")

        assert premiere.status_code == status.HTTP_201_CREATED
        assert seconde.status_code == status.HTTP_409_CONFLICT
        assert Order.objects.count() == 1

    def test_un_participant_ne_confirme_pas(
        self, as_guest: APIClient, joined: GroupCart, as_host: APIClient, menu_item: MenuItem
    ) -> None:
        """L'hôte paie : laisser un participant confirmer l'engagerait pour une
        commande qu'il n'a pas relue."""
        add_line(as_host, joined, menu_item)
        adresse_invitee = Address.objects.create(
            user=User.objects.get(email="invite@elcorazon.test"),
            label="Bureau",
            line1="Boulevard du 13 Janvier",
            city=City.objects.first(),
            location=joined.restaurant.location,
        )

        response = as_guest.post(
            detail_url(joined, "confirm/"), confirm_payload(adresse_invitee), format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not Order.objects.exists()

    def test_l_adresse_d_un_tiers_est_refusee(
        self,
        as_host: APIClient,
        joined: GroupCart,
        menu_item: MenuItem,
        outsider: User,
    ) -> None:
        """Même contrôle que sur la création de commande : il n'y a aucune raison
        qu'il soit plus faible parce que le panier était partagé."""
        add_line(as_host, joined, menu_item)
        chez_autrui = Address.objects.create(
            user=outsider,
            label="Chez elle",
            line1="Rue Inconnue",
            city=City.objects.first(),
            location=joined.restaurant.location,
        )

        response = as_host.post(
            detail_url(joined, "confirm/"), confirm_payload(chez_autrui), format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "address" in response.data["errors"]

    def test_un_panier_vide_ne_se_confirme_pas(
        self, as_host: APIClient, joined: GroupCart, address: Address
    ) -> None:
        response = as_host.post(
            detail_url(joined, "confirm/"), confirm_payload(address), format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not Order.objects.exists()

    def test_la_confirmation_decompte_le_stock(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem, address: Address
    ) -> None:
        """La commande passe par `create_from_selection`, donc par le même
        décompte que n'importe quelle commande : le panier collaboratif n'a pas de
        second chemin qui l'oublierait."""
        MenuItem.objects.filter(pk=menu_item.pk).update(tracks_stock=True, stock_quantity=5)
        add_line(as_host, joined, menu_item, quantity=2)

        as_host.post(detail_url(joined, "confirm/"), confirm_payload(address), format="json")

        menu_item.refresh_from_db()
        assert menu_item.stock_quantity == 3

    def test_un_article_devenu_indisponible_bloque_la_confirmation(
        self, as_host: APIClient, joined: GroupCart, menu_item: MenuItem, address: Address
    ) -> None:
        """Le refus nomme l'article : un panier de groupe qui refuse sans dire
        lequel des vingt plats pose problème est inutilisable."""
        add_line(as_host, joined, menu_item)
        MenuItem.objects.filter(pk=menu_item.pk).update(is_available=False)

        response = as_host.post(
            detail_url(joined, "confirm/"), confirm_payload(address), format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert menu_item.name in response.data["unavailable"]
        assert not Order.objects.exists()


class TestContratDEntree:
    def test_l_auteur_d_une_ligne_ne_se_choisit_pas_dans_la_requete(self) -> None:
        """Le champ n'existe dans aucun sérialiseur d'entrée : l'accepter
        laisserait un participant déposer des plats au nom d'un autre — et c'est
        l'hôte qui les paierait."""
        from apps.groupcarts.serializers import GroupCartLineWriteSerializer

        assert "member" not in GroupCartLineWriteSerializer().fields

    def test_ni_le_statut_ni_le_code_ne_s_ecrivent_a_l_ouverture(self) -> None:
        """Le premier naîtrait déjà confirmé ; le second permettrait de se poser
        sur le code d'un panier existant."""
        from apps.groupcarts.serializers import GroupCartOpenSerializer

        champs = GroupCartOpenSerializer().fields
        assert "status" not in champs
        assert "code" not in champs
        assert "order" not in champs
