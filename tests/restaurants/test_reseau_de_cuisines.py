"""Le réseau de cuisines reste cohérent — pays, ville, zone, cuisine, commande.

Quatre défauts trouvés à l'audit du 2026-09-14, et chacun a son test ici :

* **une zone municipale de la ville voisine tarifait une cuisine désignée.**
  `resolve_zone` n'était pas bornée à la ville de la cuisine : avec un panier
  déjà ouvert, une adresse de la ville d'à côté était « desservie », au barème
  de l'autre ville, alors que le choix automatique la refusait ;
* **une cuisine pouvait se poser sur la zone propre d'une autre**, et se
  publier sur un marché fermé ;
* **la commande ne retenait ni sa zone, ni sa ville, ni son pays** : déplacer
  une cuisine réécrivait l'histoire des rapports, et « combien de commandes à
  Cocody ? » n'avait pas de réponse ;
* **aucune fermeture datée n'existait** : fermer un jour férié obligeait à
  retirer une plage d'horaires — donc tous les mardis — et le client lisait
  « fermé » sans savoir jusqu'à quand.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.carts.services import CartService
from apps.catalog.models import MenuItem
from apps.geography.models import City, Country, DeliveryZone
from apps.orders.models import Order, PaymentMethod
from apps.profiles.models import Address
from apps.restaurants.availability import kitchen_state, reopening_label
from apps.restaurants.delivery import check_delivery
from apps.restaurants.models import (
    KitchenClosure,
    OpeningHours,
    Restaurant,
    RestaurantStatus,
    StaffMembership,
    Weekday,
)
from common.availability import UnavailabilityCode
from common.money import Money
from tests.fixtures import LOME, ouvert_en_permanence

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"
LOME_TZ = ZoneInfo("Africa/Lome")


# ================================================================ outillage


def carre(centre: Point, demi_cote: float) -> MultiPolygon:
    x, y = centre.x, centre.y
    return MultiPolygon(
        Polygon(
            (
                (x - demi_cote, y - demi_cote),
                (x + demi_cote, y - demi_cote),
                (x + demi_cote, y + demi_cote),
                (x - demi_cote, y + demi_cote),
                (x - demi_cote, y - demi_cote),
            ),
            srid=4326,
        ),
        srid=4326,
    )


def zone_de(
    city: City, nom: str, centre: Point, demi_cote: float = 0.05, **kw: Any
) -> DeliveryZone:
    return DeliveryZone.objects.create(
        city=city,
        name=nom,
        boundary=carre(centre, demi_cote),
        base_fee=Money(kw.pop("base", 500), XOF),
        fee_per_km=Money(0, XOF),
        max_distance_km=kw.pop("max_km", "50"),
        **kw,
    )


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def membre(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    user = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=user, restaurant=restaurant)
    return user


def commander(customer: User, restaurant: Restaurant, address: Address, menu_item: MenuItem) -> Any:
    CartService.add_line(
        cart=CartService.cart_for(customer, restaurant), menu_item=menu_item, quantity=1, options=[]
    )
    return connecte(customer).post(
        reverse("v1:orders:order-list"),
        {
            "restaurant": restaurant.slug,
            "address": str(address.pk),
            "payment_method": PaymentMethod.CASH,
        },
        format="json",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.fixture
def siege() -> APIClient:
    return connecte(User.objects.create_superuser("siege.reseau@elcorazon.test", "motdepasse"))


# ============================================ une zone ne tarifie que sa ville


class TestLaZoneMunicipaleResteDansSaVille:
    def test_une_zone_de_la_ville_voisine_ne_dessert_pas_une_cuisine_designee(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        """Le panier ouvert à Lomé ne se fait pas livrer au barème de la ville d'à côté.

        Avant : la zone municipale de « Baguida » couvrait le point, `resolve_zone`
        la retenait pour la cuisine de Lomé — et la course partait, facturée
        au tarif d'une ville où la cuisine n'est pas.
        """
        voisine = City.objects.create(
            country=country, name="Baguida", slug="baguida", centroid=Point(1.33, 6.16, srid=4326)
        )
        point = Point(1.36, 6.16, srid=4326)  # hors du carré « Centre » de Lomé
        zone_de(voisine, "Baguida", point, base=9_000)

        verdict = check_delivery(point=point, restaurant=restaurant)

        assert verdict.is_available is False
        assert verdict.unavailable_code == UnavailabilityCode.ADDRESS_NOT_SERVED

    def test_la_zone_de_la_ville_de_la_cuisine_la_dessert_toujours(
        self, restaurant: Restaurant, zone: DeliveryZone
    ) -> None:
        verdict = check_delivery(point=Point(1.2355, 6.1319, srid=4326), restaurant=restaurant)

        assert verdict.is_available is True
        assert verdict.zone == zone

    def test_le_choix_automatique_et_la_cuisine_designee_disent_la_meme_chose(
        self, restaurant: Restaurant, country: Country
    ) -> None:
        voisine = City.objects.create(
            country=country, name="Aného", slug="aneho", centroid=Point(1.60, 6.23, srid=4326)
        )
        point = Point(1.36, 6.16, srid=4326)
        zone_de(voisine, "Aného large", point, demi_cote=0.3)

        automatique = check_delivery(point=point)
        designee = check_delivery(point=point, restaurant=restaurant)

        # Aucune cuisine d'Aného : personne ne dessert ; et la cuisine de Lomé,
        # désignée, ne dessert pas davantage.
        assert automatique.unavailable_code == UnavailabilityCode.NO_KITCHEN_AVAILABLE
        assert designee.is_available is False


# ============================================= une cuisine sur une zone valide


class TestLeRattachementDUneCuisine:
    def test_une_cuisine_ne_se_pose_pas_sur_la_zone_propre_d_une_autre(
        self, siege: APIClient, restaurant: Restaurant, city: City
    ) -> None:
        propre = zone_de(city, "Zone de Lomé Centre", LOME, restaurant=restaurant)

        reponse = siege.post(
            reverse("v1:restaurants:managed-restaurant-list"),
            {
                "name": "El Corazón Bè",
                "slug": "el-corazon-be",
                "zone": str(propre.pk),
                "address": "Bè",
                "location": {"lat": LOME.y, "lon": LOME.x},
                "phone": "+22890000077",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert "propre à un autre établissement" in str(reponse.data)
        assert not Restaurant.objects.filter(slug="el-corazon-be").exists()

    def test_une_cuisine_peut_se_poser_sur_l_une_de_ses_zones(
        self, siege: APIClient, restaurant: Restaurant, city: City
    ) -> None:
        propre = zone_de(city, "Zone propre", LOME, restaurant=restaurant)

        reponse = siege.patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"zone": str(propre.pk)},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK, reponse.data

    @pytest.mark.parametrize("etage", ["zone", "city", "country"])
    def test_un_marche_ferme_bloque_la_publication(
        self, restaurant: Restaurant, etage: str
    ) -> None:
        cible = {
            "zone": restaurant.zone,
            "city": restaurant.zone.city,
            "country": restaurant.zone.city.country,
        }[etage]
        type(cible).objects.filter(pk=cible.pk).update(is_active=False)
        restaurant.refresh_from_db()

        manques = restaurant.configuration_gaps()

        assert any("désactivée" in manque or "fermé" in manque for manque in manques), manques

    def test_une_zone_devenue_propre_a_une_autre_cuisine_est_signalee(
        self, restaurant: Restaurant, zone: DeliveryZone
    ) -> None:
        autre = ouvert_en_permanence(
            Restaurant.objects.create(
                name="Autre",
                slug="autre",
                zone=zone,
                address="x",
                location=LOME,
                phone="+22890000078",
            )
        )
        DeliveryZone.objects.filter(pk=zone.pk).update(restaurant=autre)
        restaurant.refresh_from_db()

        assert any("propre à un autre établissement" in m for m in restaurant.configuration_gaps())


# ==================================================== la commande garde sa géo


class TestLaCommandeFigeSaGeographie:
    def test_pays_ville_et_zone_sont_figes_a_la_creation(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        zone: DeliveryZone,
    ) -> None:
        reponse = commander(customer, restaurant, address, menu_item)

        assert reponse.status_code == status.HTTP_201_CREATED, reponse.data
        commande = Order.objects.get(pk=reponse.data["id"])
        assert commande.country_id == zone.city.country_id
        assert commande.city_id == zone.city_id
        assert commande.delivery_zone_id == zone.pk
        assert commande.delivery_zone_name == "Centre"
        assert reponse.data["country"] == "TG"
        assert reponse.data["city"] == "Lomé"
        assert reponse.data["delivery_zone_name"] == "Centre"

    def test_la_zone_retenue_est_celle_de_l_adresse_pas_celle_de_la_cuisine(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        city: City,
    ) -> None:
        """La cuisine est posée sur « Centre » ; l'adresse tombe dans « Bè »,
        plus petite : c'est « Bè » qui tarife, et c'est « Bè » que la commande garde."""
        be = zone_de(city, "Bè", address.location, demi_cote=0.01, base=700)

        reponse = commander(customer, restaurant, address, menu_item)

        assert reponse.status_code == status.HTTP_201_CREATED, reponse.data
        assert Order.objects.get(pk=reponse.data["id"]).delivery_zone_id == be.pk

    def test_deplacer_la_cuisine_ne_reecrit_pas_l_histoire(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        city: City,
    ) -> None:
        reponse = commander(customer, restaurant, address, menu_item)
        commande = Order.objects.get(pk=reponse.data["id"])

        ailleurs = City.objects.create(
            country=city.country, name="Kara", slug="kara", centroid=Point(1.19, 9.55, srid=4326)
        )
        Restaurant.objects.filter(pk=restaurant.pk).update(
            zone=zone_de(ailleurs, "Kara", Point(1.19, 9.55, srid=4326))
        )

        commande.refresh_from_db()
        assert commande.city_id == city.pk
        assert commande.delivery_zone_name == "Centre"

    def test_la_supervision_filtre_par_pays_ville_zone_et_cuisine(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
        zone: DeliveryZone,
    ) -> None:
        commander(customer, restaurant, address, menu_item)
        superviseur = connecte(membre("sup.reseau@elcorazon.test", restaurant, "orders.read"))
        url = reverse("v1:orders:managed-order-list")

        def compte(**filtres: str) -> int:
            reponse = superviseur.get(url, filtres)
            assert reponse.status_code == status.HTTP_200_OK, reponse.data
            return int(reponse.data["count"])

        assert compte(country__iso_code="TG") == 1
        assert compte(country__iso_code="CI") == 0
        assert compte(city__slug="lome") == 1
        assert compte(city__slug="abidjan") == 0
        assert compte(delivery_zone=str(zone.pk)) == 1
        # Une autre zone de la même ville : la commande n'y a pas été prise.
        # (Une zone inexistante est une saisie fausse, refusée en 400.)
        voisine = zone_de(zone.city, "Bè", LOME, demi_cote=0.001)
        assert compte(delivery_zone=str(voisine.pk)) == 0
        assert (
            compte(country__iso_code="TG", city__slug="lome", restaurant__slug=restaurant.slug) == 1
        )

        comptes = superviseur.get(
            reverse("v1:orders:managed-order-counts"), {"city__slug": "abidjan"}
        )
        assert sum(comptes.data.values()) == 0


# =============================================== fermetures exceptionnelles


@pytest.fixture
def lome_de_11_a_23(restaurant: Restaurant) -> Restaurant:
    """Horaires réels d'un établissement de démonstration : 11 h – 23 h tous les jours."""
    OpeningHours.objects.filter(restaurant=restaurant).delete()
    OpeningHours.objects.bulk_create(
        OpeningHours(
            restaurant=restaurant, weekday=jour, opens_at=dt.time(11), closes_at=dt.time(23)
        )
        for jour in Weekday
    )
    return restaurant


def a_lome(annee: int, mois: int, jour: int, heure: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(annee, mois, jour, heure, minute, tzinfo=LOME_TZ)


class TestFermeturesEtReouverture:
    def test_hors_horaires_la_reouverture_est_annoncee(self, lome_de_11_a_23: Restaurant) -> None:
        # Lundi 14 septembre 2026, 8 h à Lomé.
        etat = kitchen_state(lome_de_11_a_23, a_lome(2026, 9, 14, 8))

        assert etat.unavailability is not None
        assert etat.unavailability.code == UnavailabilityCode.KITCHEN_CLOSED
        assert etat.reopens_at == a_lome(2026, 9, 14, 11)
        assert etat.reopens_label == "aujourd'hui à 11 h 00"
        assert "Réouverture aujourd'hui à 11 h 00." in etat.unavailability.message

    def test_apres_la_fermeture_la_reouverture_est_demain(
        self, lome_de_11_a_23: Restaurant
    ) -> None:
        etat = kitchen_state(lome_de_11_a_23, a_lome(2026, 9, 14, 23, 30))

        assert etat.reopens_at == a_lome(2026, 9, 15, 11)
        assert etat.reopens_label == "demain à 11 h 00"

    def test_une_fermeture_datee_ferme_dans_les_horaires(self, lome_de_11_a_23: Restaurant) -> None:
        KitchenClosure.objects.create(
            restaurant=lome_de_11_a_23,
            starts_at=a_lome(2026, 9, 14, 10),
            ends_at=a_lome(2026, 9, 14, 18),
            reason="coupure de gaz",
        )

        etat = kitchen_state(lome_de_11_a_23, a_lome(2026, 9, 14, 12))

        assert etat.is_open is True  # les horaires, eux, disent ouvert
        assert etat.is_temporarily_closed is True
        assert etat.unavailability is not None
        assert etat.unavailability.code == UnavailabilityCode.KITCHEN_TEMPORARILY_CLOSED
        assert "coupure de gaz" in etat.unavailability.message
        assert etat.reopens_at == a_lome(2026, 9, 14, 18)
        assert etat.unavailability.details["reopens_label"] == "aujourd'hui à 18 h 00"

    def test_la_fermeture_se_leve_d_elle_meme(self, lome_de_11_a_23: Restaurant) -> None:
        KitchenClosure.objects.create(
            restaurant=lome_de_11_a_23,
            starts_at=a_lome(2026, 9, 14, 10),
            ends_at=a_lome(2026, 9, 14, 18),
        )

        assert kitchen_state(lome_de_11_a_23, a_lome(2026, 9, 14, 18, 1)).can_accept_orders is True

    def test_une_fermeture_qui_deborde_l_horaire_rouvre_au_service_suivant(
        self, lome_de_11_a_23: Restaurant
    ) -> None:
        """Fermée jusqu'à 23 h 30 : la réouverture n'est pas 23 h 30 (hors
        horaires), mais le lendemain 11 h."""
        KitchenClosure.objects.create(
            restaurant=lome_de_11_a_23,
            starts_at=a_lome(2026, 9, 14, 12),
            ends_at=a_lome(2026, 9, 14, 23, 30),
        )

        etat = kitchen_state(lome_de_11_a_23, a_lome(2026, 9, 14, 15))

        assert etat.reopens_at == a_lome(2026, 9, 15, 11)

    def test_sans_aucune_plage_aucune_reouverture_n_est_inventee(
        self, restaurant: Restaurant
    ) -> None:
        OpeningHours.objects.filter(restaurant=restaurant).delete()

        etat = kitchen_state(restaurant, a_lome(2026, 9, 14, 12))

        assert etat.reopens_at is None
        assert etat.reopens_label == ""
        assert etat.unavailability is not None
        assert "Réouverture" not in etat.unavailability.message

    def test_le_libelle_se_compose_dans_le_fuseau_du_pays(self) -> None:
        maintenant = dt.datetime(2026, 9, 14, 20, tzinfo=dt.UTC)
        # 23 h 30 UTC le 14 = 1 h 30 à Nairobi le 15 : « demain », pas « aujourd'hui ».
        reouverture = dt.datetime(2026, 9, 14, 23, 30, tzinfo=dt.UTC)
        assert (
            reopening_label(reouverture, now=maintenant, timezone_name="Africa/Nairobi")
            == "demain à 2 h 30"
        )
        assert reopening_label(
            reouverture + dt.timedelta(days=3), now=maintenant, timezone_name="Africa/Lome"
        ).startswith("jeudi")

    def test_la_commande_est_refusee_pendant_une_fermeture(
        self,
        customer: User,
        restaurant: Restaurant,
        address: Address,
        menu_item: MenuItem,
    ) -> None:
        maintenant = timezone.now()
        KitchenClosure.objects.create(
            restaurant=restaurant,
            starts_at=maintenant - dt.timedelta(hours=1),
            ends_at=maintenant + dt.timedelta(hours=2),
            reason="inventaire",
        )

        reponse = commander(customer, restaurant, address, menu_item)

        assert reponse.status_code == status.HTTP_409_CONFLICT, reponse.data
        assert reponse.data["unavailable_code"] == "kitchen_temporarily_closed"
        assert "reopens_at" in reponse.data
        assert not Order.objects.exists()

    def test_l_annuaire_client_dit_fermee_et_jusqu_a_quand(self, restaurant: Restaurant) -> None:
        maintenant = timezone.now()
        KitchenClosure.objects.create(
            restaurant=restaurant,
            starts_at=maintenant - dt.timedelta(minutes=5),
            ends_at=maintenant + dt.timedelta(hours=3),
        )

        fiche = APIClient().get(reverse("v1:restaurants:restaurant-list")).data["results"][0]

        assert fiche["can_order_now"] is False
        assert fiche["unavailable_code"] == "kitchen_temporarily_closed"
        assert fiche["is_temporarily_closed"] is True
        assert fiche["reopens_at"] is not None
        assert fiche["reopens_label"]

    def test_l_annuaire_ne_coute_pas_une_requete_par_cuisine(
        self, restaurant: Restaurant, zone: DeliveryZone, django_assert_max_num_queries: Any
    ) -> None:
        for rang in range(4):
            cuisine = ouvert_en_permanence(
                Restaurant.objects.create(
                    name=f"C{rang}",
                    slug=f"c-{rang}",
                    zone=zone,
                    address="x",
                    location=LOME,
                    phone="+22890000010",
                    status=RestaurantStatus.ACTIVE,
                )
            )
            KitchenClosure.objects.create(
                restaurant=cuisine,
                starts_at=timezone.now() - dt.timedelta(hours=1),
                ends_at=timezone.now() + dt.timedelta(hours=1),
            )

        client = APIClient()
        with django_assert_max_num_queries(6):
            reponse = client.get(reverse("v1:restaurants:restaurant-list"))
        assert reponse.status_code == status.HTTP_200_OK
        assert len(reponse.data["results"]) == 5


class TestFermeturesAuBackOffice:
    def url(self) -> str:
        return reverse("v1:restaurants:managed-closure-list")

    def test_le_gerant_ferme_sa_cuisine_et_le_client_le_voit(self, restaurant: Restaurant) -> None:
        gerant = connecte(
            membre(
                "gerant.fermeture@elcorazon.test",
                restaurant,
                "restaurants.read",
                "restaurants.write",
            )
        )
        maintenant = timezone.now()

        reponse = gerant.post(
            self.url(),
            {
                "restaurant": str(restaurant.pk),
                "starts_at": (maintenant - dt.timedelta(minutes=1)).isoformat(),
                "ends_at": (maintenant + dt.timedelta(hours=2)).isoformat(),
                "reason": "jour férié",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_201_CREATED, reponse.data
        assert reponse.data["is_current"] is True
        fermeture = KitchenClosure.objects.get()
        assert fermeture.created_by is not None
        fiche = gerant.get(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug])
        )
        assert fiche.data["is_temporarily_closed"] is True
        assert fiche.data["closure_reason"] == "jour férié"

    def test_l_heure_saisie_est_celle_de_la_cuisine(self, restaurant: Restaurant) -> None:
        """Le fuseau est celui de l'établissement, pas celui du poste.

        Le back-office envoyait un instant absolu fabriqué à partir de
        l'horloge du **poste** : un siège à Lomé (UTC+0) qui fermait une
        cuisine de Douala (UTC+1) le 25 décembre à minuit fermait en réalité à
        une heure du matin, heure de Douala. Les horaires d'ouverture, eux,
        se saisissent depuis toujours en heure de la cuisine — deux conventions
        pour deux champs voisins du même écran.
        """
        Country.objects.filter(pk=restaurant.zone.city.country.pk).update(timezone="Africa/Douala")
        gerant = connecte(
            membre(
                "gerant.fuseau@elcorazon.test",
                restaurant,
                "restaurants.read",
                "restaurants.write",
            )
        )

        reponse = gerant.post(
            self.url(),
            {
                "restaurant": str(restaurant.pk),
                "starts_at_local": "2030-12-25T00:00:00",
                "ends_at_local": "2030-12-26T11:00:00",
                "reason": "Noël",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_201_CREATED, reponse.data
        fermeture = KitchenClosure.objects.get()
        # Minuit à Douala, c'est 23 h la veille en UTC.
        assert fermeture.starts_at == dt.datetime(2030, 12, 24, 23, tzinfo=dt.UTC)
        # Et la réponse rend l'heure telle que l'exploitant l'a saisie.
        assert reponse.data["starts_at_local"].startswith("2030-12-25T00:00:00")
        assert reponse.data["timezone_name"] == "Africa/Douala"

    def test_une_heure_locale_avec_decalage_est_refusee(self, restaurant: Restaurant) -> None:
        """Les deux formes ne se mélangent pas : l'une est murale, l'autre absolue."""
        gerant = connecte(
            membre(
                "gerant.melange@elcorazon.test",
                restaurant,
                "restaurants.read",
                "restaurants.write",
            )
        )

        reponse = gerant.post(
            self.url(),
            {
                "restaurant": str(restaurant.pk),
                "starts_at_local": "2030-12-25T00:00:00+02:00",
                "ends_at_local": "2030-12-26T11:00:00",
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert not KitchenClosure.objects.exists()

    def test_sans_debut_ni_fin_rien_ne_se_ferme(self, restaurant: Restaurant) -> None:
        gerant = connecte(
            membre(
                "gerant.vide@elcorazon.test",
                restaurant,
                "restaurants.read",
                "restaurants.write",
            )
        )

        reponse = gerant.post(
            self.url(), {"restaurant": str(restaurant.pk), "reason": "?"}, format="json"
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_fermeture_hors_perimetre_est_refusee(
        self, restaurant: Restaurant, zone: DeliveryZone
    ) -> None:
        autre = Restaurant.objects.create(
            name="Kara", slug="kara", zone=zone, address="x", location=LOME, phone="+22890000011"
        )
        gerant = connecte(
            membre(
                "gerant.lome@elcorazon.test", restaurant, "restaurants.read", "restaurants.write"
            )
        )

        reponse = gerant.post(
            self.url(),
            {
                "restaurant": str(autre.pk),
                "starts_at": timezone.now().isoformat(),
                "ends_at": (timezone.now() + dt.timedelta(hours=1)).isoformat(),
            },
            format="json",
        )

        assert reponse.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}
        assert not KitchenClosure.objects.exists()

    def test_sans_permission_d_ecriture_rien_ne_se_ferme(self, restaurant: Restaurant) -> None:
        lecteur = connecte(
            membre("lecteur.fermeture@elcorazon.test", restaurant, "restaurants.read")
        )

        reponse = lecteur.post(
            self.url(),
            {
                "restaurant": str(restaurant.pk),
                "starts_at": timezone.now().isoformat(),
                "ends_at": (timezone.now() + dt.timedelta(hours=1)).isoformat(),
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize(
        ("debut", "fin", "motif"),
        [
            (2, 1, "suivre le début"),
            (-3, -1, "déjà terminée"),
        ],
    )
    def test_une_fermeture_absurde_est_refusee(
        self, siege: APIClient, restaurant: Restaurant, debut: int, fin: int, motif: str
    ) -> None:
        maintenant = timezone.now()
        reponse = siege.post(
            self.url(),
            {
                "restaurant": str(restaurant.pk),
                "starts_at": (maintenant + dt.timedelta(hours=debut)).isoformat(),
                "ends_at": (maintenant + dt.timedelta(hours=fin)).isoformat(),
            },
            format="json",
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert motif in str(reponse.data)

    def test_le_filtre_upcoming_masque_le_passe(
        self, siege: APIClient, restaurant: Restaurant
    ) -> None:
        maintenant = timezone.now()
        KitchenClosure.objects.create(
            restaurant=restaurant,
            starts_at=maintenant - dt.timedelta(days=3),
            ends_at=maintenant - dt.timedelta(days=2),
        )
        KitchenClosure.objects.create(
            restaurant=restaurant,
            starts_at=maintenant + dt.timedelta(days=1),
            ends_at=maintenant + dt.timedelta(days=2),
        )

        tout = siege.get(self.url(), {"restaurant": str(restaurant.pk)})
        a_venir = siege.get(self.url(), {"restaurant": str(restaurant.pk), "upcoming": "true"})

        assert tout.data["count"] == 2
        assert a_venir.data["count"] == 1
