"""API du carnet d'adresses.

Le test qui porte cette suite est `test_l_adresse_d_autrui_est_introuvable` :
c'est la forme la plus courante de faille d'API — un identifiant deviné et
aucun filtre sur le propriétaire.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.geography.models import City
from apps.profiles.models import Address

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(client: APIClient, customer: User) -> APIClient:
    client.force_authenticate(customer)
    return client


def payload(city: City, **overrides: object) -> dict[str, object]:
    return {
        "label": "Bureau",
        "line1": "Boulevard du 13 Janvier",
        "city": str(city.pk),
        "location": {"lat": 6.1400, "lon": 1.2200},
        **overrides,
    }


class TestCreation:
    def test_un_client_ajoute_une_adresse(self, as_customer: APIClient, city: City) -> None:
        response = as_customer.post(
            reverse("v1:profiles:address-list"), payload(city), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["location"] == {"lat": 6.14, "lon": 1.22}
        assert response.data["city_name"] == "Lomé"

    def test_la_premiere_adresse_devient_le_defaut(
        self, as_customer: APIClient, city: City
    ) -> None:
        """Un carnet sans défaut obligerait l'écran de commande à choisir
        arbitrairement, ou à ne rien pré-remplir."""
        response = as_customer.post(
            reverse("v1:profiles:address-list"), payload(city), format="json"
        )

        assert response.data["is_default"] is True

    def test_le_proprietaire_ne_s_envoie_pas(
        self, as_customer: APIClient, city: City, courier_user: User
    ) -> None:
        """Un champ propriétaire acceptable en entrée serait une prise de
        contrôle en un paramètre de formulaire."""
        response = as_customer.post(
            reverse("v1:profiles:address-list"),
            payload(city, user=str(courier_user.pk)),
            format="json",
        )

        assert Address.objects.get(pk=response.data["id"]).user.email == "cliente@elcorazon.test"

    def test_une_position_est_exigee(self, as_customer: APIClient, city: City) -> None:
        """À Lomé, l'adressage postal ne permet pas de trouver une porte : le
        point est ce dont le livreur se sert réellement."""
        sans_point = payload(city)
        del sans_point["location"]

        response = as_customer.post(reverse("v1:profiles:address-list"), sans_point, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "location" in response.data["errors"]


class TestDefautUnique:
    def test_promouvoir_une_adresse_retire_le_defaut_a_l_autre(
        self, as_customer: APIClient, address: Address, city: City
    ) -> None:
        """L'index unique partiel refuse deux défauts : la promotion doit
        rétrograder l'ancienne, dans la même transaction."""
        seconde = as_customer.post(
            reverse("v1:profiles:address-list"), payload(city), format="json"
        ).data

        as_customer.patch(
            reverse("v1:profiles:address-detail", args=[seconde["id"]]),
            {"is_default": True},
            format="json",
        )

        address.refresh_from_db()
        assert address.is_default is False
        assert Address.objects.filter(user=address.user, is_default=True).count() == 1


class TestCloisonnement:
    def test_le_carnet_ne_montre_que_ses_adresses(
        self, as_customer: APIClient, address: Address, courier_user: User, city: City
    ) -> None:
        Address.objects.create(
            user=courier_user,
            label="Chez le livreur",
            line1="Ailleurs",
            city=city,
            location=address.location,
        )

        response = as_customer.get(reverse("v1:profiles:address-list"))

        assert response.data["count"] == 1
        assert response.data["results"][0]["id"] == str(address.pk)

    def test_l_adresse_d_autrui_est_introuvable(
        self, as_customer: APIClient, courier_user: User, city: City, address: Address
    ) -> None:
        """404 et non 403 : un 403 confirmerait que l'identifiant deviné
        existe, ce qui se lit dans le code de statut sans jamais voir le
        contenu."""
        autre = Address.objects.create(
            user=courier_user,
            label="Chez le livreur",
            line1="Ailleurs",
            city=city,
            location=address.location,
        )

        for method in ("get", "delete"):
            response = getattr(as_customer, method)(
                reverse("v1:profiles:address-detail", args=[autre.pk])
            )
            assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_un_visiteur_anonyme_n_a_pas_de_carnet(self, client: APIClient) -> None:
        assert (
            client.get(reverse("v1:profiles:address-list")).status_code
            == status.HTTP_401_UNAUTHORIZED
        )


class TestPreferences:
    def test_lues_sans_avoir_ete_creees(self, as_customer: APIClient) -> None:
        """Créées à la demande : le client n'a pas à savoir qu'un objet doit
        exister avant d'être lu."""
        response = as_customer.get(reverse("v1:profiles:preferences"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["marketing_push_enabled"] is True

    def test_modifiables_partiellement(self, as_customer: APIClient) -> None:
        response = as_customer.patch(
            reverse("v1:profiles:preferences"),
            {"allergens": ["arachide"], "marketing_email_enabled": False},
            format="json",
        )

        assert response.data["allergens"] == ["arachide"]
        assert response.data["marketing_email_enabled"] is False
        assert response.data["marketing_push_enabled"] is True
