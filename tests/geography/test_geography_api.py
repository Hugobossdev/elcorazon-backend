"""API de la géographie — ADR-006.

Le test qui porte cette suite est celui de la résolution de zone : c'est la
réponse qui décide des frais annoncés au client, et c'est ce que
l'implémentation précédente n'avait pas du tout — une constante, contradictoire
entre deux écrans.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.geography.models import City, Country, DeliveryZone
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def square(west: float, south: float, side: float) -> MultiPolygon:
    ring = Polygon(
        (
            (west, south),
            (west + side, south),
            (west + side, south + side),
            (west, south + side),
            (west, south),
        ),
        srid=4326,
    )
    return MultiPolygon(ring, srid=4326)


class TestListes:
    def test_les_pays_sont_lisibles_sans_compte(self, client: APIClient, country: Country) -> None:
        """L'écran de choix du pays précède l'inscription : exiger un jeton ici
        obligerait à créer un compte pour savoir si le service existe chez
        soi."""
        response = client.get(reverse("v1:geography:country-list"))

        assert response.status_code == status.HTTP_200_OK
        assert [c["iso_code"] for c in response.data["results"]] == ["TG"]

    def test_un_pays_desactive_disparait_de_l_api(
        self, client: APIClient, country: Country
    ) -> None:
        """Désactivé, pas supprimé : sa devise reste nécessaire à la lecture
        des commandes déjà passées là-bas."""
        Country.objects.filter(pk=country.pk).update(is_active=False)

        response = client.get(reverse("v1:geography:country-list"))

        assert response.data["count"] == 0
        assert Country.objects.filter(pk=country.pk).exists()

    def test_les_villes_se_filtrent_par_code_iso(self, client: APIClient, city: City) -> None:
        """Le client a `TG` en main, pas l'UUID du pays."""
        response = client.get(reverse("v1:geography:city-list"), {"country__iso_code": "TG"})

        assert [c["slug"] for c in response.data["results"]] == ["lome"]
        assert response.data["results"][0]["country"]["currency"] == XOF

    def test_la_ville_porte_sa_position_nommee(self, client: APIClient, city: City) -> None:
        centroid = client.get(reverse("v1:geography:city-list")).data["results"][0]["centroid"]

        assert centroid == {"lat": pytest.approx(6.1319), "lon": pytest.approx(1.2255)}


class TestResolutionDeZone:
    def test_un_point_couvert_renvoie_le_bareme(
        self, client: APIClient, zone: DeliveryZone
    ) -> None:
        response = client.get(reverse("v1:geography:zone-resolve"), {"lat": 6.1319, "lon": 1.2255})

        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_covered"] is True
        assert response.data["zone"]["base_fee"] == {"amount": "500", "currency": XOF}
        assert response.data["zone"]["fee_per_km"] == {"amount": "100", "currency": XOF}

    def test_un_point_hors_couverture_n_est_pas_une_erreur(
        self, client: APIClient, zone: DeliveryZone
    ) -> None:
        """« Je viens d'emménager hors zone » est une réponse légitime à une
        question légitime. La traiter en 404 rangerait le cas nominal dans la
        branche d'exception de chaque client."""
        response = client.get(reverse("v1:geography:zone-resolve"), {"lat": 5.0, "lon": 0.0})

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"is_covered": False, "zone": None}

    def test_la_zone_la_plus_specifique_l_emporte(self, client: APIClient, city: City) -> None:
        """Une zone « Centre » incluse dans un « Grand Lomé » doit gagner :
        c'est la plus petite qui porte le barème juste."""
        grand = DeliveryZone.objects.create(
            city=city,
            name="Grand Lomé",
            boundary=square(1.10, 6.05, 0.30),
            base_fee=Money(1_500, XOF),
            fee_per_km=Money(200, XOF),
        )
        centre = DeliveryZone.objects.create(
            city=city,
            name="Centre",
            boundary=square(1.20, 6.11, 0.06),
            base_fee=Money(500, XOF),
            fee_per_km=Money(100, XOF),
        )

        response = client.get(reverse("v1:geography:zone-resolve"), {"lat": 6.1319, "lon": 1.2255})

        assert response.data["zone"]["id"] == str(centre.pk)
        assert response.data["zone"]["id"] != str(grand.pk)

    def test_une_zone_desactivee_ne_couvre_plus(
        self, client: APIClient, zone: DeliveryZone
    ) -> None:
        DeliveryZone.objects.filter(pk=zone.pk).update(is_active=False)

        response = client.get(reverse("v1:geography:zone-resolve"), {"lat": 6.1319, "lon": 1.2255})

        assert response.data["is_covered"] is False

    @pytest.mark.parametrize(
        "params",
        [{}, {"lat": 6.13}, {"lat": 91, "lon": 1.22}, {"lat": "nord", "lon": 1.22}],
    )
    def test_des_coordonnees_invalides_sont_refusees(
        self, client: APIClient, params: dict[str, object]
    ) -> None:
        response = client.get(reverse("v1:geography:zone-resolve"), params)

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_le_contour_n_est_jamais_expose(self, client: APIClient, zone: DeliveryZone) -> None:
        """Plusieurs kilo-octets de `MultiPolygon` qu'aucun écran n'affiche, et
        que le client n'a pas à connaître : il demande, la base répond."""
        response = client.get(reverse("v1:geography:zone-resolve"), {"lat": 6.1319, "lon": 1.2255})

        assert "boundary" not in response.data["zone"]


class TestFuseauHoraire:
    def test_un_fuseau_inconnu_est_refuse_a_la_saisie(self) -> None:
        """Validé au back-office, où l'on peut corriger — plutôt qu'en 500 sur
        la première liste de restaurants d'un client."""
        from django.core.exceptions import ValidationError

        pays = Country(
            iso_code="ZZ", name="Ailleurs", currency=XOF, phone_prefix="+0", timezone="Africa/Lomé"
        )

        with pytest.raises(ValidationError, match="Fuseau"):
            pays.full_clean()
