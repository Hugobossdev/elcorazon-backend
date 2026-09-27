"""Correction d'un dossier livreur — `PATCH /delivery/couriers/{id}/`.

Le back-office n'avait aucun moyen de rectifier une plaque relevée de travers à
l'embauche : `StaffCourierViewSet` était `CreateModelMixin + ReadOnly`. La seule
issue était d'ouvrir un second compte — c'est-à-dire de dédoubler un livreur et
de scinder ses compteurs entre deux dossiers.

**Le cœur de ces tests n'est pas que la modification passe, c'est qu'elle ne
passe que là où elle doit.** Une route d'écriture nouvelle sur un dossier qui
porte un statut de vérification, des compteurs de livraison et des gains est
exactement l'endroit où une liste blanche trop large ne se voit pas : tout
continue de fonctionner, et un compte muni de `couriers.write` peut désormais
valider son propre recrutement ou se fabriquer une note de 5/5.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import CourierProfile, VehicleType
from apps.delivery.states import VerificationStatus
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def _personnel(restaurant: Restaurant | None, email: str, *permissions: str) -> User:
    membre = User.objects.create_user(
        email, "motdepasse", full_name="Afi Responsable", user_type=UserType.STAFF
    )
    membre.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


@pytest.fixture
def as_responsable(restaurant: Restaurant) -> APIClient:
    """Personnel muni de `couriers.write` — le droit d'écrire sur la flotte."""
    client = APIClient()
    client.force_authenticate(
        _personnel(restaurant, "flotte@elcorazon.test", "couriers.read", "couriers.write")
    )
    return client


def _url(courier: CourierProfile) -> str:
    return reverse("v1:delivery:courier-detail", args=[courier.pk])


class TestCorrection:
    def test_la_plaque_se_corrige(self, as_responsable: APIClient, courier: CourierProfile) -> None:
        reponse = as_responsable.patch(
            _url(courier), {"vehicle_plate": "TG-4242-AB"}, format="json"
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.vehicle_plate == "TG-4242-AB"

    def test_le_nom_et_le_telephone_vivent_sur_le_compte_et_se_corrigent_quand_meme(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """L'appelant corrige « le livreur », pas « le compte du livreur » : la
        distinction est une affaire de schéma et n'a pas à remonter."""
        reponse = as_responsable.patch(
            _url(courier),
            {"full_name": "Kodjo Mensah-Adjei", "phone": "+22891234567"},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.user.refresh_from_db()
        assert courier.user.full_name == "Kodjo Mensah-Adjei"
        assert courier.user.phone == "+22891234567"

    def test_le_vehicule_se_change(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        reponse = as_responsable.patch(
            _url(courier), {"vehicle_type": VehicleType.CAR}, format="json"
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.vehicle_type == VehicleType.CAR

    def test_la_reponse_rend_le_dossier_complet(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """L'écran remplace sa ligne avec ce qu'il reçoit : une réponse
        partielle lui ferait perdre les compteurs qu'il affichait."""
        reponse = as_responsable.patch(
            _url(courier), {"vehicle_plate": "TG-0001-ZZ"}, format="json"
        )

        assert {"deliveries_completed", "rating_average", "verification_status"} <= set(
            reponse.data
        )
        assert reponse.data["vehicle_plate"] == "TG-0001-ZZ"

    def test_un_corps_vide_est_refuse(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """Sinon DRF rend 200 sans rien écrire, et l'écran annonce
        « enregistré » sur une requête qui n'a rien enregistré."""
        reponse = as_responsable.patch(_url(courier), {}, format="json")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST


class TestListeBlanche:
    """Ce que `PATCH` ne doit **pas** pouvoir écrire.

    Chaque cas correspond à un pouvoir qu'une liste blanche trop large
    accorderait sans que rien ne le signale.
    """

    @pytest.mark.parametrize(
        ("champ", "valeur"),
        [
            # Valider son propre recrutement, sans passer par `couriers.approve`.
            ("verification_status", VerificationStatus.APPROVED),
            # Rendre un livreur éligible à des courses qu'il ne verra pas.
            ("is_online", True),
            # Fabriquer une réputation.
            ("rating_average", "5.00"),
            ("rating_count", 999),
            # Fabriquer de l'ancienneté.
            ("deliveries_completed", 9999),
            ("deliveries_cancelled", 0),
            # Effacer le motif d'une suspension.
            ("verification_notes", "rien à signaler"),
        ],
    )
    def test_le_champ_est_ignore(
        self, as_responsable: APIClient, courier: CourierProfile, champ: str, valeur: object
    ) -> None:
        avant = getattr(courier, champ)

        reponse = as_responsable.patch(
            _url(courier), {champ: valeur, "vehicle_plate": "TG-1111-XX"}, format="json"
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert getattr(courier, champ) == avant
        # La correction légitime de la même requête, elle, a bien pris : le
        # champ interdit est ignoré, il ne fait pas échouer le reste.
        assert courier.vehicle_plate == "TG-1111-XX"

    def test_l_etablissement_ne_se_mute_pas(
        self, as_responsable: APIClient, courier: CourierProfile, restaurant: Restaurant
    ) -> None:
        """Muter un livreur le ferait sortir du périmètre de qui le mute, qui
        perdrait le dossier de vue au milieu du geste."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            phone="+22890000009",
            location=restaurant.location,
        )

        reponse = as_responsable.patch(
            _url(courier), {"restaurant": str(ailleurs.pk)}, format="json"
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        courier.refresh_from_db()
        assert courier.restaurant_id == restaurant.pk

    def test_l_adresse_electronique_ne_se_change_pas(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """C'est l'identifiant du compte, donc un chemin de reprise par
        « mot de passe oublié »."""
        avant = courier.user.email

        reponse = as_responsable.patch(
            _url(courier),
            {"email": "pirate@example.test", "vehicle_plate": "TG-2222-YY"},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.user.refresh_from_db()
        assert courier.user.email == avant

    def test_put_est_refuse(self, as_responsable: APIClient, courier: CourierProfile) -> None:
        """Un remplacement complet écraserait par un défaut le premier champ
        que l'appelant oublierait."""
        reponse = as_responsable.put(_url(courier), {"vehicle_plate": "TG-3333-WW"}, format="json")

        assert reponse.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestValidation:
    def test_un_type_de_vehicule_inconnu_est_refuse(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        reponse = as_responsable.patch(
            _url(courier), {"vehicle_type": "hélicoptère"}, format="json"
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_telephone_deja_pris_est_refuse_avec_un_message(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """La contrainte d'unicité de la base sortirait en 500."""
        User.objects.create_user(
            "autre@elcorazon.test",
            "motdepasse",
            full_name="Autre",
            user_type=UserType.CUSTOMER,
            phone="+22899887766",
        )

        reponse = as_responsable.patch(_url(courier), {"phone": "+22899887766"}, format="json")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert "phone" in reponse.data.get("errors", reponse.data)

    def test_son_propre_telephone_reste_acceptable(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        """L'unicité ne doit pas se déclencher contre le dossier qu'on corrige :
        renvoyer le formulaire sans toucher au numéro échouerait."""
        courier.user.phone = "+22890001122"
        courier.user.save(update_fields=["phone"])

        reponse = as_responsable.patch(
            _url(courier), {"phone": "+22890001122", "vehicle_plate": "TG-5555-VV"}, format="json"
        )

        assert reponse.status_code == status.HTTP_200_OK

    def test_un_numero_mal_forme_est_refuse(
        self, as_responsable: APIClient, courier: CourierProfile
    ) -> None:
        reponse = as_responsable.patch(_url(courier), {"phone": "pas un numéro"}, format="json")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST


class TestPermissionsEtPerimetre:
    def test_sans_couriers_write_la_correction_est_refusee(
        self, restaurant: Restaurant, courier: CourierProfile
    ) -> None:
        """Lire la flotte ne donne pas le droit de l'écrire."""
        client = APIClient()
        client.force_authenticate(_personnel(restaurant, "lecteur@elcorazon.test", "couriers.read"))

        reponse = client.patch(_url(courier), {"vehicle_plate": "TG-6666-UU"}, format="json")

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_un_dossier_hors_perimetre_est_introuvable(
        self, restaurant: Restaurant, courier: CourierProfile
    ) -> None:
        """404 et non 403 : l'existence d'un livreur d'une autre enseigne n'a
        pas à se déduire d'un code de réponse."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=restaurant.zone,
            address="Kara",
            phone="+22890000008",
            location=restaurant.location,
        )
        client = APIClient()
        client.force_authenticate(
            _personnel(ailleurs, "kara@elcorazon.test", "couriers.read", "couriers.write")
        )

        reponse = client.patch(_url(courier), {"vehicle_plate": "TG-7777-TT"}, format="json")

        assert reponse.status_code == status.HTTP_404_NOT_FOUND

    def test_un_livreur_ne_corrige_pas_son_propre_dossier_par_cette_route(
        self, courier: CourierProfile
    ) -> None:
        """Le livreur a sa propre route (`/delivery/me/`) ; celle-ci est celle
        du personnel et exige `couriers.write`."""
        client = APIClient()
        client.force_authenticate(courier.user)

        reponse = client.patch(_url(courier), {"vehicle_plate": "TG-8888-SS"}, format="json")

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_un_dossier_inexistant_repond_404(self, as_responsable: APIClient) -> None:
        reponse = as_responsable.patch(
            reverse("v1:delivery:courier-detail", args=["00000000-0000-0000-0000-000000000000"]),
            {"vehicle_plate": "TG-9999-RR"},
            format="json",
        )

        assert reponse.status_code == status.HTTP_404_NOT_FOUND
