"""Ouvrir un établissement depuis le back-office — ADR-005, ADR-006, ADR-010.

Ce module garde la promesse qui justifiait toute la hiérarchie de l'ADR-006 :
**ajouter un restaurant ne demande aucune modification de code.** Il vérifie
donc le chemin complet — pays, ville, zone, établissement, publication — par
l'API, celle-là même qu'appelle le back-office.

Trois propriétés y sont tenues, et chacune correspond à un défaut qui existait :

* **un établissement neuf n'est pas public.** `is_active` valait `True` par
  défaut : une fiche créée apparaissait immédiatement dans l'application
  cliente, sans carte, sans horaires, sans livreur ;
* **la publication est gardée par la complétude.** Le serveur refuse la mise en
  service tant qu'il manque quelque chose, et **dit quoi** — la liste voyage
  dans le corps d'erreur, pas dans un message générique ;
* **la géographie cascade.** Fermer un pays retirait ses villes de l'API et
  laissait ses restaurants : l'application cliente affichait un établissement
  d'un marché fermé.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem
from apps.delivery.models import CourierProfile
from apps.delivery.states import VerificationStatus
from apps.geography.models import City, Country, DeliveryZone
from apps.restaurants.models import (
    IncompleteConfiguration,
    OpeningHours,
    Restaurant,
    RestaurantStatus,
    StaffMembership,
    Weekday,
)
from common.money import Money
from common.state_machine import IllegalTransition

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"

#: Contour carré autour d'Abidjan, en GeoJSON.
CARRE_ABIDJAN: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [-4.10, 5.28],
            [-3.90, 5.28],
            [-3.90, 5.45],
            [-4.10, 5.45],
            [-4.10, 5.28],
        ]
    ],
}

#: Point au cœur du carré ci-dessus.
ABIDJAN = Point(-4.0083, 5.3600, srid=4326)


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def siege() -> APIClient:
    return connecte(User.objects.create_superuser("siege@elcorazon.test", "motdepasse"))


@pytest.fixture
def gerant(restaurant: Restaurant) -> APIClient:
    """Gérant de l'établissement de Lomé — cloisonné, avec les droits d'écriture."""
    membre = User.objects.create_user(
        "gerant@elcorazon.test", "motdepasse", full_name="Gérante", user_type=UserType.STAFF
    )
    membre.roles.add(
        Role.objects.create(name="Gérant", permissions=["restaurants.read", "restaurants.write"])
    )
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return connecte(membre)


def completer(etablissement: Restaurant) -> None:
    """Donne à un établissement tout ce que la mise en service exige.

    Rassemblé ici parce que la liste est **le contrat** de
    `configuration_gaps` : horaires, catégorie active, article disponible,
    personnel rattaché, livreur approuvé. Un test qui n'en poserait que trois
    vérifierait la garde par accident.
    """
    OpeningHours.objects.create(
        restaurant=etablissement, weekday=Weekday.MONDAY, opens_at="11:00", closes_at="23:00"
    )
    categorie = Category.objects.create(
        restaurant=etablissement, name="Plats", slug="plats", is_active=True
    )
    MenuItem.objects.create(
        restaurant=etablissement,
        category=categorie,
        name="Poulet braisé",
        slug="poulet-braise",
        price=Money(3_200, XOF),
        is_available=True,
    )
    employe = User.objects.create_user(
        f"employe-{etablissement.slug}@elcorazon.test",
        "motdepasse",
        full_name="Employé",
        user_type=UserType.STAFF,
    )
    StaffMembership.objects.create(user=employe, restaurant=etablissement)

    livreur = User.objects.create_user(
        f"livreur-{etablissement.slug}@elcorazon.test",
        "motdepasse",
        full_name="Livreur",
        user_type=UserType.COURIER,
    )
    CourierProfile.objects.create(
        user=livreur,
        restaurant=etablissement,
        vehicle_type="motorcycle",
        verification_status=VerificationStatus.APPROVED,
    )


# ------------------------------------------------------- le parcours complet


class TestOuvertureDUnMarche:
    """Pays → ville → zone → établissement, par l'API et sans toucher au code."""

    def test_le_siege_ouvre_un_marche_de_bout_en_bout(self, siege: APIClient) -> None:
        pays = siege.post(
            reverse("v1:geography:managed-country-list"),
            {
                "iso_code": "CI",
                "name": "Côte d'Ivoire",
                "currency": XOF,
                "phone_prefix": "+225",
                "timezone": "Africa/Abidjan",
            },
            format="json",
        )
        assert pays.status_code == status.HTTP_201_CREATED, pays.data

        ville = siege.post(
            reverse("v1:geography:managed-city-list"),
            {
                "country": "CI",
                "name": "Abidjan",
                "slug": "abidjan",
                "centroid": {"lat": 5.3600, "lon": -4.0083},
            },
            format="json",
        )
        assert ville.status_code == status.HTTP_201_CREATED, ville.data

        zone = siege.post(
            reverse("v1:geography:managed-zone-list"),
            {
                "city": ville.data["id"],
                "name": "Abidjan — Plateau",
                "boundary": CARRE_ABIDJAN,
                "base_fee": {"amount": "800", "currency": XOF},
                "fee_per_km": {"amount": "150", "currency": XOF},
            },
            format="json",
        )
        assert zone.status_code == status.HTTP_201_CREATED, zone.data

        etablissement = siege.post(
            reverse("v1:restaurants:managed-restaurant-list"),
            {
                "name": "El Corazón Abidjan",
                "slug": "el-corazon-abidjan",
                "zone": zone.data["id"],
                "address": "Rue des Jardins, Cocody",
                "location": {"lat": 5.3610, "lon": -4.0070},
                "phone": "+22507000000",
            },
            format="json",
        )
        assert etablissement.status_code == status.HTTP_201_CREATED, etablissement.data

        # La devise et le fuseau descendent du pays sans avoir été saisis : ce
        # sont des propriétés du marché, jamais de l'établissement.
        assert etablissement.data["currency"] == XOF
        assert etablissement.data["timezone"] == "Africa/Abidjan"
        assert etablissement.data["city"] == "Abidjan"
        assert etablissement.data["country"] == "CI"

    def test_un_etablissement_neuf_n_est_pas_public(
        self, siege: APIClient, zone: DeliveryZone
    ) -> None:
        """`is_active` valait `True` par défaut : une fiche à peine créée
        apparaissait dans l'application cliente, vide."""
        reponse = siege.post(
            reverse("v1:restaurants:managed-restaurant-list"),
            {
                "name": "El Corazón Kara",
                "slug": "el-corazon-kara",
                "zone": str(zone.pk),
                "address": "Kara",
                "location": {"lat": 6.1319, "lon": 1.2255},
                "phone": "+22890000001",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_201_CREATED
        assert reponse.data["status"] == RestaurantStatus.DRAFT
        assert reponse.data["is_active"] is False

        publique = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert "el-corazon-kara" not in [r["slug"] for r in publique.data["results"]]

    def test_l_ouverture_releve_du_siege(self, gerant: APIClient, zone: DeliveryZone) -> None:
        """Un gérant modifie le sien mais n'en crée pas : une création
        s'attribuerait un périmètre qu'on ne lui a pas donné."""
        reponse = gerant.post(
            reverse("v1:restaurants:managed-restaurant-list"),
            {
                "name": "El Corazón Kara",
                "slug": "el-corazon-kara",
                "zone": str(zone.pk),
                "address": "Kara",
                "location": {"lat": 6.1319, "lon": 1.2255},
                "phone": "+22890000001",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------- publication


class TestMiseEnService:
    def test_la_publication_est_refusee_tant_qu_il_manque_quelque_chose(
        self, siege: APIClient, restaurant: Restaurant
    ) -> None:
        """Et le refus **dit quoi** : « opération impossible » n'apprendrait
        rien à qui vient de remplir un formulaire."""
        restaurant.status = RestaurantStatus.READY
        restaurant.save(update_fields=["status"])

        reponse = siege.post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["code"] == "incomplete_configuration"
        assert reponse.data["missing"], "la liste des manques doit voyager avec l'erreur"

        restaurant.refresh_from_db()
        assert restaurant.status == RestaurantStatus.READY
        assert restaurant.is_active is False

    def test_un_etablissement_complet_se_publie(
        self, siege: APIClient, restaurant: Restaurant
    ) -> None:
        completer(restaurant)
        restaurant.status = RestaurantStatus.READY
        restaurant.save(update_fields=["status"])

        reponse = siege.post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["status"] == RestaurantStatus.ACTIVE
        assert reponse.data["configuration_gaps"] == []

        restaurant.refresh_from_db()
        assert restaurant.is_active is True

        publique = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert restaurant.slug in [r["slug"] for r in publique.data["results"]]

    def test_la_machine_refuse_un_enchainement_illegal(
        self, siege: APIClient, restaurant: Restaurant
    ) -> None:
        """On ne publie pas depuis le brouillon : il faut être passé par la
        configuration, c'est-à-dire avoir regardé la fiche."""
        restaurant.status = RestaurantStatus.DRAFT
        restaurant.save(update_fields=["status"])

        reponse = siege.post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["code"] == "illegal_transition"
        # La réponse dit ce qui était possible : le client peut afficher les
        # bons boutons plutôt que de faire deviner.
        assert RestaurantStatus.CONFIGURING in reponse.data["allowed_transitions"]

    def test_la_mise_en_service_releve_du_siege(
        self, gerant: APIClient, restaurant: Restaurant
    ) -> None:
        """Un gérant suspend et rouvre la configuration du sien ; ouvrir au
        public engage l'enseigne."""
        completer(restaurant)
        restaurant.status = RestaurantStatus.READY
        restaurant.save(update_fields=["status"])

        reponse = gerant.post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.ACTIVE},
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_la_suspension_retire_de_l_application_sans_rien_supprimer(
        self, siege: APIClient, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        siege.post(
            reverse("v1:restaurants:managed-restaurant-status", args=[restaurant.slug]),
            {"status": RestaurantStatus.INACTIVE},
            format="json",
        )

        publique = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert publique.data["count"] == 0

        # Rien n'a disparu : la carte est intacte, et l'établissement reste
        # visible du siège — c'est de là qu'on le rouvre.
        assert MenuItem.objects.filter(pk=menu_item.pk).exists()
        interne = siege.get(reverse("v1:restaurants:managed-restaurant-list"))
        assert restaurant.slug in [r["slug"] for r in interne.data["results"]]


# ------------------------------------------------------ dérivation et cascade


class TestDerivationDuStatut:
    def test_is_active_suit_le_statut_sans_pouvoir_en_diverger(
        self, restaurant: Restaurant
    ) -> None:
        """Une seule colonne d'état s'écrit ; l'autre en découle.

        Écrire `is_active` à la main est sans effet durable, et c'est
        exactement ce qu'on veut : deux leviers de publication finiraient par se
        contredire.
        """
        restaurant.is_active = False
        restaurant.save(update_fields=["is_active"])
        restaurant.refresh_from_db()

        assert restaurant.status == RestaurantStatus.ACTIVE
        assert restaurant.is_active is True

    def test_une_transition_par_update_fields_emporte_le_booleen(
        self, restaurant: Restaurant
    ) -> None:
        """`save(update_fields=["status"])` est la forme qu'écrit naturellement
        une transition. Sans le report, la ligne resterait publiée."""
        restaurant.status = RestaurantStatus.INACTIVE
        restaurant.save(update_fields=["status"])
        restaurant.refresh_from_db()

        assert restaurant.is_active is False

    def test_is_active_n_est_pas_inscriptible_par_l_api(
        self, siege: APIClient, restaurant: Restaurant
    ) -> None:
        reponse = siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"is_active": False},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        restaurant.refresh_from_db()
        assert restaurant.is_active is True


class TestCascadeGeographique:
    """Fermer un marché retire ce qu'il contient — jusqu'aux établissements.

    `CityViewSet` cascadait déjà, `RestaurantViewSet` non : l'application
    cliente affichait un restaurant d'un pays fermé, dont la ville n'existait
    plus pour elle. La fiche s'ouvrait, la commande échouait plus loin, et rien
    n'expliquait pourquoi.
    """

    @pytest.fixture
    def publie(self, restaurant: Restaurant) -> Restaurant:
        return restaurant

    def test_fermer_le_pays_retire_ses_etablissements(
        self, publie: Restaurant, country: Country
    ) -> None:
        country.is_active = False
        country.save(update_fields=["is_active"])

        reponse = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert reponse.data["count"] == 0

    def test_fermer_la_ville_retire_ses_etablissements(
        self, publie: Restaurant, city: City
    ) -> None:
        city.is_active = False
        city.save(update_fields=["is_active"])

        reponse = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert reponse.data["count"] == 0

    def test_fermer_la_zone_retire_ses_etablissements(
        self, publie: Restaurant, zone: DeliveryZone
    ) -> None:
        zone.is_active = False
        zone.save(update_fields=["is_active"])

        reponse = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert reponse.data["count"] == 0

    def test_rouvrir_le_pays_les_ramene(self, publie: Restaurant, country: Country) -> None:
        """Rien n'a été supprimé : la fermeture est réversible."""
        country.is_active = False
        country.save(update_fields=["is_active"])
        country.is_active = True
        country.save(update_fields=["is_active"])

        reponse = APIClient().get(reverse("v1:restaurants:restaurant-list"))
        assert publie.slug in [r["slug"] for r in reponse.data["results"]]


# ------------------------------------------------------------- la complétude


class TestCompletude:
    def test_la_position_hors_zone_est_signalee(self, restaurant: Restaurant) -> None:
        """La seule incohérence géographique que le schéma ne rend pas
        impossible : une ville d'un autre pays ne peut pas être choisie — `City`
        porte une clé vers `Country` — mais un point posé ailleurs, si."""
        restaurant.location = ABIDJAN
        restaurant.save(update_fields=["location"])

        manques = restaurant.configuration_gaps()
        assert any("hors de sa zone" in manque for manque in manques)

    def test_le_catalogue_et_la_flotte_s_annoncent_par_le_registre(
        self, restaurant: Restaurant
    ) -> None:
        """`restaurants` ne connaît ni `catalog` ni `delivery` (ADR-002) : ces
        deux exigences arrivent par abonnement, posé au `ready()` de chaque
        application. Ce test vérifie que l'abonnement est bien branché — sans
        lui, un établissement s'ouvrirait avec une carte vide."""
        manques = restaurant.configuration_gaps()

        assert any("catégorie active" in manque for manque in manques)
        assert any("article disponible" in manque for manque in manques)
        assert any("livreur approuvé" in manque for manque in manques)

    def test_un_etablissement_complet_n_a_plus_de_manque(self, restaurant: Restaurant) -> None:
        completer(restaurant)
        assert restaurant.configuration_gaps() == []

    def test_transition_to_refuse_la_publication_incomplete(self, restaurant: Restaurant) -> None:
        restaurant.status = RestaurantStatus.READY
        restaurant.save(update_fields=["status"])

        with pytest.raises(IncompleteConfiguration) as refus:
            restaurant.transition_to(RestaurantStatus.ACTIVE)

        assert refus.value.manques
        assert refus.value.code == "incomplete_configuration"

    def test_transition_to_refuse_un_enchainement_illegal(self, restaurant: Restaurant) -> None:
        with pytest.raises(IllegalTransition):
            # « en service » → « prêt » n'existe pas : on suspend d'abord.
            restaurant.transition_to(RestaurantStatus.READY)


# ---------------------------------------------------------------- isolation


class TestIsolationEntreEtablissements:
    """Deux restaurants, deux cartes, deux barèmes — et rien qui déborde."""

    @pytest.fixture
    def abidjan(self, country: Country) -> Restaurant:
        ivoire = Country.objects.create(
            iso_code="CI",
            name="Côte d'Ivoire",
            currency=XOF,
            phone_prefix="+225",
            timezone="Africa/Abidjan",
        )
        ville = City.objects.create(
            country=ivoire, name="Abidjan", slug="abidjan", centroid=ABIDJAN
        )
        from django.contrib.gis.geos import MultiPolygon, Polygon

        contour = MultiPolygon(
            Polygon(
                (
                    (-4.10, 5.28),
                    (-3.90, 5.28),
                    (-3.90, 5.45),
                    (-4.10, 5.45),
                    (-4.10, 5.28),
                ),
                srid=4326,
            ),
            srid=4326,
        )
        zone = DeliveryZone.objects.create(
            city=ville,
            name="Plateau",
            boundary=contour,
            # Barème **différent** : un frais qui sortirait du mauvais barème se
            # verrait au franc près.
            base_fee=Money(800, XOF),
            fee_per_km=Money(150, XOF),
        )
        return Restaurant.objects.create(
            name="El Corazón Abidjan",
            slug="el-corazon-abidjan",
            zone=zone,
            address="Cocody",
            location=ABIDJAN,
            phone="+22507000000",
            status=RestaurantStatus.ACTIVE,
        )

    def test_deux_etablissements_portent_deux_prix_pour_le_meme_article(
        self, restaurant: Restaurant, abidjan: Restaurant, category: Category
    ) -> None:
        """Même slug, deux restaurants, deux prix. La contrainte d'unicité du
        catalogue est **par établissement** — c'est ce qui rend la carte
        propre à chacun."""
        MenuItem.objects.create(
            restaurant=restaurant,
            category=category,
            name="Poulet braisé",
            slug="poulet-braise",
            price=Money(2_500, XOF),
        )
        categorie_abidjan = Category.objects.create(restaurant=abidjan, name="Plats", slug="plats")
        MenuItem.objects.create(
            restaurant=abidjan,
            category=categorie_abidjan,
            name="Poulet braisé",
            slug="poulet-braise",
            price=Money(3_200, XOF),
        )

        client = APIClient()
        a_lome = client.get(reverse("v1:catalog:item-list"), {"restaurant__slug": restaurant.slug})
        a_abidjan = client.get(reverse("v1:catalog:item-list"), {"restaurant__slug": abidjan.slug})

        assert [a["price"]["amount"] for a in a_lome.data["results"]] == ["2500"]
        assert [a["price"]["amount"] for a in a_abidjan.data["results"]] == ["3200"]

    def test_la_devise_et_le_fuseau_descendent_du_pays(
        self, restaurant: Restaurant, abidjan: Restaurant
    ) -> None:
        assert restaurant.timezone == "Africa/Lome"
        assert abidjan.timezone == "Africa/Abidjan"
        # Même devise ici — les deux marchés sont en zone UEMOA — mais elle est
        # **héritée**, pas recopiée : c'est ce que vérifie la propriété.
        assert abidjan.currency == abidjan.zone.city.country.currency

    def test_les_frais_de_livraison_suivent_la_zone_de_l_etablissement(
        self, restaurant: Restaurant, abidjan: Restaurant
    ) -> None:
        """Un barème unique en constante — ce qu'avait l'implémentation
        précédente — donnerait le même frais aux deux."""
        client = APIClient()
        liste = client.get(reverse("v1:restaurants:restaurant-list"))
        frais = {r["slug"]: r["delivery_fee_from"]["amount"] for r in liste.data["results"]}

        assert frais[restaurant.slug] == "500"
        assert frais[abidjan.slug] == "800"

    def test_un_livreur_est_rattache_a_un_seul_etablissement(
        self, restaurant: Restaurant, abidjan: Restaurant
    ) -> None:
        livreur = User.objects.create_user(
            "livreur-abidjan@elcorazon.test",
            "motdepasse",
            full_name="Livreur",
            user_type=UserType.COURIER,
        )
        profil = CourierProfile.objects.create(
            user=livreur,
            restaurant=abidjan,
            vehicle_type="motorcycle",
            verification_status=VerificationStatus.APPROVED,
        )

        assert profil.restaurant_id == abidjan.pk
        assert not CourierProfile.objects.filter(restaurant=restaurant).exists()
