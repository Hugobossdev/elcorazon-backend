"""Barème de livraison.

Remplace la constante de l'implémentation précédente, qui valait `5.00` côté
commande et `500.0` côté panier — deux écrans, deux prix, aucune règle. Le
calcul vit désormais à un seul endroit, et c'est celui-ci qui est testé.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.geography.models import DeliveryZone
from apps.geography.services import quote_delivery
from common.exceptions import BusinessRuleViolation
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


class TestCalculDesFrais:
    def test_base_plus_kilometrage(self, zone: DeliveryZone) -> None:
        """500 F de base, 100 F/km, 3,5 km → 850 F."""
        quote = quote_delivery(zone=zone, distance_m=3_500, subtotal=Money(5_000, XOF))

        assert quote.fee == Money(850, XOF)
        assert quote.distance_km == Decimal("3.50")

    def test_l_arrondi_est_explicite(self, zone: DeliveryZone) -> None:
        """1,234 km × 100 F = 123,4 F, arrondi au demi supérieur en devise
        sans décimale. Un flottant donnerait 123,39999… et un franc de moins
        une fois sur deux."""
        quote = quote_delivery(zone=zone, distance_m=1_234, subtotal=Money(5_000, XOF))

        assert quote.fee == Money(500 + 123, XOF)

    def test_une_distance_nulle_ne_facture_que_la_base(self, zone: DeliveryZone) -> None:
        assert quote_delivery(zone=zone, distance_m=0, subtotal=Money(5_000, XOF)).fee == Money(
            500, XOF
        )


class TestFranco:
    def test_au_dela_du_seuil_la_livraison_est_offerte(self, zone: DeliveryZone) -> None:
        zone.free_delivery_threshold = Money(10_000, XOF)
        zone.save()

        quote = quote_delivery(zone=zone, distance_m=3_500, subtotal=Money(10_000, XOF))

        assert quote.is_free is True
        assert quote.fee == Money(0, XOF)

    def test_la_course_garde_sa_valeur_meme_offerte(self, zone: DeliveryZone) -> None:
        """Le franco est une remise faite au client, pas une baisse du coût de
        la course. Confondre les deux ferait rouler le livreur gratuitement
        chaque fois qu'un panier dépasse le seuil."""
        zone.free_delivery_threshold = Money(10_000, XOF)
        zone.save()

        quote = quote_delivery(zone=zone, distance_m=3_500, subtotal=Money(10_000, XOF))

        assert quote.fee == Money(0, XOF)
        assert quote.gross_fee == Money(850, XOF)

    def test_hors_franco_les_deux_montants_coincident(self, zone: DeliveryZone) -> None:
        quote = quote_delivery(zone=zone, distance_m=3_500, subtotal=Money(5_000, XOF))

        assert quote.fee == quote.gross_fee == Money(850, XOF)

    def test_juste_en_dessous_du_seuil_elle_est_due(self, zone: DeliveryZone) -> None:
        zone.free_delivery_threshold = Money(10_000, XOF)
        zone.save()

        quote = quote_delivery(zone=zone, distance_m=3_500, subtotal=Money(9_999, XOF))

        assert quote.is_free is False


class TestRefus:
    def test_au_dela_de_la_distance_maximale(self, zone: DeliveryZone) -> None:
        """Un contour se dessine large ; la distance réellement parcourue est
        ce qui coûte. Être dans le polygone ne suffit donc pas."""
        with pytest.raises(BusinessRuleViolation, match="au-delà des"):
            quote_delivery(zone=zone, distance_m=20_000, subtotal=Money(5_000, XOF))

    def test_en_deca_du_minimum_de_commande(self, zone: DeliveryZone) -> None:
        zone.min_order_amount = Money(2_000, XOF)
        zone.save()

        with pytest.raises(BusinessRuleViolation, match="minimum"):
            quote_delivery(zone=zone, distance_m=1_000, subtotal=Money(1_999, XOF))

    def test_une_devise_etrangere_n_est_pas_convertie(self, zone: DeliveryZone) -> None:
        """Aucune conversion implicite : un taux inventé au passage produirait
        un total faux dont personne ne retrouverait l'origine."""
        with pytest.raises(BusinessRuleViolation, match="EUR"):
            quote_delivery(zone=zone, distance_m=1_000, subtotal=Money(5_000, "EUR"))
