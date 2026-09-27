"""Zones circulaires, règle de résolution unique, et livrabilité d'une adresse.

## Le défaut que cette suite garde fermé

La question « quelle zone s'applique à ce point ? » était résolue à **deux
endroits par deux règles différentes** : l'écran qui annonce un tarif retenait
la zone de plus petite surface, la commande qui le facture retenait celle de plus
petit `max_distance_km`. Les deux coïncident tant qu'une ville n'a qu'une zone —
l'état du réseau au moment où le défaut a été trouvé — et divergent dès qu'une
zone est posée à l'intérieur d'une autre.

Le premier test de `TestRegleUnique` construit exactement cette configuration :
une zone « centre » dans une zone « agglomération », avec des barèmes opposés et
des `max_distance_km` choisis pour que les deux anciennes règles se contredisent.
Il échouerait sur l'implémentation précédente, quelle que soit celle des deux
qu'on eût gardée.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.geography.models import City, Country, DeliveryZone, ZoneShape
from apps.geography.resolution import resolve_zone
from apps.geography.shapes import circle_to_boundary, polygon_to_boundary
from apps.restaurants.delivery import check_delivery
from apps.restaurants.models import Restaurant, RestaurantStatus
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
LOME = Point(1.2255, 6.1319, srid=4326)


@pytest.fixture
def sans_zone_municipale(city: City) -> None:
    """Désactive les zones du décor partagé.

    La fixture `zone` de `tests/fixtures.py` pose un carré nommé « Centre »
    autour de Lomé, dont dépend la fixture `restaurant`. Les tests de résolution
    ont besoin de contrôler **exactement** quelles zones concourent : la laisser
    active ferait gagner une zone que le test n'a pas posée, et le résultat ne
    dirait plus rien de la règle qu'on vérifie.

    Désactivée plutôt que supprimée : `Restaurant.zone` est une clé étrangère
    non nulle, et l'effacer emporterait le restaurant du décor.
    """
    DeliveryZone.objects.filter(city=city).update(is_active=False)


def zone_circulaire(
    city: City,
    nom: str,
    centre: Point,
    rayon_m: int,
    *,
    base: int,
    restaurant: Restaurant | None = None,
    priority: int = 0,
    max_km: str = "50",
) -> DeliveryZone:
    """Zone en disque, telle que le back-office la produit."""
    return DeliveryZone.objects.create(
        city=city,
        restaurant=restaurant,
        name=nom,
        shape=ZoneShape.CIRCLE,
        center=centre,
        radius_meters=rayon_m,
        boundary=circle_to_boundary(centre, rayon_m),
        base_fee=Money(base, XOF),
        fee_per_km=Money(0, XOF),
        priority=priority,
        max_distance_km=max_km,
    )


class TestDisqueVersContour:
    """La conversion d'un rayon en contour — la brique du mode « cercle »."""

    def test_les_sommets_sont_a_la_distance_demandee(self) -> None:
        """Chaque sommet est réellement à `radius_meters` du centre.

        L'approximation courante — ajouter `rayon / 111320` aux deux
        coordonnées — produit un disque à l'équateur et une ellipse ailleurs.
        À Lomé l'écart est de 0,5 %, à Paris de 35 % : la même saisie donnerait
        deux zones très différentes selon le marché.
        """
        import math

        def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
            """Distance en mètres entre deux (lon, lat).

            Calculée ici plutôt que par `Point.distance()` : celle-ci est
            **cartésienne** et rend des degrés sur une géométrie non projetée.
            La confondre avec des mètres est le piège que ce test existe pour
            débusquer, et l'y reproduire n'aurait rien vérifié.
            """
            lon1, lat1 = math.radians(a[0]), math.radians(a[1])
            lon2, lat2 = math.radians(b[0]), math.radians(b[1])
            h = (
                math.sin((lat2 - lat1) / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
            )
            return 2 * 6_371_008.8 * math.asin(math.sqrt(h))

        centre = Point(2.3522, 48.8566, srid=4326)  # Paris : la latitude qui piège
        contour = circle_to_boundary(centre, 5000)

        distances = [haversine((x, y), (centre.x, centre.y)) for x, y in contour[0][0].coords[:-1]]
        ecart = max(abs(d - 5000) for d in distances)
        assert ecart < 60, f"écart maximal {ecart:.0f} m — la projection est fausse"

    def test_un_disque_couvre_son_centre_et_pas_le_double_du_rayon(self) -> None:
        contour = circle_to_boundary(LOME, 3000)

        assert contour.covers(LOME)
        # Un point à ~6 km au nord : hors du disque de 3 km.
        assert not contour.covers(Point(LOME.x, LOME.y + 0.054, srid=4326))

    def test_un_rayon_nul_est_refuse(self) -> None:
        with pytest.raises(ValueError, match="strictement positif"):
            circle_to_boundary(LOME, 0)

    def test_un_contour_de_deux_sommets_est_refuse(self) -> None:
        """Deux points font un segment, pas une surface."""
        with pytest.raises(ValueError, match="trois sommets"):
            polygon_to_boundary([[1.0, 6.0], [1.1, 6.0]])

    def test_un_contour_se_ferme_seul(self) -> None:
        """Une carte rend rarement le premier sommet répété.

        L'exiger de chaque client ferait échouer la saisie sur un détail de
        format, alors que l'intention est sans ambiguïté.
        """
        contour = polygon_to_boundary([[1.20, 6.10], [1.25, 6.10], [1.25, 6.15]])

        assert contour.covers(Point(1.24, 6.11, srid=4326))


class TestRegleUnique:
    """Une seule règle décide de la zone — celle qui affiche et celle qui facture."""

    def test_la_zone_la_plus_specifique_gagne(self, city: City, sans_zone_municipale: None) -> None:
        """**Le test qui porte cette suite.**

        « Centre » est incluse dans « Agglomération » et coûte moins cher. Les
        `max_distance_km` sont choisis pour que l'ancienne règle de la commande
        — le plus petit rayon maximal — désigne **l'autre** zone que l'ancienne
        règle de l'écran. Sur l'implémentation précédente, l'une des deux se
        trompait donc nécessairement.
        """
        agglomeration = zone_circulaire(city, "Agglomération", LOME, 20_000, base=2000, max_km="10")
        centre = zone_circulaire(city, "Centre-ville", LOME, 3000, base=500, max_km="40")

        retenue = resolve_zone(LOME)

        assert retenue == centre, "la plus petite surface doit l'emporter"
        assert retenue != agglomeration

    def test_la_priorite_passe_avant_la_surface(
        self, city: City, sans_zone_municipale: None
    ) -> None:
        """Le départage explicite, pour ce que la géométrie ne tranche pas."""
        zone_circulaire(city, "Petite", LOME, 3000, base=500)
        grande = zone_circulaire(city, "Grande prioritaire", LOME, 5000, base=900, priority=10)

        assert resolve_zone(LOME) == grande

    def test_une_zone_d_etablissement_prime_sur_la_zone_municipale(
        self, city: City, restaurant: Restaurant, sans_zone_municipale: None
    ) -> None:
        """Deux cuisines d'une même ville peuvent facturer différemment.

        Sans cette priorité, l'une hériterait du barème de l'autre.
        """
        zone_circulaire(city, "Municipale", LOME, 3000, base=500)
        propre = zone_circulaire(
            city, "Propre à la cuisine", LOME, 8000, base=1200, restaurant=restaurant
        )

        assert resolve_zone(LOME, restaurant_id=restaurant.pk) == propre

    def test_la_zone_d_un_autre_etablissement_ne_s_applique_pas(
        self, city: City, restaurant: Restaurant, sans_zone_municipale: None
    ) -> None:
        """Le client ne paie pas le barème d'une cuisine où il ne commande pas."""
        autre = Restaurant.objects.create(
            name="El Corazón Nord",
            slug="el-corazon-nord",
            zone=restaurant.zone,
            address="Nord",
            location=LOME,
            phone="+22890000002",
            status=RestaurantStatus.ACTIVE,
        )
        municipale = zone_circulaire(city, "Municipale", LOME, 3000, base=500)
        zone_circulaire(city, "Propre à l'autre", LOME, 1000, base=9000, restaurant=autre)

        assert resolve_zone(LOME, restaurant_id=restaurant.pk) == municipale

    def test_sans_etablissement_les_zones_propres_ne_concourent_pas(
        self, city: City, restaurant: Restaurant, sans_zone_municipale: None
    ) -> None:
        zone_circulaire(city, "Propre", LOME, 1000, base=9000, restaurant=restaurant)

        assert resolve_zone(LOME) is None

    def test_un_marche_ferme_retire_ses_zones(
        self, city: City, country: Country, sans_zone_municipale: None
    ) -> None:
        """Fermer un pays doit rendre ses adresses indesservies.

        Sans la cascade, une adresse resterait « livrable » dans un pays où
        l'enseigne n'opère plus.
        """
        zone_circulaire(city, "Centre-ville", LOME, 3000, base=500)
        country.is_active = False
        country.save()

        assert resolve_zone(LOME) is None


class TestLivrabilite:
    """`check_delivery` — la réponse complète, pour les trois applications."""

    def test_elle_rend_etablissement_zone_distance_et_delai(
        self, city: City, restaurant: Restaurant
    ) -> None:
        # Un disque de 2 km, plus petit que le carré du décor : il gagne par
        # spécificité, ce qui est exactement la configuration réelle d'une zone
        # de centre-ville posée dans une zone d'agglomération.
        zone_circulaire(city, "Centre-ville", LOME, 2000, base=500)

        reponse = check_delivery(point=Point(1.2300, 6.1350, srid=4326))

        assert reponse.is_available
        assert reponse.restaurant == restaurant
        assert reponse.zone is not None
        assert reponse.distance_m is not None and reponse.distance_m > 0
        # Préparation **plus** course : c'est ce que le client attend
        # réellement. La zone seule promettait un repas en trente minutes là où
        # la cuisine en demande vingt de plus.
        assert reponse.estimated_minutes == (
            restaurant.default_preparation_minutes + reponse.zone.estimated_delivery_minutes
        )

    def test_elle_chiffre_quand_on_lui_donne_un_panier(
        self, city: City, restaurant: Restaurant
    ) -> None:
        zone_circulaire(city, "Centre-ville", LOME, 2000, base=700)

        reponse = check_delivery(point=Point(1.2300, 6.1350, srid=4326), subtotal=Money(5000, XOF))

        assert reponse.quote is not None
        assert reponse.quote.fee == Money(700, XOF)

    def test_sans_panier_elle_ne_chiffre_pas_mais_repond(
        self, city: City, restaurant: Restaurant
    ) -> None:
        """Savoir si l'on est desservi n'exige pas d'avoir déjà commandé.

        Un montant minimum ne veut rien dire face à un panier vide.
        """
        zone_circulaire(city, "Centre-ville", LOME, 2000, base=700)

        reponse = check_delivery(point=Point(1.2300, 6.1350, srid=4326))

        assert reponse.is_available
        assert reponse.quote is None

    def test_hors_zone_elle_dit_pourquoi(self, city: City, restaurant: Restaurant) -> None:
        zone_circulaire(city, "Centre-ville", LOME, 1000, base=500)

        reponse = check_delivery(point=Point(1.60, 6.60, srid=4326))

        assert not reponse.is_available
        assert reponse.reason
        # Le message doit désigner un geste : ici, changer d'adresse.
        assert "dessert" in reponse.reason or "zone" in reponse.reason

    def test_au_dela_du_rayon_maximal_elle_refuse_avec_la_distance(
        self, city: City, restaurant: Restaurant
    ) -> None:
        """Un contour se dessine large ; la distance parcourue est ce qui coûte.

        Le dire ici évite qu'un écran annonce « desservi » et que la commande
        échoue trois écrans plus loin.
        """
        zone_circulaire(city, "Grand rayon", LOME, 40_000, base=500, max_km="2")

        reponse = check_delivery(point=Point(1.32, 6.22, srid=4326))

        assert not reponse.is_available
        assert reponse.zone is not None, "la zone est connue, c'est la distance qui refuse"
        assert "km" in (reponse.reason or "")

    def test_un_refus_de_panier_conserve_ses_donnees_contextuelles(
        self, city: City, restaurant: Restaurant
    ) -> None:
        """`min_order_amount` doit survivre au relais.

        C'est ce dont l'écran a besoin pour dire **combien il manque** ; réduit
        à sa phrase, le refus ne permettrait plus que « ce n'est pas possible ».
        """
        zone = zone_circulaire(city, "Centre-ville", LOME, 2000, base=500)
        zone.min_order_amount = Money(50_000, XOF)
        zone.save()

        reponse = check_delivery(point=Point(1.2300, 6.1350, srid=4326), subtotal=Money(1000, XOF))

        assert not reponse.is_available
        assert reponse.refusal is not None
        assert "min_order_amount" in reponse.refusal.extra


class TestApiLivrabilite:
    """`POST /restaurants/delivery-check/` — la route que les trois apps appellent."""

    def test_elle_repond_sans_jeton(self, city: City, restaurant: Restaurant) -> None:
        """Un visiteur doit savoir si on le livre avant de créer un compte."""
        zone_circulaire(city, "Centre-ville", LOME, 2000, base=500)

        reponse = APIClient().post(
            reverse("v1:restaurants:delivery-check"),
            {"lat": 6.1350, "lon": 1.2300},
            format="json",
        )

        assert reponse.status_code == 200
        assert reponse.data["is_available"] is True
        assert reponse.data["restaurant"]["slug"] == restaurant.slug
        assert reponse.data["zone"]["name"] == "Centre-ville"

    def test_elle_chiffre_quand_le_panier_est_fourni(
        self, city: City, restaurant: Restaurant
    ) -> None:
        zone_circulaire(city, "Centre-ville", LOME, 2000, base=800)

        reponse = APIClient().post(
            reverse("v1:restaurants:delivery-check"),
            {
                "lat": 6.1350,
                "lon": 1.2300,
                "subtotal": {"amount": "5000", "currency": XOF},
            },
            format="json",
        )

        assert reponse.data["delivery_fee"]["amount"] == "800"
        assert reponse.data["estimated_minutes"] is not None

    def test_un_point_hors_couverture_n_est_pas_une_erreur(self) -> None:
        """C'est une réponse légitime à une question légitime.

        La traiter en 404 obligerait chaque client à ranger « je viens
        d'emménager hors zone » dans sa branche d'exception.
        """
        reponse = APIClient().post(
            reverse("v1:restaurants:delivery-check"),
            {"lat": 48.8566, "lon": 2.3522},
            format="json",
        )

        assert reponse.status_code == 200
        assert reponse.data["is_available"] is False
        assert reponse.data["reason"]

    def test_une_latitude_hors_bornes_est_refusee(self) -> None:
        reponse = APIClient().post(
            reverse("v1:restaurants:delivery-check"),
            {"lat": 120, "lon": 2.0},
            format="json",
        )

        assert reponse.status_code == 400


class TestSaisieDesZones:
    """Les trois modes de saisie, et ce que le serveur refuse."""

    @pytest.fixture
    def as_siege(self) -> APIClient:
        compte = User.objects.create_superuser("siege-zones@elcorazon.test", "motdepasse")
        client = APIClient()
        client.force_authenticate(compte)
        return client

    def _corps(self, city: City, **overrides: object) -> dict[str, object]:
        corps: dict[str, object] = {
            "city": str(city.pk),
            "name": "Zone de test",
            "base_fee": {"amount": "500", "currency": XOF},
            "fee_per_km": {"amount": "100", "currency": XOF},
        }
        corps.update(overrides)
        return corps

    def test_un_cercle_se_saisit_par_centre_et_rayon(self, as_siege: APIClient, city: City) -> None:
        """Le contour est **déduit**, pas envoyé.

        Un back-office qui devrait produire lui-même le polygone d'un disque
        porterait sa propre discrétisation, et deux écrans en auraient deux
        différentes — donc deux zones pour la même saisie.
        """
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(
                city,
                shape="circle",
                center={"lat": 6.1319, "lon": 1.2255},
                radius_meters=5000,
            ),
            format="json",
        )

        assert reponse.status_code == 201, reponse.data
        zone = DeliveryZone.objects.get(pk=reponse.data["id"])
        assert zone.shape == ZoneShape.CIRCLE
        assert zone.radius_meters == 5000
        assert zone.boundary.covers(LOME), "le contour déduit doit couvrir son centre"

    def test_un_cercle_sans_rayon_est_refuse(self, as_siege: APIClient, city: City) -> None:
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(city, shape="circle", center={"lat": 6.13, "lon": 1.22}),
            format="json",
        )

        assert reponse.status_code == 400
        assert "radius_meters" in reponse.data["errors"]

    def test_un_polygone_se_saisit_par_ses_sommets(self, as_siege: APIClient, city: City) -> None:
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(
                city,
                shape="polygon",
                polygon_coordinates=[
                    [1.20, 6.10],
                    [1.26, 6.10],
                    [1.26, 6.16],
                    [1.20, 6.16],
                ],
            ),
            format="json",
        )

        assert reponse.status_code == 201, reponse.data
        zone = DeliveryZone.objects.get(pk=reponse.data["id"])
        assert zone.shape == ZoneShape.POLYGON
        assert zone.center is None and zone.radius_meters is None
        assert zone.boundary.covers(Point(1.23, 6.13, srid=4326))

    def test_un_polygone_de_deux_sommets_est_refuse(self, as_siege: APIClient, city: City) -> None:
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(city, shape="polygon", polygon_coordinates=[[1.20, 6.10], [1.26, 6.10]]),
            format="json",
        )

        assert reponse.status_code == 400

    def test_une_zone_sans_aucun_contour_est_refusee(self, as_siege: APIClient, city: City) -> None:
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"), self._corps(city), format="json"
        )

        assert reponse.status_code == 400

    def test_le_geojson_reste_accepte(self, as_siege: APIClient, city: City) -> None:
        """Le contrat d'origine n'a pas été retiré.

        C'est le bon chemin pour un contour administratif importé d'un outil
        cartographique, et le casser aurait invalidé les appelants existants
        pour n'apporter qu'une uniformité de façade.
        """
        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(
                city,
                shape="administrative",
                boundary={
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[1.20, 6.10], [1.26, 6.10], [1.26, 6.16], [1.20, 6.16], [1.20, 6.10]]]
                    ],
                },
            ),
            format="json",
        )

        assert reponse.status_code == 201, reponse.data

    def test_une_zone_d_etablissement_doit_etre_dans_sa_ville(
        self, as_siege: APIClient, city: City, restaurant: Restaurant, country: Country
    ) -> None:
        """Sinon elle ne couvrirait jamais une adresse que cette cuisine dessert.

        L'erreur ne se verrait qu'à la première commande refusée, sans que rien
        n'en donne la raison.
        """
        ailleurs = City.objects.create(
            country=country, name="Kara", slug="kara", centroid=Point(1.18, 9.55, srid=4326)
        )

        # La route de `restaurants` : c'est elle qui écrit le rattachement, la
        # géographie n'ayant pas le droit de connaître les établissements.
        reponse = as_siege.post(
            reverse("v1:restaurants:managed-restaurant-zone-list"),
            self._corps(
                ailleurs,
                name="Zone mal placée",
                restaurant=restaurant.slug,
                shape="circle",
                center={"lat": 9.55, "lon": 1.18},
                radius_meters=4000,
            ),
            format="json",
        )

        assert reponse.status_code == 400
        assert "restaurant" in reponse.data["errors"]

    def test_le_chevauchement_avertit_sans_refuser(
        self, as_siege: APIClient, city: City, sans_zone_municipale: None
    ) -> None:
        """Une exception tarifaire s'exprime par une zone dans une autre.

        L'écran le signale pour que la décision soit consciente, jamais pour
        empêcher l'écriture.
        """
        zone_circulaire(city, "Agglomération", LOME, 20_000, base=2000)

        reponse = as_siege.post(
            reverse("v1:geography:managed-zone-list"),
            self._corps(
                city,
                name="Centre-ville",
                shape="circle",
                center={"lat": 6.1319, "lon": 1.2255},
                radius_meters=3000,
            ),
            format="json",
        )

        assert reponse.status_code == 201
        relu = as_siege.get(reverse("v1:geography:managed-zone-detail", args=[reponse.data["id"]]))
        assert "Agglomération" in relu.data["overlaps"]

    def test_corriger_un_tarif_n_oblige_pas_a_redessiner(
        self, as_siege: APIClient, city: City, sans_zone_municipale: None
    ) -> None:
        zone = zone_circulaire(city, "Centre-ville", LOME, 5000, base=500)
        contour_avant = zone.boundary.wkt

        reponse = as_siege.patch(
            reverse("v1:geography:managed-zone-detail", args=[zone.pk]),
            {"base_fee": {"amount": "900", "currency": XOF}},
            format="json",
        )

        assert reponse.status_code == 200
        zone.refresh_from_db()
        assert zone.boundary.wkt == contour_avant
        assert zone.base_fee == Money(900, XOF)


class TestZonesDEtablissement:
    """`/restaurants/manage/zones/` — l'écriture que la géographie ne peut pas porter.

    Rendre le rattachement inscriptible depuis `geography` demanderait un import
    `geography → restaurants`, qui fermerait un cycle. Le test d'architecture l'a
    refusé, et l'écriture a donc migré du côté qui a le droit de connaître les
    deux.
    """

    @pytest.fixture
    def as_siege(self) -> APIClient:
        compte = User.objects.create_superuser("siege-propres@elcorazon.test", "motdepasse")
        client = APIClient()
        client.force_authenticate(compte)
        return client

    def _corps(self, city: City, restaurant: Restaurant, **overrides: object) -> dict[str, object]:
        corps: dict[str, object] = {
            "city": str(city.pk),
            "restaurant": restaurant.slug,
            "name": "Livraison express",
            "shape": "circle",
            "center": {"lat": 6.1319, "lon": 1.2255},
            "radius_meters": 4000,
            "base_fee": {"amount": "1200", "currency": XOF},
            "fee_per_km": {"amount": "150", "currency": XOF},
        }
        corps.update(overrides)
        return corps

    def test_une_cuisine_peut_avoir_sa_propre_zone(
        self, as_siege: APIClient, city: City, restaurant: Restaurant
    ) -> None:
        reponse = as_siege.post(
            reverse("v1:restaurants:managed-restaurant-zone-list"),
            self._corps(city, restaurant),
            format="json",
        )

        assert reponse.status_code == 201, reponse.data
        zone = DeliveryZone.objects.get(pk=reponse.data["id"])
        assert zone.restaurant_id == restaurant.pk
        assert zone.boundary.covers(LOME)

    def test_elle_prime_sur_la_zone_municipale(
        self, as_siege: APIClient, city: City, restaurant: Restaurant
    ) -> None:
        """C'est ce qui permet à deux cuisines d'une ville de facturer différemment."""
        as_siege.post(
            reverse("v1:restaurants:managed-restaurant-zone-list"),
            self._corps(city, restaurant),
            format="json",
        )

        retenue = resolve_zone(LOME, restaurant_id=restaurant.pk)

        assert retenue is not None
        assert retenue.restaurant_id == restaurant.pk

    def test_le_barème_reste_valide_dans_la_devise_du_marche(
        self, as_siege: APIClient, city: City, restaurant: Restaurant
    ) -> None:
        """La validation héritée s'applique — elle n'est pas réécrite.

        Un second sérialiseur complet aurait produit deux validations de barème,
        qui auraient divergé au premier correctif.
        """
        reponse = as_siege.post(
            reverse("v1:restaurants:managed-restaurant-zone-list"),
            self._corps(city, restaurant, base_fee={"amount": "12", "currency": "EUR"}),
            format="json",
        )

        assert reponse.status_code == 400
        assert "base_fee" in reponse.data["errors"]

    def test_un_gerant_ne_voit_que_les_zones_de_ses_cuisines(
        self, city: City, restaurant: Restaurant
    ) -> None:
        from apps.accounts.models import Role, UserType
        from apps.restaurants.models import StaffMembership

        autre = Restaurant.objects.create(
            name="El Corazón Ailleurs",
            slug="el-corazon-ailleurs",
            zone=restaurant.zone,
            address="Ailleurs",
            location=LOME,
            phone="+22890000003",
            status=RestaurantStatus.ACTIVE,
        )
        zone_circulaire(city, "Zone d'un autre", LOME, 3000, base=800, restaurant=autre)
        mienne = zone_circulaire(city, "Ma zone", LOME, 3000, base=600, restaurant=restaurant)

        gerant = User.objects.create_user(
            "gerant-zones@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        gerant.roles.add(
            Role.objects.create(
                name="Gérant zones", permissions=["restaurants.read", "restaurants.write"]
            )
        )
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(gerant)

        reponse = client.get(reverse("v1:restaurants:managed-restaurant-zone-list"))

        assert [z["name"] for z in reponse.data["results"]] == [mienne.name]

    def test_un_gerant_n_offre_pas_sa_zone_a_une_autre_cuisine(
        self, city: City, restaurant: Restaurant
    ) -> None:
        """Sinon il exporterait son barème vers une cuisine qu'il n'administre pas."""
        from apps.accounts.models import Role, UserType
        from apps.restaurants.models import StaffMembership

        autre = Restaurant.objects.create(
            name="El Corazón Voisin",
            slug="el-corazon-voisin",
            zone=restaurant.zone,
            address="Voisin",
            location=LOME,
            phone="+22890000004",
            status=RestaurantStatus.ACTIVE,
        )
        mienne = zone_circulaire(city, "Ma zone", LOME, 3000, base=600, restaurant=restaurant)

        gerant = User.objects.create_user(
            "gerant-zones2@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        gerant.roles.add(
            Role.objects.create(
                name="Gérant zones 2", permissions=["restaurants.read", "restaurants.write"]
            )
        )
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(gerant)

        reponse = client.patch(
            reverse("v1:restaurants:managed-restaurant-zone-detail", args=[mienne.pk]),
            {"restaurant": autre.slug},
            format="json",
        )

        assert reponse.status_code == 403
