"""Rattachement de marché et de ville — le palier qui manquait.

Le cloisonnement n'avait que deux étages : le siège, qui voit tout, et le
rattachement à un établissement, qui ne voit que lui. Un directeur pays devait
donc être rattaché à chacun de ses établissements, un par un — et le jour où
l'on en ouvrait un nouveau, il cessait **silencieusement** de le voir.

C'est ce silence que ces tests visent. Le premier d'entre eux ouvre un
restaurant après coup et vérifie qu'il entre dans le périmètre sans qu'on ait
touché au compte : c'est la propriété que la table intermédiaire ne pouvait pas
donner, et la seule raison d'ajouter un modèle.
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import AreaMembership, Restaurant, RestaurantStatus, StaffMembership
from apps.restaurants.scoping import staff_restaurant_ids
from common.money import Money

pytestmark = pytest.mark.django_db

XOF = "XOF"
ABIDJAN = Point(-4.0083, 5.3600, srid=4326)
KARA = Point(1.1861, 9.5511, srid=4326)


def _carre(centre: Point) -> MultiPolygon:
    lon, lat = centre.x, centre.y
    return MultiPolygon(
        Polygon(
            (
                (lon - 0.1, lat - 0.1),
                (lon + 0.1, lat - 0.1),
                (lon + 0.1, lat + 0.1),
                (lon - 0.1, lat + 0.1),
                (lon - 0.1, lat - 0.1),
            ),
            srid=4326,
        ),
        srid=4326,
    )


def _zone_dans(pays: Country, nom_ville: str, centre: Point) -> DeliveryZone:
    ville = City.objects.create(
        country=pays, name=nom_ville, slug=nom_ville.lower(), centroid=centre
    )
    return DeliveryZone.objects.create(
        city=ville,
        name=f"Centre {nom_ville}",
        boundary=_carre(centre),
        base_fee=Money(500, pays.currency),
        fee_per_km=Money(100, pays.currency),
    )


def _etablissement(zone: DeliveryZone, nom: str, centre: Point) -> Restaurant:
    return Restaurant.objects.create(
        name=nom,
        slug=nom.lower().replace(" ", "-"),
        zone=zone,
        address=nom,
        location=centre,
        phone="+22890000000",
        status=RestaurantStatus.ACTIVE,
    )


@pytest.fixture
def cote_d_ivoire() -> Country:
    return Country.objects.create(
        iso_code="CI",
        name="Côte d'Ivoire",
        currency=XOF,
        phone_prefix="+225",
        timezone="Africa/Abidjan",
    )


def _compte(email: str, permissions: list[str]) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name=email, user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=permissions))
    return membre


class TestPerimetreCalcule:
    """Ce que le rattachement de marché ajoute au point de passage unique."""

    def test_un_directeur_pays_voit_tous_les_etablissements_de_son_pays(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        kara = _etablissement(_zone_dans(country, "Kara", KARA), "El Corazón Kara", KARA)
        directeur = _compte("dir-tg@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)

        assert staff_restaurant_ids(directeur) == {restaurant.pk, kara.pk}

    def test_un_etablissement_ouvert_apres_coup_entre_seul_dans_le_perimetre(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        """**La raison d'être de ce modèle.**

        Avec des rattachements un par un, le restaurant ouvert ce matin
        échappait au directeur jusqu'à ce que quelqu'un pense à repasser sur son
        compte. Le défaut ne se signalait pas : il voyait simplement une liste
        plus courte.
        """
        directeur = _compte("dir-tg2@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)
        avant = staff_restaurant_ids(directeur)

        nouveau = _etablissement(_zone_dans(country, "Kara", KARA), "El Corazón Kara", KARA)

        assert nouveau.pk not in avant
        assert nouveau.pk in staff_restaurant_ids(directeur)

    def test_un_responsable_de_ville_ne_voit_que_sa_ville(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        kara = _etablissement(_zone_dans(country, "Kara", KARA), "El Corazón Kara", KARA)
        responsable = _compte("ville@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=responsable, city=restaurant.zone.city)

        perimetre = staff_restaurant_ids(responsable)

        assert restaurant.pk in perimetre
        assert kara.pk not in perimetre

    def test_un_directeur_ne_voit_pas_l_autre_marche(
        self, restaurant: Restaurant, country: Country, cote_d_ivoire: Country
    ) -> None:
        abidjan = _etablissement(
            _zone_dans(cote_d_ivoire, "Abidjan", ABIDJAN), "El Corazón Abidjan", ABIDJAN
        )
        directeur = _compte("dir-tg3@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)

        assert abidjan.pk not in staff_restaurant_ids(directeur)

    def test_les_deux_rattachements_se_cumulent(
        self, restaurant: Restaurant, country: Country, cote_d_ivoire: Country
    ) -> None:
        """Un directeur du Togo qui couvre aussi Abidjan.

        Les axes sont indépendants : l'union est la bonne composition, pas
        l'intersection — un rattachement supplémentaire élargit toujours.
        """
        abidjan = _etablissement(
            _zone_dans(cote_d_ivoire, "Abidjan", ABIDJAN), "El Corazón Abidjan", ABIDJAN
        )
        directeur = _compte("mixte@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)
        AreaMembership.objects.create(user=directeur, city=abidjan.zone.city)

        assert staff_restaurant_ids(directeur) == {restaurant.pk, abidjan.pk}

    def test_un_compte_sans_rattachement_ne_voit_toujours_rien(
        self, restaurant: Restaurant
    ) -> None:
        """Le défaut sûr n'a pas bougé."""
        assert staff_restaurant_ids(_compte("rien@elcorazon.test", [])) == set()


class TestOuvertureDansSonMarche:
    """Ouvrir un établissement : le siège, ou le directeur du marché visé."""

    def _client(self, membre: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(membre)
        return client

    def _corps(self, zone: DeliveryZone, centre: Point, slug: str) -> dict[str, object]:
        return {
            "name": f"Nouveau {slug}",
            "slug": slug,
            "zone": str(zone.pk),
            "address": "Adresse",
            "location": {"lat": centre.y, "lon": centre.x},
            "phone": "+22890000001",
        }

    def test_un_directeur_pays_ouvre_chez_lui(self, country: Country) -> None:
        zone = _zone_dans(country, "Kara", KARA)
        directeur = _compte("ouvre@elcorazon.test", ["restaurants.read", "restaurants.write"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(directeur).post(
            reverse("v1:restaurants:managed-restaurant-list"),
            self._corps(zone, KARA, "el-corazon-kara"),
            format="json",
        )

        assert reponse.status_code == 201

    def test_un_directeur_pays_n_ouvre_pas_ailleurs(
        self, country: Country, cote_d_ivoire: Country
    ) -> None:
        zone = _zone_dans(cote_d_ivoire, "Abidjan", ABIDJAN)
        directeur = _compte("borne@elcorazon.test", ["restaurants.read", "restaurants.write"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(directeur).post(
            reverse("v1:restaurants:managed-restaurant-list"),
            self._corps(zone, ABIDJAN, "el-corazon-abidjan"),
            format="json",
        )

        assert reponse.status_code == 403
        assert not Restaurant.objects.filter(slug="el-corazon-abidjan").exists()

    def test_un_gerant_d_etablissement_n_ouvre_rien(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        """La règle d'origine tient : une création s'attribuerait un périmètre.

        C'est la différence avec le directeur de marché — chez lui,
        l'établissement neuf tombe dans un périmètre qu'on lui avait **déjà**
        donné.
        """
        zone = _zone_dans(country, "Kara", KARA)
        gerant = _compte("gerant@elcorazon.test", ["restaurants.read", "restaurants.write"])
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)

        reponse = self._client(gerant).post(
            reverse("v1:restaurants:managed-restaurant-list"),
            self._corps(zone, KARA, "el-corazon-kara"),
            format="json",
        )

        assert reponse.status_code == 403

    def test_un_directeur_met_en_service_chez_lui(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        """Publier engage l'enseigne, mais reste dans le marché du directeur."""
        restaurant.status = RestaurantStatus.READY
        restaurant.save()
        directeur = _compte("publie@elcorazon.test", ["restaurants.read", "restaurants.write"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(directeur).post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        # 200 si la fiche est complète, 409 si des pièces manquent — les deux
        # disent que la **permission** est passée. C'est ce qu'on vérifie ici ;
        # la complétude a ses propres tests.
        assert reponse.status_code in (200, 409)

    def test_un_gerant_ne_met_pas_en_service(self, restaurant: Restaurant) -> None:
        restaurant.status = RestaurantStatus.READY
        restaurant.save()
        gerant = _compte("nonpublie@elcorazon.test", ["restaurants.read", "restaurants.write"])
        StaffMembership.objects.create(user=gerant, restaurant=restaurant)

        reponse = self._client(gerant).post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        assert reponse.status_code == 403


class TestAttributionDesPerimetres:
    """On n'accorde pas un marché qu'on ne couvre pas soi-même."""

    def _client(self, membre: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(membre)
        return client

    def test_le_siege_nomme_un_directeur_pays(self, country: Country) -> None:
        siege = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")

        reponse = self._client(siege).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "nouveau@elcorazon.test",
                "full_name": "Directeur Togo",
                "password": "motdepasse-solide-42",
                "countries": [country.iso_code],
            },
            format="json",
        )

        assert reponse.status_code == 201
        assert reponse.data["countries"] == [country.iso_code]

    def test_un_directeur_ne_s_octroie_pas_un_autre_marche(
        self, country: Country, cote_d_ivoire: Country
    ) -> None:
        """Sans cette garde, `roles.write` vaudrait un pays entier.

        Le rattachement de périmètre est plus large que celui d'établissement,
        et la garde qui protège le second ne dit rien du premier.
        """
        directeur = _compte("escalade@elcorazon.test", ["roles.read", "roles.write"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(directeur).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "complice@elcorazon.test",
                "full_name": "Complice",
                "password": "motdepasse-solide-42",
                "countries": [cote_d_ivoire.iso_code],
            },
            format="json",
        )

        assert reponse.status_code == 403
        assert not User.objects.filter(email="complice@elcorazon.test").exists()

    def test_un_directeur_pays_nomme_un_responsable_de_ville_chez_lui(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        """Une ville est couverte par elle-même ou par son pays.

        Sinon, un directeur pays devrait passer par le siège pour chacune de ses
        villes.
        """
        directeur = _compte("nomme@elcorazon.test", ["roles.read", "roles.write"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(directeur).post(
            reverse("v1:restaurants:staff-list"),
            {
                "email": "ville-lome@elcorazon.test",
                "full_name": "Responsable Lomé",
                "password": "motdepasse-solide-42",
                "cities": [restaurant.zone.city.slug],
            },
            format="json",
        )

        assert reponse.status_code == 201

    def test_un_champ_absent_ne_retire_pas_les_perimetres(self, country: Country) -> None:
        """Corriger un numéro de téléphone ne doit pas coûter son marché.

        `None` veut dire « ce champ n'était pas dans la requête » ; une liste
        vide veut dire « retire-les tous ». Confondre les deux est le défaut
        classique des mises à jour partielles.
        """
        siege = User.objects.create_superuser("siege2@elcorazon.test", "motdepasse")
        directeur = _compte("stable@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[directeur.pk]),
            {"phone": "+22890999999"},
            format="json",
        )

        assert reponse.status_code == 200
        assert reponse.data["countries"] == [country.iso_code]

    def test_une_liste_vide_retire_bien_les_perimetres(self, country: Country) -> None:
        siege = User.objects.create_superuser("siege3@elcorazon.test", "motdepasse")
        directeur = _compte("revoque@elcorazon.test", ["restaurants.read"])
        AreaMembership.objects.create(user=directeur, country=country)

        reponse = self._client(siege).patch(
            reverse("v1:restaurants:staff-detail", args=[directeur.pk]),
            {"countries": []},
            format="json",
        )

        assert reponse.status_code == 200
        assert reponse.data["countries"] == []
        assert staff_restaurant_ids(directeur) == set()
