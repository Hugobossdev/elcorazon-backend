"""Back-office de la gamification et des récompenses.

Ces catalogues sont de la **donnée d'exploitation** : créer « 10 commandes ce
mois-ci, 500 points » ne doit pas demander un déploiement.

Les tests les plus utiles ici sont ceux qui vérifient ce que ces routes **ne**
font pas : ni attribuer un succès, ni créditer des points, ni supprimer ce
qu'un client a déjà débloqué.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.gamification.models import Achievement, Badge, Challenge
from apps.loyalty.models import Reward, RewardKind
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = pytest.mark.django_db

XOF = "XOF"


@pytest.fixture
def as_animateur() -> APIClient:
    """Compte qui compose les catalogues, sans périmètre d'établissement."""
    membre = User.objects.create_user(
        "jeux@elcorazon.test", "motdepasse", full_name="Animation", user_type=UserType.STAFF
    )
    membre.roles.add(
        Role.objects.create(
            name="Animation",
            permissions=[
                "gamification.read",
                "gamification.write",
                "loyalty.read",
                "loyalty.write",
            ],
        )
    )
    client = APIClient()
    client.force_authenticate(membre)
    return client


@pytest.fixture
def as_gerant(restaurant: Restaurant) -> APIClient:
    """Même métier, mais cloisonné sur un établissement."""
    membre = User.objects.create_user(
        "gerant@elcorazon.test", "motdepasse", full_name="Gérant", user_type=UserType.STAFF
    )
    membre.roles.add(
        Role.objects.create(name="Gérance", permissions=["loyalty.read", "loyalty.write"])
    )
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    client = APIClient()
    client.force_authenticate(membre)
    return client


class TestSucces:
    def test_l_exploitation_cree_un_succes_sans_deploiement(self, as_animateur: APIClient) -> None:
        response = as_animateur.post(
            reverse("v1:gamification:managed-achievement-list"),
            {
                "name": "Habitué",
                "description": "Dix commandes livrées",
                "condition_type": "orders_count",
                "condition_value": 10,
                "points_reward": 500,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Achievement.objects.get(name="Habitué").points_reward == 500

    def test_un_succes_desactive_reste_visible_au_back_office(
        self, as_animateur: APIClient
    ) -> None:
        """La forme cliente le masque ; l'écran qui sert à le réactiver doit
        pouvoir le voir."""
        Achievement.objects.create(
            name="Ancien", condition_type="orders_count", condition_value=3, is_active=False
        )

        response = as_animateur.get(reverse("v1:gamification:managed-achievement-list"))

        assert [item["name"] for item in response.data["results"]] == ["Ancien"]

    def test_la_suppression_n_est_pas_exposee(self, as_animateur: APIClient) -> None:
        """Elle emporterait par cascade ce que des clients ont débloqué."""
        succes = Achievement.objects.create(
            name="Fidèle", condition_type="orders_count", condition_value=5
        )

        response = as_animateur.delete(
            reverse("v1:gamification:managed-achievement-detail", kwargs={"pk": succes.pk})
        )

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert Achievement.objects.filter(pk=succes.pk).exists()


class TestDefis:
    def test_un_defi_dont_la_fin_precede_le_debut_est_refuse_lisiblement(
        self, as_animateur: APIClient
    ) -> None:
        """En 400 plutôt qu'en violation d'intégrité : l'exploitation lirait un
        500 comme une panne du serveur."""
        maintenant = timezone.now()

        response = as_animateur.post(
            reverse("v1:gamification:managed-challenge-list"),
            {
                "title": "Semaine folle",
                "challenge_type": "weekly",
                "condition_type": "orders_count",
                "target_value": 5,
                "starts_at": maintenant.isoformat(),
                "ends_at": (maintenant - dt.timedelta(days=1)).isoformat(),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        # Forme RFC 9457 (ADR-009) : les erreurs de champ sont sous `errors`.
        assert "ends_at" in response.data["errors"]

    def test_un_defi_bien_borne_est_accepte(self, as_animateur: APIClient) -> None:
        maintenant = timezone.now()

        response = as_animateur.post(
            reverse("v1:gamification:managed-challenge-list"),
            {
                "title": "Semaine folle",
                "challenge_type": "weekly",
                "condition_type": "orders_count",
                "target_value": 5,
                "reward_points": 200,
                "starts_at": maintenant.isoformat(),
                "ends_at": (maintenant + dt.timedelta(days=7)).isoformat(),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Challenge.objects.get(title="Semaine folle").target_value == 5


class TestRecompenses:
    def test_une_recompense_a_zero_point_est_refusee(self, as_animateur: APIClient) -> None:
        """Elle ne débiterait rien et se réclamerait en boucle."""
        response = as_animateur.post(
            reverse("v1:loyalty:managed-reward-list"),
            {
                "name": "Café offert",
                "kind": RewardKind.FREE_DELIVERY,
                "points_cost": 0,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_le_montant_de_remise_s_ecrit_en_money(
        self, as_gerant: APIClient, restaurant: Restaurant
    ) -> None:
        response = as_gerant.post(
            reverse("v1:loyalty:managed-reward-list"),
            {
                "name": "500 F de remise",
                "kind": RewardKind.DISCOUNT,
                "points_cost": 1_000,
                "discount": {"amount": "500", "currency": XOF},
                "restaurant": str(restaurant.pk),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Reward.objects.get(name="500 F de remise").discount == Money(500, XOF)

    def test_un_gerant_ne_cree_pas_de_recompense_nationale(self, as_gerant: APIClient) -> None:
        """Elle s'échangerait dans les établissements des autres."""
        response = as_gerant.post(
            reverse("v1:loyalty:managed-reward-list"),
            {"name": "Partout", "kind": RewardKind.FREE_DELIVERY, "points_cost": 800},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Reward.objects.filter(name="Partout").exists()

    def test_un_gerant_cree_une_recompense_de_son_etablissement(
        self, as_gerant: APIClient, restaurant: Restaurant
    ) -> None:
        response = as_gerant.post(
            reverse("v1:loyalty:managed-reward-list"),
            {
                "name": "Dessert offert",
                "kind": RewardKind.DISCOUNT,
                "points_cost": 600,
                "discount": {"amount": "300", "currency": XOF},
                "restaurant": str(restaurant.pk),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_un_gerant_voit_les_recompenses_nationales_sans_les_modifier(
        self, as_gerant: APIClient
    ) -> None:
        nationale = Reward.objects.create(
            name="Livraison offerte", kind=RewardKind.FREE_DELIVERY, points_cost=400
        )

        liste = as_gerant.get(reverse("v1:loyalty:managed-reward-list"))
        modification = as_gerant.patch(
            reverse("v1:loyalty:managed-reward-detail", kwargs={"pk": nationale.pk}),
            {"points_cost": 1},
            format="json",
        )

        assert [item["name"] for item in liste.data["results"]] == ["Livraison offerte"]
        assert modification.status_code == status.HTTP_403_FORBIDDEN


class TestAcces:
    def test_sans_permission_rien_ne_s_ecrit(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        response = client.post(
            reverse("v1:gamification:managed-badge-list"),
            {"title": "VIP", "points_required": 10},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_la_lecture_seule_ne_donne_pas_l_ecriture(self) -> None:
        """`gamification.read` consulte ; composer le catalogue est un autre
        métier."""
        observateur = User.objects.create_user(
            "observe@elcorazon.test",
            "motdepasse",
            full_name="Observateur",
            user_type=UserType.STAFF,
        )
        observateur.roles.add(
            Role.objects.create(name="Lecture jeux", permissions=["gamification.read"])
        )
        client = APIClient()
        client.force_authenticate(observateur)
        Badge.objects.create(title="Bronze", points_required=100)

        lecture = client.get(reverse("v1:gamification:managed-badge-list"))
        ecriture = client.post(
            reverse("v1:gamification:managed-badge-list"),
            {"title": "Argent", "points_required": 500},
            format="json",
        )

        assert lecture.status_code == status.HTTP_200_OK
        assert ecriture.status_code == status.HTTP_403_FORBIDDEN
