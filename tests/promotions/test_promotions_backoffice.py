"""Administration des codes promotionnels — F4 et ADR-005.

Deux vérifications portent ce module :

* `test_un_code_national_est_refuse_a_un_compte_cloisonne` — accorder le
  national à qui n'a qu'un restaurant lui donnerait le pouvoir de remiser les
  autres ;
* `test_un_titulaire_ne_se_designe_pas` — un code nominatif naît d'un échange
  de points, qui l'a fait payer. En frapper un ici distribuerait des
  récompenses sans débit.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.promotions.models import DiscountKind, Promotion
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def personnel(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def marketing(restaurant: Restaurant) -> APIClient:
    """Chargé de marketing, cloisonné à son établissement."""
    client = APIClient()
    client.force_authenticate(
        personnel("marketing@elcorazon.test", restaurant, "promotions.read", "promotions.write")
    )
    return client


@pytest.fixture
def siege() -> APIClient:
    """Le siège : superutilisateur, donc non cloisonné."""
    admin = User.objects.create_superuser("siege@elcorazon.test", "motdepasse")
    client = APIClient()
    client.force_authenticate(admin)
    return client


def corps(**extra: object) -> dict[str, object]:
    debut = timezone.now()
    valeurs: dict[str, object] = {
        "code": "WEEKEND",
        "kind": DiscountKind.FIXED,
        "amount": {"amount": "500", "currency": XOF},
        "starts_at": debut.isoformat(),
        "ends_at": (debut + dt.timedelta(days=2)).isoformat(),
        "usage_limit": 10,
        "usage_limit_per_user": 1,
    }
    valeurs.update(extra)
    return valeurs


class TestPermissions:
    def test_lire_ne_donne_pas_le_droit_d_ecrire(self, restaurant: Restaurant) -> None:
        client = APIClient()
        client.force_authenticate(
            personnel("lecture@elcorazon.test", restaurant, "promotions.read")
        )

        lecture = client.get(reverse("v1:promotions:promotion-list"))
        ecriture = client.post(
            reverse("v1:promotions:promotion-list"),
            corps(restaurant=restaurant.slug),
            format="json",
        )

        assert lecture.status_code == status.HTTP_200_OK
        assert ecriture.status_code == status.HTTP_403_FORBIDDEN

    def test_un_client_n_atteint_pas_la_liste(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        assert (
            client.get(reverse("v1:promotions:promotion-list")).status_code
            == status.HTTP_403_FORBIDDEN
        )


class TestPerimetre:
    def test_un_code_d_etablissement_se_cree_dans_son_perimetre(
        self, marketing: APIClient, restaurant: Restaurant
    ) -> None:
        response = marketing.post(
            reverse("v1:promotions:promotion-list"),
            corps(restaurant=restaurant.slug),
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Promotion.objects.get(code="WEEKEND").restaurant_id == restaurant.pk

    def test_un_code_national_est_refuse_a_un_compte_cloisonne(self, marketing: APIClient) -> None:
        """Un code sans établissement s'applique partout : le confier à qui n'a
        qu'un restaurant lui donnerait le pouvoir de remiser les autres."""
        response = marketing.post(reverse("v1:promotions:promotion-list"), corps(), format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Promotion.objects.exists()

    def test_le_siege_cree_un_code_national(self, siege: APIClient) -> None:
        response = siege.post(reverse("v1:promotions:promotion-list"), corps(), format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert Promotion.objects.get(code="WEEKEND").restaurant_id is None

    def test_un_code_national_est_invisible_a_un_compte_cloisonne(
        self, marketing: APIClient
    ) -> None:
        Promotion.objects.create(  # type: ignore[misc]
            code="NATIONAL",
            kind=DiscountKind.FIXED,
            amount=Money(500, XOF),
            starts_at=timezone.now(),
            ends_at=timezone.now() + dt.timedelta(days=1),
        )

        assert marketing.get(reverse("v1:promotions:promotion-list")).data["count"] == 0


class TestCoherence:
    def test_un_pourcentage_sans_pourcentage_est_refuse(
        self, marketing: APIClient, restaurant: Restaurant
    ) -> None:
        """La contrainte `CHECK` reste la dernière ligne de défense ; ce qu'on
        gagne ici, c'est un refus lisible plutôt qu'une erreur serveur."""
        response = marketing.post(
            reverse("v1:promotions:promotion-list"),
            corps(restaurant=restaurant.slug, kind=DiscountKind.PERCENTAGE, percentage="0"),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_montant_fixe_sans_montant_est_refuse(
        self, marketing: APIClient, restaurant: Restaurant
    ) -> None:
        response = marketing.post(
            reverse("v1:promotions:promotion-list"),
            corps(restaurant=restaurant.slug, amount=None),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_periode_vide_est_refusee(
        self, marketing: APIClient, restaurant: Restaurant
    ) -> None:
        moment = timezone.now()
        response = marketing.post(
            reverse("v1:promotions:promotion-list"),
            corps(
                restaurant=restaurant.slug,
                starts_at=moment.isoformat(),
                ends_at=(moment - dt.timedelta(hours=1)).isoformat(),
            ),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestCompteurs:
    def test_le_compteur_d_usage_ne_s_ecrit_pas(
        self, marketing: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Il est tenu sous verrou par `PromotionService`. Le rendre
        inscriptible permettrait de rouvrir un quota épuisé sans trace."""
        promotion = Promotion.objects.create(  # type: ignore[misc]
            code="ETE",
            kind=DiscountKind.FIXED,
            amount=Money(500, XOF),
            starts_at=timezone.now(),
            ends_at=timezone.now() + dt.timedelta(days=1),
            restaurant=restaurant,
            usage_limit=1,
        )
        Promotion.objects.filter(pk=promotion.pk).update(used_count=1)

        marketing.patch(
            reverse("v1:promotions:promotion-detail", args=[promotion.pk]),
            {"used_count": 0},
            format="json",
        )

        promotion.refresh_from_db()
        assert promotion.used_count == 1

    def test_un_titulaire_ne_se_designe_pas(
        self, marketing: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """Un code nominatif naît d'un échange de points, qui l'a fait payer."""
        response = marketing.post(
            reverse("v1:promotions:promotion-list"),
            corps(restaurant=restaurant.slug, owner=str(customer.pk)),
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Promotion.objects.get(code="WEEKEND").owner_id is None


class TestSuspension:
    def test_un_code_se_suspend_sans_s_effacer(
        self, marketing: APIClient, restaurant: Restaurant
    ) -> None:
        """Les utilisations déjà consommées y renvoient : l'effacer rendrait
        illisible la remise portée par une commande passée."""
        promotion = Promotion.objects.create(  # type: ignore[misc]
            code="ETE",
            kind=DiscountKind.FIXED,
            amount=Money(500, XOF),
            starts_at=timezone.now(),
            ends_at=timezone.now() + dt.timedelta(days=1),
            restaurant=restaurant,
        )
        url = reverse("v1:promotions:promotion-detail", args=[promotion.pk])

        suspendu = marketing.patch(url, {"is_active": False}, format="json")
        efface = marketing.delete(url)

        assert suspendu.status_code == status.HTTP_200_OK
        assert efface.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        promotion.refresh_from_db()
        assert not promotion.is_active
