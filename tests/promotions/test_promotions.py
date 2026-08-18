"""Codes promotionnels — invariant F4.

Cinq conditions, et il faut les cinq. Cette suite les prend une par une, puis
vérifie ce qui les tient réellement : le devis ne réserve rien, la
consommation se fait sous verrou, et une commande annulée rend le code.

Le test qui porte l'ensemble est
`test_le_montant_de_la_remise_n_est_jamais_accepte_du_client` : c'est C1
transposé aux promotions. Un client qui annoncerait sa propre remise serait la
même faille que celui qui annonçait son propre prix.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.carts.services import CartService
from apps.catalog.models import MenuItem
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.profiles.models import Address
from apps.promotions.models import DiscountKind, Promotion, PromotionRedemption
from apps.promotions.services import PromotionRefused, PromotionService
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def promo(**overrides: Any) -> Promotion:
    defaults: dict[str, Any] = {
        "code": "BIENVENUE",
        "kind": DiscountKind.PERCENTAGE,
        "percentage": 20,
        "starts_at": timezone.now() - dt.timedelta(days=1),
        "ends_at": timezone.now() + dt.timedelta(days=7),
    }
    return Promotion.objects.create(**{**defaults, **overrides})


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def garni(customer: User, restaurant: Restaurant, menu_item: MenuItem) -> None:
    """Panier d'un burger à 3 500 F."""
    cart = CartService.cart_for(customer, restaurant)
    CartService.add_line(cart=cart, menu_item=menu_item, quantity=1, options=[])


def devis(
    code: str, user: User, restaurant: Restaurant, subtotal: int = 5_000, frais: int = 500
) -> Any:
    return PromotionService.quote(
        code=code,
        user=user,
        restaurant=restaurant,
        subtotal=Money(subtotal, XOF),
        delivery_fee=Money(frais, XOF),
    )


class TestBareme:
    def test_un_pourcentage_s_applique_au_sous_total(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        promo()

        assert devis("BIENVENUE", customer, restaurant).discount == Money(1_000, XOF)

    def test_un_montant_fixe_s_applique_tel_quel(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        promo(code="MOINS500", kind=DiscountKind.FIXED, percentage=0, amount=Money(500, XOF))

        assert devis("MOINS500", customer, restaurant).discount == Money(500, XOF)

    def test_la_livraison_offerte_vaut_les_frais(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        promo(code="LIVRAISON", kind=DiscountKind.FREE_DELIVERY, percentage=0)

        assert devis("LIVRAISON", customer, restaurant, frais=750).discount == Money(750, XOF)

    def test_le_code_est_insensible_a_la_casse(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Un client tape en minuscules ; refuser pour cette raison serait une
        énigme."""
        promo()

        assert devis("bienvenue", customer, restaurant).discount == Money(1_000, XOF)


class TestLesCinqConditions:
    """F4 — une seule oubliée et le code devient une fuite."""

    def test_un_code_inexistant_est_refuse(self, customer: User, restaurant: Restaurant) -> None:
        with pytest.raises(PromotionRefused, match="n'existe pas"):
            devis("INVENTE", customer, restaurant)

    def test_hors_periode(self, customer: User, restaurant: Restaurant) -> None:
        promo(
            starts_at=timezone.now() - dt.timedelta(days=30),
            ends_at=timezone.now() - dt.timedelta(days=1),
        )

        with pytest.raises(PromotionRefused, match="plus valable"):
            devis("BIENVENUE", customer, restaurant)

    def test_suspendu_par_l_exploitation(self, customer: User, restaurant: Restaurant) -> None:
        """`is_active` et la période disent deux choses différentes : l'une est
        une décision, l'autre un calendrier."""
        promo(is_active=False)

        with pytest.raises(PromotionRefused):
            devis("BIENVENUE", customer, restaurant)

    def test_en_dessous_du_minimum_de_commande(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        promo(min_order_amount=Money(10_000, XOF))

        with pytest.raises(PromotionRefused, match="au moins"):
            devis("BIENVENUE", customer, restaurant, subtotal=9_999)

    def test_le_plafond_borne_un_pourcentage(self, customer: User, restaurant: Restaurant) -> None:
        """Sans plafond, « −20 % » sur une commande de groupe coûte ce qu'on
        n'avait pas prévu."""
        promo(max_discount=Money(1_500, XOF))

        assert devis("BIENVENUE", customer, restaurant, subtotal=100_000).discount == Money(
            1_500, XOF
        )

    def test_le_quota_global_epuise(self, customer: User, restaurant: Restaurant) -> None:
        promotion = promo(usage_limit=2)
        Promotion.objects.filter(pk=promotion.pk).update(used_count=2)

        with pytest.raises(PromotionRefused, match="limite"):
            devis("BIENVENUE", customer, restaurant)

    def test_le_quota_par_personne_epuise(self, customer: User, restaurant: Restaurant) -> None:
        promotion = promo(usage_limit_per_user=1)
        PromotionRedemption.objects.create(
            promotion=promotion, user=customer, order_id=uuid.uuid4(), discount=Money(100, XOF)
        )

        with pytest.raises(PromotionRefused, match="déjà utilisé"):
            devis("BIENVENUE", customer, restaurant)

    def test_le_quota_par_personne_ne_gene_pas_les_autres(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        promotion = promo(usage_limit_per_user=1)
        PromotionRedemption.objects.create(
            promotion=promotion, user=courier_user, order_id=uuid.uuid4(), discount=Money(100, XOF)
        )

        assert devis("BIENVENUE", customer, restaurant).discount == Money(1_000, XOF)

    def test_un_code_d_etablissement_ne_vaut_pas_ailleurs(
        self, customer: User, restaurant: Restaurant, zone: Any
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="Autre",
            slug="autre-promo",
            zone=zone,
            address="X",
            location=restaurant.location,
            phone="+22890000031",
        )
        promo(restaurant=ailleurs)

        with pytest.raises(PromotionRefused, match="établissement"):
            devis("BIENVENUE", customer, restaurant)


class TestBornesDeSecurite:
    def test_la_remise_ne_depasse_jamais_ce_qu_il_y_a_a_remiser(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """La base refuse une remise supérieure au sous-total plus les frais :
        au-delà, la « commande » rapporterait de l'argent au client. Mieux vaut
        la borner ici que se heurter à une violation d'intégrité au milieu d'un
        passage de commande."""
        promo(code="ENORME", kind=DiscountKind.FIXED, percentage=0, amount=Money(999_999, XOF))

        assert devis("ENORME", customer, restaurant, subtotal=1_000, frais=500).discount == Money(
            1_500, XOF
        )

    def test_une_devise_etrangere_est_refusee(self, customer: User, restaurant: Restaurant) -> None:
        promo(code="EURO", kind=DiscountKind.FIXED, percentage=0, amount=Money(500, "EUR"))

        with pytest.raises(PromotionRefused, match="EUR"):
            devis("EURO", customer, restaurant)

    def test_un_code_qui_ne_remise_rien_est_refuse(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Un « livraison offerte » sur une commande à retirer sur place n'a
        rien à offrir : le dire vaut mieux que rendre une remise nulle."""
        promo(code="LIVRAISON", kind=DiscountKind.FREE_DELIVERY, percentage=0)

        with pytest.raises(PromotionRefused, match="ne réduit rien"):
            devis("LIVRAISON", customer, restaurant, frais=0)


class TestConsommation:
    def test_le_devis_ne_reserve_rien(self, customer: User, restaurant: Restaurant) -> None:
        """Sinon un code s'épuiserait en le tapant, sans jamais commander."""
        promotion = promo(usage_limit=1)

        devis("BIENVENUE", customer, restaurant)
        devis("BIENVENUE", customer, restaurant)

        promotion.refresh_from_db()
        assert promotion.used_count == 0

    def test_la_consommation_decompte(self, customer: User, restaurant: Restaurant) -> None:
        promotion = promo()

        PromotionService.redeem(
            promotion=promotion, user=customer, order_id=uuid.uuid4(), discount=Money(1_000, XOF)
        )

        promotion.refresh_from_db()
        assert promotion.used_count == 1

    def test_le_quota_est_reverifie_dans_le_verrou(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Entre le devis et la validation, quelqu'un d'autre a pu prendre le
        dernier coupon. Vérifier au devis seul rendrait la limite indicative."""
        promotion = promo(usage_limit=1)
        Promotion.objects.filter(pk=promotion.pk).update(used_count=1)

        with pytest.raises(PromotionRefused, match="limite"):
            PromotionService.redeem(
                promotion=promotion,
                user=customer,
                order_id=uuid.uuid4(),
                discount=Money(1_000, XOF),
            )

    def test_la_meme_commande_ne_consomme_qu_une_fois(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """L'idempotence de l'ADR-009 permet de rejouer une création : le quota
        ne doit pas se décompter deux fois pour un seul repas."""
        promotion = promo()
        commande = uuid.uuid4()

        for _ in range(2):
            PromotionService.redeem(
                promotion=promotion, user=customer, order_id=commande, discount=Money(1_000, XOF)
            )

        promotion.refresh_from_db()
        assert promotion.used_count == 1
        assert PromotionRedemption.objects.count() == 1

    def test_la_meme_commande_se_rejoue_malgre_un_quota_par_personne(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Le rejeu ne doit pas se heurter au quota qu'il a lui-même consommé.

        Les quotas étaient vérifiés **avant** la recherche d'une utilisation
        existante, si bien qu'un rejeu se voyait refuser par le décompte de sa
        propre consommation : « Vous avez déjà utilisé ce code », alors qu'il
        s'agissait de la même commande. Le rattrapage de la violation d'unicité,
        censé porter l'idempotence de l'ADR-009, était inatteignable dans ce cas.
        """
        promotion = promo(usage_limit_per_user=1)
        commande = uuid.uuid4()

        premiere = PromotionService.redeem(
            promotion=promotion, user=customer, order_id=commande, discount=Money(1_000, XOF)
        )
        seconde = PromotionService.redeem(
            promotion=promotion, user=customer, order_id=commande, discount=Money(1_000, XOF)
        )

        assert seconde.pk == premiere.pk
        promotion.refresh_from_db()
        assert promotion.used_count == 1
        assert PromotionRedemption.objects.count() == 1

    def test_le_rejeu_passe_par_une_lecture_sans_tenter_d_insertion(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """Plus d'insertion vouée à violer
        `one_redemption_per_promotion_and_order` : le rejeu se résout par un
        `SELECT`, et n'inscrit donc plus d'erreur au journal PostgreSQL pour un
        cas parfaitement normal."""
        promotion = promo()
        commande = uuid.uuid4()
        PromotionService.redeem(
            promotion=promotion, user=customer, order_id=commande, discount=Money(1_000, XOF)
        )

        with CaptureQueriesContext(connection) as capture:
            PromotionService.redeem(
                promotion=promotion, user=customer, order_id=commande, discount=Money(1_000, XOF)
            )

        inserts = [
            q["sql"]
            for q in capture.captured_queries
            if "INSERT" in q["sql"] and "promotions_promotionredemption" in q["sql"]
        ]
        assert not inserts, f"Le rejeu tente encore une insertion : {inserts}"

    def test_un_quota_par_personne_reste_oppose_a_une_autre_commande(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """La contrepartie du test précédent : rendre le rejeu possible ne doit
        pas rendre le quota inopérant sur une commande *différente*."""
        promotion = promo(usage_limit_per_user=1)
        PromotionService.redeem(
            promotion=promotion, user=customer, order_id=uuid.uuid4(), discount=Money(1_000, XOF)
        )

        with pytest.raises(PromotionRefused, match="déjà utilisé"):
            PromotionService.redeem(
                promotion=promotion,
                user=customer,
                order_id=uuid.uuid4(),
                discount=Money(1_000, XOF),
            )


class TestPassageDeCommande:
    def test_la_remise_apparait_sur_la_commande(
        self, customer: User, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        promo()

        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
            promo_code="BIENVENUE",
        )

        assert order.discount == Money(700, XOF)  # 20 % de 3 500
        assert order.promo_code == "BIENVENUE"
        assert order.total == order.subtotal + order.delivery_fee - order.discount

    def test_sans_code_la_remise_est_nulle(
        self, customer: User, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
        )

        assert order.discount == Money(0, XOF)
        assert order.promo_code == ""

    def test_un_code_refuse_empeche_la_commande(
        self, customer: User, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Plutôt que de commander sans la remise annoncée : le client a choisi
        son panier en fonction du code."""
        promo(min_order_amount=Money(50_000, XOF))

        with pytest.raises(PromotionRefused):
            OrderService.create_from_cart(
                user=customer,
                cart=CartService.cart_for(customer, restaurant),
                address=address,
                payment_method="cash",
                promo_code="BIENVENUE",
            )

        assert Order.objects.count() == 0

    def test_la_livraison_offerte_ne_prive_pas_le_livreur(
        self, customer: User, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Le même raisonnement que pour le franco : la remise est faite au
        client, pas prélevée sur la course."""
        promo(code="LIVRAISON", kind=DiscountKind.FREE_DELIVERY, percentage=0)

        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
            promo_code="LIVRAISON",
        )

        assert order.discount == order.delivery_fee
        assert order.delivery_fee_gross is not None
        assert order.delivery_fee_gross.is_positive

    def test_une_annulation_rend_le_code(
        self, customer: User, restaurant: Restaurant, address: Address, garni: None
    ) -> None:
        """Le client ne doit pas perdre son code parce que le restaurant a
        annulé : il a été décompté pour un repas qu'il n'a jamais reçu."""
        promotion = promo(usage_limit=1)
        order = OrderService.create_from_cart(
            user=customer,
            cart=CartService.cart_for(customer, restaurant),
            address=address,
            payment_method="cash",
            promo_code="BIENVENUE",
        )

        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED, reason="Rupture")

        promotion.refresh_from_db()
        assert promotion.used_count == 0
        assert PromotionRedemption.objects.count() == 0


class TestDevisAvantCommande:
    def test_le_client_verifie_son_code_avant_de_commander(
        self, as_customer: APIClient, restaurant: Restaurant, garni: None
    ) -> None:
        promo()

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"promo_code": "BIENVENUE", "restaurant": restaurant.slug},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["discount"] == {"amount": "700", "currency": XOF}
        assert response.data["promotion"]["code"] == "BIENVENUE"

    def test_la_validation_ne_consomme_pas(
        self, as_customer: APIClient, restaurant: Restaurant, garni: None
    ) -> None:
        promotion = promo(usage_limit=1)

        for _ in range(3):
            as_customer.post(
                reverse("v1:orders:order-preview"),
                {"promo_code": "BIENVENUE", "restaurant": restaurant.slug},
                format="json",
            )

        promotion.refresh_from_db()
        assert promotion.used_count == 0

    def test_un_refus_dit_pourquoi(
        self, as_customer: APIClient, restaurant: Restaurant, garni: None
    ) -> None:
        """« Vous n'atteignez pas le minimum » se corrige en ajoutant un
        article ; « le code est expiré » non."""
        promo(min_order_amount=Money(50_000, XOF))

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"promo_code": "BIENVENUE", "restaurant": restaurant.slug},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "promotion_refused"
        assert response.data["min_order_amount"] == "50000"

    def test_un_panier_vide_ne_remise_rien(
        self, as_customer: APIClient, restaurant: Restaurant
    ) -> None:
        """Le devis répond quand même : un écran de panier vide doit pouvoir
        s'afficher sans traiter une erreur."""
        promo()

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"promo_code": "BIENVENUE", "restaurant": restaurant.slug},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["discount"] == {"amount": "0", "currency": XOF}
        assert response.data["is_orderable"] is False

    def test_le_montant_de_la_remise_n_est_jamais_accepte_du_client(self) -> None:
        """C1 transposé : le client envoie **un code**, jamais un montant. Le
        champ n'existe dans aucun sérialiseur d'entrée."""
        from apps.orders.serializers import OrderCreateSerializer, OrderPreviewSerializer

        assert "discount" not in OrderCreateSerializer().fields
        assert "discount" not in OrderPreviewSerializer().fields
        assert "promo_code" in OrderCreateSerializer().fields

    def test_le_devis_donne_le_detail_et_pas_seulement_le_total(
        self, as_customer: APIClient, restaurant: Restaurant, garni: None
    ) -> None:
        """Un client qui voit « 4 200 F » sans savoir ce qui vient des frais et
        ce qui vient de la remise n'a aucun moyen de vérifier."""
        promo()

        response = as_customer.post(
            reverse("v1:orders:order-preview"),
            {"promo_code": "BIENVENUE", "restaurant": restaurant.slug},
            format="json",
        )

        assert set(response.data) == {
            "subtotal",
            "delivery_fee",
            "discount",
            "total",
            "promotion",
            "is_orderable",
        }
