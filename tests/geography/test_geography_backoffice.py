"""Administration de la géographie et des établissements — ADR-006, ADR-005.

Deux vérifications portent ce module :

* `test_le_bareme_est_libelle_dans_la_devise_du_pays` — un forfait en euros sur
  une zone togolaise ne se verrait qu'au calcul des frais, c'est-à-dire au
  passage de commande d'un client ;
* `test_un_gerant_ne_change_pas_la_zone_de_son_etablissement` — changer de zone
  change la ville, donc le pays, donc la devise et le barème.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import OpeningHours, Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"

#: Contour carré autour de Lomé, en GeoJSON — la forme que produit un outil de
#: dessin cartographique.
CARRE: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [[[1.15, 6.08], [1.30, 6.08], [1.30, 6.22], [1.15, 6.22], [1.15, 6.08]]],
}


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def siege() -> APIClient:
    return connecte(User.objects.create_superuser("siege@elcorazon.test", "motdepasse"))


@pytest.fixture
def gerant(restaurant: Restaurant) -> APIClient:
    return connecte(
        personnel("gerant@elcorazon.test", restaurant, "restaurants.read", "restaurants.write")
    )


class TestGeographieReserveeAuSiege:
    def test_un_gerant_lit_les_zones_mais_n_en_cree_pas(
        self, gerant: APIClient, city: City
    ) -> None:
        """Une zone n'appartient à aucun établissement : le cloisonnement ne
        peut rien en dire, et le défaut sûr est le refus."""
        lecture = gerant.get(reverse("v1:geography:managed-zone-list"))
        ecriture = gerant.post(
            reverse("v1:geography:managed-zone-list"),
            {
                "city": str(city.pk),
                "name": "Nouvelle zone",
                "boundary": CARRE,
                "base_fee": {"amount": "500", "currency": XOF},
                "fee_per_km": {"amount": "100", "currency": XOF},
            },
            format="json",
        )

        assert lecture.status_code == status.HTTP_200_OK
        assert ecriture.status_code == status.HTTP_403_FORBIDDEN

    def test_le_siege_cree_une_zone_depuis_un_contour_geojson(
        self, siege: APIClient, city: City
    ) -> None:
        """Un `Polygon` est accepté et converti : une zone d'un seul tenant est
        le cas courant, et l'exiger en `MultiPolygon` ferait échouer l'export
        de tous les outils de dessin."""
        response = siege.post(
            reverse("v1:geography:managed-zone-list"),
            {
                "city": str(city.pk),
                "name": "Est",
                "boundary": CARRE,
                "base_fee": {"amount": "700", "currency": XOF},
                "fee_per_km": {"amount": "120", "currency": XOF},
                "max_distance_km": "12.00",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        zone = DeliveryZone.objects.get(name="Est")
        assert zone.boundary.geom_type == "MultiPolygon"
        assert zone.base_fee.amount_minor == 700

    def test_le_bareme_est_libelle_dans_la_devise_du_pays(
        self, siege: APIClient, city: City
    ) -> None:
        """La devise est héritée du pays (ADR-006) ; un forfait en euros ne se
        verrait qu'au calcul des frais, sur le chemin du chiffre d'affaires."""
        response = siege.post(
            reverse("v1:geography:managed-zone-list"),
            {
                "city": str(city.pk),
                "name": "Ouest",
                "boundary": CARRE,
                "base_fee": {"amount": "500", "currency": "EUR"},
                "fee_per_km": {"amount": "100", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_contour_illisible_est_refuse(self, siege: APIClient, city: City) -> None:
        response = siege.post(
            reverse("v1:geography:managed-zone-list"),
            {
                "city": str(city.pk),
                "name": "Bancale",
                "boundary": {"type": "Point", "coordinates": [1.2, 6.1]},
                "base_fee": {"amount": "500", "currency": XOF},
                "fee_per_km": {"amount": "100", "currency": XOF},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_pays_se_ferme_sans_s_effacer(self, siege: APIClient, country: Country) -> None:
        """Commandes, adresses et établissements y renvoient : les clés sont en
        `PROTECT`, et un `DELETE` échouerait en violation d'intégrité plutôt
        que par une règle lisible."""
        url = reverse("v1:geography:managed-country-detail", args=[country.pk])

        ferme = siege.patch(url, {"is_active": False}, format="json")

        assert ferme.status_code == status.HTTP_200_OK
        assert siege.delete(url).status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        country.refresh_from_db()
        assert not country.is_active


class TestEtablissements:
    def test_ouvrir_un_etablissement_releve_du_siege(
        self, gerant: APIClient, zone: DeliveryZone
    ) -> None:
        """Une création s'attribuerait un périmètre qu'on ne lui a pas donné."""
        response = gerant.post(
            reverse("v1:restaurants:managed-restaurant-list"),
            {
                "name": "El Corazón Kara",
                "slug": "el-corazon-kara",
                "zone": str(zone.pk),
                "address": "Kara",
                "location": {"lat": 6.13, "lon": 1.22},
                "phone": "+22890000009",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_gerant_suspend_la_prise_de_commande_de_son_etablissement(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        """`accepts_orders` est conjoncturel, `is_active` est structurel : les
        confondre ferait disparaître le restaurant pour arrêter les commandes
        une heure."""
        response = gerant.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"accepts_orders": False},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        restaurant.refresh_from_db()
        assert not restaurant.accepts_orders
        assert restaurant.is_active

    def test_un_gerant_ne_change_pas_la_zone_de_son_etablissement(
        self, gerant: APIClient, restaurant: Restaurant, city: City
    ) -> None:
        """Changer de zone change la ville, donc le pays, donc la devise."""
        ailleurs = DeliveryZone.objects.create(
            city=city,
            name="Autre",
            boundary=restaurant.zone.boundary,
            base_fee=restaurant.zone.base_fee,
            fee_per_km=restaurant.zone.fee_per_km,
        )

        response = gerant.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"zone": str(ailleurs.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        restaurant.refresh_from_db()
        assert restaurant.zone_id != ailleurs.pk

    def test_un_etablissement_hors_perimetre_est_introuvable(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000010",
        )

        liste = gerant.get(reverse("v1:restaurants:managed-restaurant-list"))
        fiche = gerant.get(
            reverse("v1:restaurants:managed-restaurant-detail", args=[ailleurs.slug])
        )

        assert [f["slug"] for f in liste.data["results"]] == [restaurant.slug]
        assert fiche.status_code == status.HTTP_404_NOT_FOUND

    def test_la_devise_ne_se_saisit_pas(self, siege: APIClient, restaurant: Restaurant) -> None:
        """Héritée du pays : deux établissements du même marché ne peuvent pas
        facturer dans deux unités."""
        response = siege.get(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug])
        )

        assert response.data["currency"] == XOF
        from apps.restaurants.serializers import ManagedRestaurantSerializer

        assert ManagedRestaurantSerializer().fields["currency"].read_only


class TestHoraires:
    def test_une_plage_de_nuit_se_saisit_telle_quelle(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        """`22:00 → 02:00` : `closes_at < opens_at` est la représentation, et
        obliger à saisir deux plages sur deux jours serait la source d'erreur
        classique du service de nuit."""
        response = gerant.post(
            reverse("v1:restaurants:managed-opening-hours-list"),
            {
                "restaurant": str(restaurant.pk),
                "weekday": 5,
                "opens_at": "22:00",
                "closes_at": "02:00",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["crosses_midnight"] is True

    def test_une_plage_vide_est_refusee_en_400(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        """La contrainte `CHECK` existe et sortirait en 500 ; `22:00 → 22:00`
        est une faute de saisie courante, qui mérite un message."""
        response = gerant.post(
            reverse("v1:restaurants:managed-opening-hours-list"),
            {
                "restaurant": str(restaurant.pk),
                "weekday": 1,
                "opens_at": "22:00",
                "closes_at": "22:00",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_plage_se_supprime_reellement(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        """Elle n'est référencée par rien, et une plage « désactivée » restant
        affichée dans un tableau hebdomadaire serait plus déroutante qu'utile."""
        plage = OpeningHours.objects.create(
            restaurant=restaurant, weekday=0, opens_at="11:00", closes_at="23:00"
        )

        response = gerant.delete(
            reverse("v1:restaurants:managed-opening-hours-detail", args=[plage.pk])
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not OpeningHours.objects.filter(pk=plage.pk).exists()

    def test_les_horaires_d_un_autre_etablissement_sont_invisibles(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000011",
        )
        OpeningHours.objects.create(
            restaurant=ailleurs, weekday=0, opens_at="11:00", closes_at="23:00"
        )

        response = gerant.get(reverse("v1:restaurants:managed-opening-hours-list"))

        assert response.data["count"] == 0
