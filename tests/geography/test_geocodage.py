"""Géocodage inverse — les composants d'une position, servis par le serveur.

## Ce qui n'est jamais appelé ici

Google. Une suite de tests qui interroge un tiers facturé est lente, coûteuse,
et rouge le jour où le réseau tombe — trois raisons de ne jamais voir personne
la lancer. La réponse du fournisseur est donc simulée, et ce qu'on vérifie est
**notre** part : l'extraction des composants, le cache, et la façon dont les
pannes sont rendues.

## Le défaut que la première classe garde fermé

L'implémentation Flutter précédente ne lisait que `formatted_address` et
devinait la ville en y cherchant son nom par sous-chaîne. « Rue de Lomé,
Cotonou » y trouvait Lomé. On lit désormais `address_components`, c'est-à-dire
ce que Google a réellement **classé**, et le premier test le vérifie sur ce cas
précis.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.geocoding import (
    GeocodingUnavailable,
    reverse_geocode,
)
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = pytest.mark.django_db


def reponse_google(**overrides: Any) -> dict[str, Any]:
    """Une réponse de l'API Geocoding, dans sa forme réelle.

    Le piège qu'elle contient est délibéré : `formatted_address` mentionne
    « Rue de Lomé » alors que la ville est Cotonou. Une extraction par
    sous-chaîne s'y trompe ; une extraction par composants, non.
    """
    charge = {
        "status": "OK",
        "results": [
            {
                "formatted_address": "Rue de Lomé, Cotonou, Bénin",
                "place_id": "ChIJexemple",
                "address_components": [
                    {"long_name": "12", "short_name": "12", "types": ["street_number"]},
                    {"long_name": "Rue de Lomé", "short_name": "Rue de Lomé", "types": ["route"]},
                    {
                        "long_name": "Ganhi",
                        "short_name": "Ganhi",
                        "types": ["sublocality_level_1", "sublocality"],
                    },
                    {"long_name": "Cotonou", "short_name": "Cotonou", "types": ["locality"]},
                    {
                        "long_name": "Littoral",
                        "short_name": "Littoral",
                        "types": ["administrative_area_level_1"],
                    },
                    {"long_name": "Bénin", "short_name": "BJ", "types": ["country"]},
                ],
            }
        ],
    }
    charge.update(overrides)
    return charge


class FausseReponse:
    """Ce que rend `urlopen` : un objet-contexte dont on lit des octets."""

    def __init__(self, charge: dict[str, Any]) -> None:
        self._octets = json.dumps(charge).encode()

    def __enter__(self) -> FausseReponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._octets


@pytest.fixture(autouse=True)
def cache_vide() -> None:
    """Le cache est partagé entre les tests — il faut le vider.

    Sans cela, le premier test qui géocode une position ferait passer les
    suivants sans qu'ils touchent au code qu'ils prétendent vérifier.
    """
    cache.clear()


@pytest.fixture
def avec_cle(settings: Any) -> None:
    settings.GOOGLE_MAPS_API_KEY = "cle-de-test"


class TestExtractionDesComposants:
    def test_la_ville_vient_des_composants_et_non_du_texte(self, avec_cle: None) -> None:
        """« Rue de Lomé, Cotonou » est à Cotonou.

        L'extraction par sous-chaîne y trouvait Lomé — le défaut exact de
        l'implémentation précédente.
        """
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(reponse_google()),
        ):
            resultat = reverse_geocode(latitude=6.37, longitude=2.42)

        assert resultat.city == "Cotonou"
        assert resultat.country == "Bénin"
        assert resultat.country_code == "BJ"

    def test_le_quartier_et_la_region_sont_rendus(self, avec_cle: None) -> None:
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(reponse_google()),
        ):
            resultat = reverse_geocode(latitude=6.37, longitude=2.42)

        assert resultat.district == "Ganhi"
        assert resultat.region == "Littoral"
        assert resultat.street == "Rue de Lomé"
        assert resultat.street_number == "12"

    def test_une_ville_classee_en_niveau_2_est_trouvee(self, avec_cle: None) -> None:
        """Google classe une agglomération tantôt en `locality`, tantôt en
        `administrative_area_level_2` — et les villes africaines tombent
        souvent dans la seconde.

        Ne lire que `locality` rendrait donc une ville nulle sur une bonne part
        du marché visé.
        """
        charge = reponse_google()
        charge["results"][0]["address_components"] = [
            {"long_name": "Kara", "short_name": "Kara", "types": ["administrative_area_level_2"]},
            {"long_name": "Togo", "short_name": "TG", "types": ["country"]},
        ]

        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(charge),
        ):
            resultat = reverse_geocode(latitude=9.55, longitude=1.18)

        assert resultat.city == "Kara"

    def test_une_position_sans_adresse_n_est_pas_une_erreur(self, avec_cle: None) -> None:
        """Au large, Google ne rend rien. La position reste valide.

        L'administrateur peut vouloir poser un point de retrait qu'aucun service
        d'adressage ne nomme ; en faire une erreur le lui interdirait.
        """
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse({"status": "ZERO_RESULTS", "results": []}),
        ):
            resultat = reverse_geocode(latitude=0.0, longitude=0.0)

        assert resultat.latitude == 0.0
        assert resultat.city is None
        assert resultat.country is None


class TestCache:
    def test_deux_appels_sur_le_meme_point_n_interrogent_google_qu_une_fois(
        self, avec_cle: None
    ) -> None:
        """Le back-office repasse sur les mêmes points à chaque ajustement.

        Sans cache, chaque pixel de déplacement d'un marqueur serait facturé.
        """
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(reponse_google()),
        ) as appel:
            reverse_geocode(latitude=6.37, longitude=2.42)
            reverse_geocode(latitude=6.37, longitude=2.42)

        assert appel.call_count == 1

    def test_deux_points_voisins_au_metre_pres_partagent_leur_reponse(self, avec_cle: None) -> None:
        """La clé arrondit à cinq décimales — environ un mètre.

        Deux relevés du même endroit ne diffèrent que par le bruit du capteur.
        """
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(reponse_google()),
        ) as appel:
            reverse_geocode(latitude=6.370001, longitude=2.420001)
            reverse_geocode(latitude=6.370002, longitude=2.420002)

        assert appel.call_count == 1


class TestPannes:
    def test_sans_cle_le_service_se_declare_indisponible(self, settings: Any) -> None:
        settings.GOOGLE_MAPS_API_KEY = ""

        with pytest.raises(GeocodingUnavailable, match="GOOGLE_MAPS_API_KEY"):
            reverse_geocode(latitude=6.37, longitude=2.42)

    def test_un_refus_du_fournisseur_nomme_la_cause(self, avec_cle: None) -> None:
        """`REQUEST_DENIED` est une panne de configuration, pas une réponse.

        Le message de Google est repris tel quel : il nomme la cause, et c'est
        l'exploitant qui le lira.
        """
        charge = {
            "status": "REQUEST_DENIED",
            "error_message": "The provided API key is invalid.",
            "results": [],
        }

        with (
            patch(
                "apps.geography.geocoding.urllib.request.urlopen",
                return_value=FausseReponse(charge),
            ),
            pytest.raises(GeocodingUnavailable, match="REQUEST_DENIED"),
        ):
            reverse_geocode(latitude=6.37, longitude=2.42)

    def test_un_reseau_muet_ne_bloque_pas_indefiniment(self, avec_cle: None) -> None:
        """L'appel est borné : sans délai, l'écran attendrait sans fin."""
        with (
            patch(
                "apps.geography.geocoding.urllib.request.urlopen",
                side_effect=TimeoutError("trop long"),
            ),
            pytest.raises(GeocodingUnavailable, match="n'a pas répondu"),
        ):
            reverse_geocode(latitude=6.37, longitude=2.42)


class TestApi:
    @pytest.fixture
    def as_operateur(self, restaurant: Restaurant) -> APIClient:
        membre = User.objects.create_user(
            "geo@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        membre.roles.add(
            Role.objects.create(
                name="Géographie", permissions=["restaurants.read", "restaurants.write"]
            )
        )
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(membre)
        return client

    def test_la_route_rend_les_composants(self, as_operateur: APIClient, avec_cle: None) -> None:
        with patch(
            "apps.geography.geocoding.urllib.request.urlopen",
            return_value=FausseReponse(reponse_google()),
        ):
            reponse = as_operateur.post(
                reverse("v1:geography:geocode-reverse"),
                {"lat": 6.37, "lon": 2.42},
                format="json",
            )

        assert reponse.status_code == 200
        assert reponse.data["city"] == "Cotonou"
        assert reponse.data["country_code"] == "BJ"

    def test_elle_exige_un_jeton(self, avec_cle: None) -> None:
        """Elle consomme un quota facturé chez un tiers.

        Ouverte, elle serait un proxy Google gratuit pour n'importe qui, et la
        facture arriverait sans qu'aucun écran du produit n'ait servi.
        """
        reponse = APIClient().post(
            reverse("v1:geography:geocode-reverse"), {"lat": 6.37, "lon": 2.42}, format="json"
        )

        assert reponse.status_code == 401

    def test_un_client_ne_peut_pas_l_appeler(self, customer: User, avec_cle: None) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        reponse = client.post(
            reverse("v1:geography:geocode-reverse"), {"lat": 6.37, "lon": 2.42}, format="json"
        )

        assert reponse.status_code == 403

    def test_sans_cle_la_route_repond_503_et_non_500(
        self, as_operateur: APIClient, settings: Any
    ) -> None:
        """Le service du projet va bien ; c'est sa dépendance qui manque.

        La distinction change ce que fait l'exploitant : vérifier une clé,
        plutôt que lire une trace d'exception.
        """
        settings.GOOGLE_MAPS_API_KEY = ""

        reponse = as_operateur.post(
            reverse("v1:geography:geocode-reverse"), {"lat": 6.37, "lon": 2.42}, format="json"
        )

        assert reponse.status_code == 503
        assert reponse.data["code"] == "geocoding_unavailable"

    def test_une_latitude_hors_bornes_est_refusee(
        self, as_operateur: APIClient, avec_cle: None
    ) -> None:
        reponse = as_operateur.post(
            reverse("v1:geography:geocode-reverse"), {"lat": 200, "lon": 2.42}, format="json"
        )

        assert reponse.status_code == 400
