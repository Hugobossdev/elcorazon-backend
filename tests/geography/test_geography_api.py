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


class TestReference:
    """`GET /geography/reference/` — les valeurs qu'un pays peut prendre.

    Le formulaire d'ouverture de marché du back-office portait dix fuseaux en
    dur. Ouvrir un onzième marché demandait donc de republier l'application :
    exactement l'opération de développement que le multi-pays existe pour
    supprimer.
    """

    def test_les_devises_proposees_sont_celles_que_le_serveur_accepte(
        self, client: APIClient
    ) -> None:
        """La liste vient de la table qui la fait respecter.

        Une devise proposée à l'écran mais absente de `CURRENCY_EXPONENTS`
        produisait un 400 après la saisie de tout le formulaire, sans dire
        lequel des champs était en cause.
        """
        from common.money import CURRENCY_EXPONENTS

        response = client.get(reverse("v1:geography:reference"))

        assert response.status_code == status.HTTP_200_OK
        proposees = {devise["code"] for devise in response.data["currencies"]}
        assert proposees == set(CURRENCY_EXPONENTS)

    def test_l_exposant_voyage_avec_la_devise(self, client: APIClient) -> None:
        """Il décide de la saisie : francs entiers en XOF, centimes en EUR.

        Sans lui, l'écran devrait redéployer la table de son côté — donc la
        dupliquer, et la laisser diverger.
        """
        response = client.get(reverse("v1:geography:reference"))

        exposants = {d["code"]: d["exponent"] for d in response.data["currencies"]}
        assert exposants["XOF"] == 0
        assert exposants["EUR"] == 2

    def test_les_fuseaux_couvrent_plus_que_la_liste_ecrite_en_dur(self, client: APIClient) -> None:
        """Le plafond des dix fuseaux est levé, pas déplacé.

        `Africa/Lome` et `Africa/Abidjan` y étaient déjà ; `America/New_York`
        n'y était pas, et c'est ce que ce test garde — la liste n'est plus une
        sélection maison.
        """
        response = client.get(reverse("v1:geography:reference"))

        fuseaux = set(response.data["timezones"])
        assert {"Africa/Lome", "Africa/Abidjan", "Africa/Douala"} <= fuseaux
        assert "America/New_York" in fuseaux

    def test_un_fuseau_propose_est_accepte_par_le_modele(self, client: APIClient) -> None:
        """Ce que l'écran propose, le serveur doit l'accepter.

        C'est la propriété qui rend la route utile : sinon elle ne fait que
        déplacer la divergence d'un fichier à l'autre.
        """
        from apps.geography.models import validate_timezone

        response = client.get(reverse("v1:geography:reference"))

        for fuseau in response.data["timezones"][:50]:
            validate_timezone(fuseau)

    def test_la_reference_est_lisible_sans_compte(self, client: APIClient) -> None:
        """Comme le reste de la géographie : ce sont des constantes publiques."""
        assert client.get(reverse("v1:geography:reference")).status_code == status.HTTP_200_OK
