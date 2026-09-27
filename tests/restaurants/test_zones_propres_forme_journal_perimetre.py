"""Zones propres à une cuisine — forme, journal et périmètre (lot 1, 2026-09-25).

Ce que le lot ajoute à l'existant (cercle et polygone étaient déjà acceptés) :

* **un contour aberrant est refusé à la saisie** — coordonnées hors du globe,
  contour qui se croise, trop de sommets, surface démesurée, rayon hors bornes.
  Aucun ne levait d'erreur : un anneau « nœud papillon » devenait une zone que
  PostGIS jugeait invalide, et dont l'appartenance d'une adresse n'avait plus
  de sens ;
* **chaque geste sur une zone a sa propre entrée au journal** — création,
  contour, barème, activation, suppression. La route des zones propres
  consignait toute modification comme un « barème », et la suppression comme
  une « activation » ;
* **le périmètre est éprouvé geste par geste** : sans session, sans droit,
  depuis une autre cuisine, depuis la sienne.
"""

from __future__ import annotations

import math

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.models import City, DeliveryZone, ZoneShape
from apps.geography.shapes import circle_to_boundary
from apps.restaurants.models import Restaurant, StaffMembership
from apps.restaurants.states import RestaurantStatus
from common.models import AuditEntry
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

LOME = Point(1.2255, 6.1319, srid=4326)
CARRE = [[1.20, 6.10], [1.26, 6.10], [1.26, 6.16], [1.20, 6.16]]
LISTE = "v1:restaurants:managed-restaurant-zone-list"
FICHE = "v1:restaurants:managed-restaurant-zone-detail"


def xof(n: int) -> dict[str, str]:
    return {"amount": str(n), "currency": "XOF"}


@pytest.fixture
def siege() -> APIClient:
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege-lot1@elcorazon.test", "x"))
    return client


@pytest.fixture
def voisine(restaurant: Restaurant) -> Restaurant:
    return Restaurant.objects.create(
        name="Cuisine voisine",
        slug="cuisine-voisine-lot1",
        zone=restaurant.zone,
        address="Voisine",
        location=LOME,
        phone="+22890000005",
        status=RestaurantStatus.ACTIVE,
    )


def gerant_de(etablissement: Restaurant, courriel: str) -> APIClient:
    compte = User.objects.create_user(courriel, "motdepasse", user_type=UserType.STAFF)
    compte.roles.add(
        Role.objects.create(
            name=f"Gérant {courriel}", permissions=["restaurants.read", "restaurants.write"]
        )
    )
    StaffMembership.objects.create(user=compte, restaurant=etablissement)
    client = APIClient()
    client.force_authenticate(compte)
    return client


def zone_de(city: City, etablissement: Restaurant, nom: str = "Bè") -> DeliveryZone:
    return DeliveryZone.objects.create(
        city=city,
        restaurant=etablissement,
        name=nom,
        shape=ZoneShape.CIRCLE,
        center=LOME,
        radius_meters=3000,
        boundary=circle_to_boundary(LOME, 3000),
        base_fee=Money(500, "XOF"),
        fee_per_km=Money(0, "XOF"),
    )


def corps(city: City, etablissement: Restaurant, **surcharges: object) -> dict[str, object]:
    donnees: dict[str, object] = {
        "city": str(city.pk),
        "restaurant": etablissement.slug,
        "name": "Tokoin",
        "shape": "polygon",
        "polygon_coordinates": CARRE,
        "base_fee": xof(500),
        "fee_per_km": xof(0),
        "max_distance_km": "10",
        "estimated_delivery_minutes": 30,
    }
    donnees.update(surcharges)
    return {cle: valeur for cle, valeur in donnees.items() if valeur is not None}


class TestForme:
    def test_un_polygone_valide_est_accepte_et_se_relit(
        self, siege: APIClient, city: City, restaurant: Restaurant
    ) -> None:
        reponse = siege.post(reverse(LISTE), corps(city, restaurant), format="json")

        assert reponse.status_code == 201, reponse.data
        assert reponse.data["shape"] == "polygon"
        # Le contour se relit en GeoJSON : c'est ce que l'éditeur rouvre.
        anneau = reponse.data["boundary"]["coordinates"][0][0]
        assert anneau[0] == anneau[-1], "l'anneau est fermé par le serveur"
        assert len(anneau) == 5

    @pytest.mark.parametrize(
        ("sommets", "raison"),
        [
            ([[1.2, 6.1], [1.3, 6.1], [1.3, 96.0]], "latitude hors du globe"),
            ([[1.2, 6.1], [191.0, 6.1], [1.3, 6.2]], "longitude hors du globe"),
            ([[1.20, 6.10], [1.26, 6.16], [1.26, 6.10], [1.20, 6.16]], "contour qui se croise"),
            ([[1.2, 6.1], [1.3, 6.1]], "moins de trois sommets"),
            ([[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]], "surface démesurée"),
        ],
    )
    def test_un_contour_aberrant_est_refuse(
        self,
        siege: APIClient,
        city: City,
        restaurant: Restaurant,
        sommets: list[list[float]],
        raison: str,
    ) -> None:
        reponse = siege.post(
            reverse(LISTE), corps(city, restaurant, polygon_coordinates=sommets), format="json"
        )

        assert reponse.status_code == 400, raison
        assert "polygon_coordinates" in reponse.data["errors"], raison
        assert not DeliveryZone.objects.filter(name="Tokoin").exists()

    def test_trop_de_sommets(self, siege: APIClient, city: City, restaurant: Restaurant) -> None:
        cercle = [
            [
                1.2255 + 0.01 * math.cos(2 * math.pi * i / 600),
                6.1319 + 0.01 * math.sin(2 * math.pi * i / 600),
            ]
            for i in range(600)
        ]

        reponse = siege.post(
            reverse(LISTE), corps(city, restaurant, polygon_coordinates=cercle), format="json"
        )

        assert reponse.status_code == 400
        assert "polygon_coordinates" in reponse.data["errors"]

    @pytest.mark.parametrize("rayon", [10, 80_000])
    def test_un_rayon_hors_bornes_est_refuse(
        self, siege: APIClient, city: City, restaurant: Restaurant, rayon: int
    ) -> None:
        reponse = siege.post(
            reverse(LISTE),
            corps(
                city,
                restaurant,
                shape="circle",
                polygon_coordinates=None,
                center={"lat": 6.1319, "lon": 1.2255},
                radius_meters=rayon,
            ),
            format="json",
        )

        assert reponse.status_code == 400
        assert "radius_meters" in reponse.data["errors"]


class TestJournal:
    def actions(self, zone_id: object) -> list[str]:
        return list(
            AuditEntry.objects.filter(target_type="zone", target_id=str(zone_id))
            .order_by("created_at")
            .values_list("action", flat=True)
        )

    def test_creation(self, siege: APIClient, city: City, restaurant: Restaurant) -> None:
        reponse = siege.post(reverse(LISTE), corps(city, restaurant), format="json")

        assert self.actions(reponse.data["id"]) == ["zone.create"]

    def test_chaque_modification_dit_ce_qui_a_bouge(
        self, siege: APIClient, city: City, restaurant: Restaurant
    ) -> None:
        zone = zone_de(city, restaurant)
        fiche = reverse(FICHE, args=[zone.pk])

        siege.patch(fiche, {"base_fee": xof(800)}, format="json")
        siege.patch(fiche, {"is_active": False}, format="json")
        siege.patch(fiche, {"shape": "polygon", "polygon_coordinates": CARRE}, format="json")

        assert self.actions(zone.pk) == ["zone.tariff", "zone.activation", "zone.boundary"]

    def test_suppression(self, siege: APIClient, city: City, restaurant: Restaurant) -> None:
        zone = zone_de(city, restaurant)

        siege.delete(reverse(FICHE, args=[zone.pk]))

        assert self.actions(zone.pk) == ["zone.delete"]


class TestPerimetre:
    def test_sans_session(self, city: City, restaurant: Restaurant) -> None:
        zone = zone_de(city, restaurant)
        anonyme = APIClient()

        assert anonyme.get(reverse(LISTE)).status_code == 401
        assert anonyme.delete(reverse(FICHE, args=[zone.pk])).status_code == 401

    def test_un_client_n_a_aucun_droit(self, city: City, restaurant: Restaurant) -> None:
        zone = zone_de(city, restaurant)
        client = APIClient()
        client.force_authenticate(User.objects.create_user("client-lot1@elcorazon.test", "x"))

        assert client.get(reverse(LISTE)).status_code == 403
        assert client.delete(reverse(FICHE, args=[zone.pk])).status_code == 403

    def test_la_cuisine_voisine_ne_voit_ni_ne_touche(
        self, city: City, restaurant: Restaurant, voisine: Restaurant
    ) -> None:
        zone = zone_de(city, restaurant)
        autre = gerant_de(voisine, "gerant-voisin-lot1@elcorazon.test")
        fiche = reverse(FICHE, args=[zone.pk])

        liste = autre.get(reverse(LISTE))
        assert all(z["id"] != str(zone.pk) for z in liste.data["results"])
        assert autre.get(fiche).status_code == 404
        assert autre.patch(fiche, {"base_fee": xof(1)}, format="json").status_code == 404
        assert autre.delete(fiche).status_code == 404
        # Ni créer une zone pour la cuisine d'à côté.
        creation = autre.post(reverse(LISTE), corps(city, restaurant), format="json")
        assert creation.status_code in (403, 404)
        assert DeliveryZone.objects.filter(pk=zone.pk).exists()

    def test_sa_propre_cuisine(self, city: City, restaurant: Restaurant) -> None:
        zone = zone_de(city, restaurant)
        gerant = gerant_de(restaurant, "gerant-lot1@elcorazon.test")

        modification = gerant.patch(
            reverse(FICHE, args=[zone.pk]), {"is_active": False}, format="json"
        )
        assert modification.status_code == 200
        creation = gerant.post(reverse(LISTE), corps(city, restaurant), format="json")
        assert creation.status_code == 201
