"""Journal des décisions d'exploitation.

Aucune trace n'existait. Déplacer un établissement, redessiner une zone, doubler
un forfait : tout s'écrivait sans que rien ne dise qui l'avait décidé ni ce qu'il
y avait avant. Ces trois écritures sont **silencieuses et coûteuses** — un rayon
réduit de deux kilomètres ne se voit que dans les commandes qu'on ne reçoit plus.

Deux propriétés portent cette suite, et la seconde compte autant que la première :

* ce qui change laisse une trace, **avec sa valeur d'avant** ;
* ce qui ne change pas n'en laisse **aucune**. Un journal bruyant ne s'ouvre
  plus, et un formulaire de back-office renvoie tous ses champs à chaque
  validation.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.geography.models import City, DeliveryZone
from apps.restaurants.models import Restaurant
from common.audit import AuditAction, record_change
from common.models import AuditEntry
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def as_siege() -> APIClient:
    compte = User.objects.create_superuser("siege-audit@elcorazon.test", "motdepasse")
    client = APIClient()
    client.force_authenticate(compte)
    return client


class TestEnregistrement:
    def test_un_changement_laisse_sa_valeur_d_avant(self) -> None:
        entree = record_change(
            actor=None,
            action=AuditAction.ZONE_TARIFF,
            target_type="zone",
            target_id="abc",
            target_label="Cocody",
            before={"base_fee": "500 XOF"},
            after={"base_fee": "1500 XOF"},
        )

        assert entree is not None
        assert entree.before == {"base_fee": "500 XOF"}
        assert entree.after == {"base_fee": "1500 XOF"}

    def test_une_écriture_sans_changement_n_ecrit_rien(self) -> None:
        """Sinon corriger un numéro de téléphone consignerait « position inchangée ».

        Le journal se remplirait de bruit jusqu'à ce que plus personne ne
        l'ouvre — ce qui est pire qu'un journal absent, parce qu'on croit
        l'avoir.
        """
        entree = record_change(
            actor=None,
            action=AuditAction.ZONE_TARIFF,
            target_type="zone",
            target_id="abc",
            target_label="Cocody",
            before={"base_fee": "500 XOF"},
            after={"base_fee": "500 XOF"},
        )

        assert entree is None
        assert AuditEntry.objects.count() == 0

    def test_le_libelle_survit_a_sa_cible(self) -> None:
        """« zone Cocody » doit rester lisible après un renommage.

        Une jointure vers la zone rendrait le journal muet précisément dans les
        cas où on l'ouvre : après une suppression, ou après le renommage qu'on
        cherche à comprendre.
        """
        record_change(
            actor=None,
            action=AuditAction.ZONE_BOUNDARY,
            target_type="zone",
            target_id="zone-supprimee",
            target_label="Cocody — Abidjan",
            before={"radius_meters": 5000},
            after={"radius_meters": 9000},
        )

        assert AuditEntry.objects.get().target_label == "Cocody — Abidjan"


class TestDeplacementDUnEtablissement:
    def test_deplacer_une_cuisine_est_consigne(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        reponse = as_siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"location": {"lat": 6.1400, "lon": 1.2300}, "address": "Nouvelle adresse"},
            format="json",
        )

        assert reponse.status_code == 200
        entree = AuditEntry.objects.get(action=AuditAction.RESTAURANT_LOCATION)
        assert entree.before["location"] == [6.1319, 1.2255]
        assert entree.after["location"] == [6.14, 1.23]
        assert entree.actor is not None
        assert entree.target_label == restaurant.name

    def test_corriger_un_telephone_ne_consigne_rien(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        """La garde qui rend le journal lisible."""
        as_siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"phone": "+22890999999"},
            format="json",
        )

        assert AuditEntry.objects.count() == 0

    def test_un_deplacement_infime_ne_consigne_rien(
        self, as_siege: APIClient, restaurant: Restaurant
    ) -> None:
        """Onze centimètres viennent du flottant, pas d'un geste.

        Consigner un « déplacement » de trois millimètres remplirait le journal
        de bruit.
        """
        as_siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"location": {"lat": 6.13190000001, "lon": 1.22550000001}},
            format="json",
        )

        assert not AuditEntry.objects.filter(action=AuditAction.RESTAURANT_LOCATION).exists()


class TestZones:
    @pytest.fixture
    def zone_simple(self, city: City) -> DeliveryZone:
        from apps.geography.models import ZoneShape
        from apps.geography.shapes import circle_to_boundary

        centre = Point(1.2255, 6.1319, srid=4326)
        return DeliveryZone.objects.create(
            city=city,
            name="Zone journalisée",
            shape=ZoneShape.CIRCLE,
            center=centre,
            radius_meters=5000,
            boundary=circle_to_boundary(centre, 5000),
            base_fee=Money(500, XOF),
            fee_per_km=Money(100, XOF),
        )

    def test_doubler_un_forfait_est_consigne(
        self, as_siege: APIClient, zone_simple: DeliveryZone
    ) -> None:
        reponse = as_siege.patch(
            reverse("v1:geography:managed-zone-detail", args=[zone_simple.pk]),
            {"base_fee": {"amount": "1500", "currency": XOF}},
            format="json",
        )

        assert reponse.status_code == 200
        entree = AuditEntry.objects.get(action=AuditAction.ZONE_TARIFF)
        assert entree.before["base_fee"] == "500 XOF"
        assert entree.after["base_fee"] == "1500 XOF"

    def test_redimensionner_une_zone_est_consigne(
        self, as_siege: APIClient, zone_simple: DeliveryZone
    ) -> None:
        """Un rayon réduit ne se voit que dans les commandes qu'on ne reçoit plus."""
        reponse = as_siege.patch(
            reverse("v1:geography:managed-zone-detail", args=[zone_simple.pk]),
            {"radius_meters": 2000},
            format="json",
        )

        assert reponse.status_code == 200, reponse.data
        entree = AuditEntry.objects.get(action=AuditAction.ZONE_BOUNDARY)
        assert entree.before["radius_meters"] == 5000
        assert entree.after["radius_meters"] == 2000
        # Le contour lui-même n'est pas recopié — plusieurs kilo-octets de
        # sommets rendraient le journal illisible. Son empreinte suffit à dire
        # *qu'il* a changé.
        assert entree.before["boundary_digest"] != entree.after["boundary_digest"]

    def test_renommer_une_zone_ne_consigne_rien(
        self, as_siege: APIClient, zone_simple: DeliveryZone
    ) -> None:
        """Seules la géographie et les barèmes sont journalisés.

        Tout journaliser produirait un volume qui rend le journal inutilisé.
        """
        as_siege.patch(
            reverse("v1:geography:managed-zone-detail", args=[zone_simple.pk]),
            {"name": "Autre nom"},
            format="json",
        )

        assert AuditEntry.objects.count() == 0

    def test_desactiver_une_zone_est_consigne(
        self, as_siege: APIClient, zone_simple: DeliveryZone
    ) -> None:
        as_siege.patch(
            reverse("v1:geography:managed-zone-detail", args=[zone_simple.pk]),
            {"is_active": False},
            format="json",
        )

        entree = AuditEntry.objects.get(action=AuditAction.ZONE_ACTIVATION)
        assert entree.before == {"is_active": True}
        assert entree.after == {"is_active": False}
