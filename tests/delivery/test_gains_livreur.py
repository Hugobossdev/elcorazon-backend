"""Les gains d'un livreur, agrégés par le serveur — `GET /delivery/me/earnings/`.

Pourquoi cette route existe
---------------------------

L'écran des gains de `Dely` additionnait les courses que l'application avait en
mémoire, c'est-à-dire au plus **soixante** : `recentlyDelivered` suit trois pages
de vingt, et c'est délibéré — l'historique d'un livreur en poste depuis un an
croît sans limite.

Mais l'écran en tirait « aujourd'hui », « cette semaine » et **« ce mois »**. Un
livreur à dix courses par jour n'avait donc, dans son onglet mensuel, que ses six
derniers jours. Le total était plus petit que la réalité, et affiché sans la
moindre mention de troncature — c'est le genre de chiffre qu'on ne met pas en
doute, puisqu'on compte sa paie dessus.

Aucune suite ne pouvait le voir : les tests Dart passent une liste au calcul et
vérifient la somme, ce qui est juste et ne dit rien de la liste.

Ce que ces cas verrouillent
---------------------------

Que la somme porte sur **toutes** les courses, que les bornes de période soient
celles du livreur — son fuseau, pas UTC — et que le cumul de carrière reste le
compteur du dossier plutôt qu'une seconde source qui divergerait.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import CourierService
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from common.money import Money
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

GAINS = "v1:delivery:me-earnings"


def livraison(
    courier: CourierProfile,
    restaurant: Restaurant,
    customer,
    *,
    quand: dt.datetime,
    montant: int,
    reference: str,
) -> Assignment:
    """Une course livrée à un instant donné, rémunérée d'un montant donné."""
    commande: Order = build_order(restaurant, customer, reference=reference)
    return Assignment.objects.create(
        order=commande,
        courier=courier,
        status=DeliveryStatus.DELIVERED,
        delivered_at=quand,
        courier_fee=Money(montant, "XOF"),
    )


@pytest.fixture
def courier_client(courier: CourierProfile) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=courier.user)
    return client


class TestLAgregation:
    def test_sans_course_livree_les_totaux_sont_a_zero(self, courier_client: APIClient) -> None:
        """Zéro et non l'absence de clé : l'écran affiche un montant, toujours."""
        reponse = courier_client.get(reverse(GAINS))

        assert reponse.status_code == 200
        for periode in ("today", "week", "month", "lifetime"):
            assert reponse.data[periode]["deliveries"] == 0
            assert reponse.data[periode]["earned"]["amount"] == "0"

    def test_le_total_du_mois_ne_s_arrete_pas_a_soixante_courses(
        self, courier_client: APIClient, courier, restaurant, customer
    ) -> None:
        """Le défaut, épinglé.

        Soixante-cinq courses : au-delà de ce que trois pages de vingt
        rendaient. L'application en manquait cinq, et le mois s'en trouvait
        amputé sans que rien ne le dise.
        """
        maintenant = timezone.now()
        for numero in range(65):
            livraison(
                courier,
                restaurant,
                customer,
                # Toutes dans le mois courant : le premier du mois à midi, plus
                # quelques minutes, pour ne franchir aucune borne.
                quand=maintenant.replace(day=1, hour=12) + dt.timedelta(minutes=numero),
                montant=1_000,
                reference=f"EC90{numero:04d}",
            )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["month"]["deliveries"] == 65
        assert reponse.data["month"]["earned"]["amount"] == "65000"

    def test_une_course_d_un_autre_livreur_ne_compte_pas(
        self, courier_client: APIClient, courier, restaurant, customer, courier_user
    ) -> None:
        from apps.accounts.models import User, UserType

        collegue_user = User.objects.create_user(
            "collegue@elcorazon.test", "motdepasse", full_name="Collègue"
        )
        collegue_user.user_type = UserType.COURIER
        collegue_user.save(update_fields=["user_type"])
        collegue = CourierProfile.objects.create(
            user=collegue_user, restaurant=restaurant, vehicle_type="motorcycle"
        )

        livraison(
            collegue,
            restaurant,
            customer,
            quand=timezone.now(),
            montant=5_000,
            reference="EC910001",
        )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["today"]["deliveries"] == 0

    def test_une_course_non_livree_ne_compte_pas(
        self, courier_client: APIClient, courier, restaurant, customer
    ) -> None:
        """Une course acceptée n'est pas une course payée."""
        commande = build_order(restaurant, customer, reference="EC920001")
        Assignment.objects.create(
            order=commande,
            courier=courier,
            status=DeliveryStatus.ACCEPTED,
            courier_fee=Money(1_000, "XOF"),
        )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["today"]["deliveries"] == 0


class TestLesBornesDePeriode:
    def test_une_course_du_mois_dernier_sort_du_mois(
        self, courier_client: APIClient, courier, restaurant, customer
    ) -> None:
        aujourdhui = timezone.now()
        le_mois_dernier = aujourdhui.replace(day=1) - dt.timedelta(days=2)

        livraison(
            courier,
            restaurant,
            customer,
            quand=le_mois_dernier,
            montant=3_000,
            reference="EC930001",
        )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["month"]["deliveries"] == 0
        # Elle reste dans le cumul de carrière, qui ne connaît pas de bornes.
        assert reponse.data["lifetime"]["deliveries"] == courier.deliveries_completed

    def test_les_bornes_suivent_le_fuseau_de_l_etablissement(
        self, courier_client: APIClient, courier, restaurant, customer
    ) -> None:
        """Une course livrée juste après minuit appartient à cette journée-là.

        Lomé est à UTC+0, ce qui rendrait le cas invisible : le pays est donc
        déplacé à l'est le temps du test, et la course posée à un instant qui
        tombe la veille en temps universel. Compter en UTC la ferait disparaître
        du total du jour, sous les yeux du livreur qui vient de la faire.

        ## Pourquoi l'heure locale est dérivée du fuseau, et non écrite en dur

        Ce cas a d'abord employé `Africa/Cairo` avec un décalage de `+2` écrit
        en dur, sur la foi d'un commentaire affirmant « sans heure d'été depuis
        2015 ». **L'Égypte l'a rétablie en 2023** : Le Caire est à `+3` de fin
        avril à fin octobre. Le décalage que le test appliquait et celui que la
        vue lisait divergeaient donc d'une heure, et « aujourd'hui » cessait de
        désigner la même journée pour les deux — l'échec ne survenant qu'entre
        21 h et 22 h UTC, une heure par jour, une moitié d'année.

        Deux corrections, et la seconde est la vraie :

        * `Africa/Nairobi` est à `+3` **toute l'année** — ce que le commentaire
          précédent croyait de son propre fuseau ;
        * l'heure locale est **dérivée du fuseau du pays** au lieu d'être
          recopiée. Un test qui réécrit la règle qu'il vérifie ne la vérifie
          plus, et la prochaine réforme horaire le ferait mentir de nouveau.
        """
        pays = restaurant.zone.city.country
        pays.timezone = "Africa/Nairobi"  # UTC+3 toute l'année, sans heure d'été
        pays.save(update_fields=["timezone"])

        local = timezone.now().astimezone(ZoneInfo(pays.timezone))
        # 00 h 30 heure locale — donc la veille en UTC, quel que soit le décalage.
        debut_de_journee = local.replace(hour=0, minute=30, second=0, microsecond=0)

        livraison(
            courier,
            restaurant,
            customer,
            quand=debut_de_journee,
            montant=2_000,
            reference="EC940001",
        )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["today"]["deliveries"] == 1


class TestLeCumulDeCarriere:
    def test_vient_du_dossier_et_non_d_une_seconde_somme(
        self, courier_client: APIClient, courier: CourierProfile
    ) -> None:
        """Deux sources pour un même chiffre finissent par diverger.

        `_credit` tient ce compteur à chaque livraison ; c'est aussi le solde sur
        lequel un retrait s'apprécie. Le recalculer ici en ferait un second, et
        un retrait accordé sur l'un serait refusé par l'autre.
        """
        courier.total_earnings = Money(123_456, "XOF")
        courier.deliveries_completed = 42
        courier.save(
            update_fields=[
                "total_earnings_minor",
                "total_earnings_currency",
                "deliveries_completed",
            ]
        )

        reponse = courier_client.get(reverse(GAINS))

        assert reponse.data["lifetime"]["earned"]["amount"] == "123456"
        assert reponse.data["lifetime"]["deliveries"] == 42


class TestLAcces:
    def test_un_client_n_y_a_pas_droit(self, customer) -> None:
        client = APIClient()
        client.force_authenticate(user=customer)

        assert client.get(reverse(GAINS)).status_code == 403

    def test_sans_session_non_plus(self) -> None:
        assert APIClient().get(reverse(GAINS)).status_code == 401


def test_le_service_est_appelable_hors_http(courier: CourierProfile) -> None:
    """La règle vit dans le service, pas dans la vue."""
    gains = CourierService.earnings(courier=courier)

    assert set(gains) == {"today", "week", "month", "lifetime"}
