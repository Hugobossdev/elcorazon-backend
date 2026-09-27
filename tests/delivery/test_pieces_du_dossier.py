"""Le dossier du livreur, par lui-même — `GET`, `POST` et `PATCH /delivery/me/`.

Ce fichier couvre deux gestes qui n'en avaient aucun.

## Le dépôt de pièces

`CourierService.replace_documents` existait depuis l'origine, sans un seul
test — et sans un seul appelant : aucune des trois applications Flutter ne
savait téléverser une pièce. Le défaut était donc invisible des deux côtés à la
fois, et il était grave : un dossier **refusé** restait `rejected` quoi qu'on y
dépose. La machine autorise pourtant `REJECTED → PENDING`, l'écran du livreur
lui promet un réexamen, et le back-office ne liste comme « à instruire » que les
dossiers `pending`. Un livreur refusé pour une photo illisible pouvait en
redéposer dix : elles arrivaient en base, et personne ne les regardait.

La symétrie compte autant que la correction. Un dossier **suspendu** ne doit
*pas* se rouvrir de la même façon : une suspension est une sanction
d'exploitation, pas un défaut de pièce, et s'en relever en téléversant une carte
grise en ferait une formalité. Les deux cas sont vérifiés côte à côte, parce que
c'est leur différence qui porte la règle.

## La correction du dossier

`PATCH /delivery/me/` n'existait pas. `Dely` affichait le véhicule et la plaque
dans des champs grisés, commentés « on ne permet pas de modifier ici pour
l'instant » ; un livreur qui changeait de véhicule n'avait aucune issue.

Le cœur de ces tests-là n'est pas que la correction passe, c'est **qu'elle
s'arrête où elle doit** : une route d'écriture ouverte au livreur lui-même, sur
un objet qui porte son statut de vérification, ses compteurs et ses gains, est
exactement l'endroit où une liste blanche trop large ne se voit pas — tout
continue de fonctionner, et le livreur valide son propre recrutement.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, UserType
from apps.delivery.models import CourierProfile, VehicleType
from apps.delivery.states import VerificationStatus

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def _url() -> str:
    """L'adresse du dossier, résolue **à l'appel** et non à l'import.

    Un `reverse()` au niveau du module s'évalue pendant la collecte de pytest,
    avant que `setup_test_environment()` n'ait posé `DEBUG = False`. Il importe
    donc la configuration d'URL en mode debug et la met en cache — avec la route
    `api/v1/docs/`, qui n'existe que sous `DEBUG`. Le test d'architecture qui
    vérifie la surface publique la voyait ensuite apparaître, et échouait selon
    l'ordre des fichiers. Tous les autres fichiers de la suite appellent
    `reverse()` dans le corps des tests ; celui-ci le fait aussi.
    """
    return reverse("v1:delivery:me")


def _piece(nom: str = "permis.jpg") -> SimpleUploadedFile:
    """Un fichier minuscule mais réel — le stockage de test est en mémoire."""
    return SimpleUploadedFile(nom, b"\xff\xd8\xff\xe0 pieces jointes", content_type="image/jpeg")


@pytest.fixture
def as_courier(courier: CourierProfile) -> APIClient:
    client = APIClient()
    client.force_authenticate(courier.user)
    return client


class TestDepotDePieces:
    """`POST /delivery/me/` — le téléversement, et ce qu'il déclenche."""

    def test_la_piece_est_enregistree(self, as_courier: APIClient, courier: CourierProfile) -> None:
        reponse = as_courier.post(_url(), {"licence_document": _piece()}, format="multipart")

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.licence_document.name
        assert reponse.data["licence_document"]

    def test_un_dossier_valide_repasse_en_instruction(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """L5 — un dossier validé sur des pièces qu'on remplace n'est plus validé."""
        assert courier.verification_status == VerificationStatus.APPROVED

        as_courier.post(_url(), {"id_document": _piece("cni.jpg")}, format="multipart")

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.PENDING
        # Et hors ligne : le laisser « en ligne » le maintiendrait dans les
        # listes d'affectation, où seul `can_accept_orders` l'écarterait.
        assert courier.is_online is False

    def test_un_dossier_refuse_retourne_dans_la_file(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Le défaut central : redéposer ne rouvrait rien.

        Sans cette transition, la pièce corrigée arrivait bien en base et le
        dossier restait `rejected` — donc absent de la file d'instruction du
        back-office, qui ne liste que `pending`. Le livreur corrigeait dans le
        vide, indéfiniment.
        """
        courier.verification_status = VerificationStatus.REJECTED
        courier.verification_notes = "Photo du permis illisible."
        courier.is_online = False
        courier.save()

        reponse = as_courier.post(_url(), {"licence_document": _piece()}, format="multipart")

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.PENDING

    def test_le_motif_du_refus_precedent_est_efface(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Il porte sur une pièce qui n'est plus là.

        Le garder ferait lire au livreur, sur le dossier qu'il vient de
        corriger, le reproche auquel il vient de répondre.
        """
        courier.verification_status = VerificationStatus.REJECTED
        courier.verification_notes = "Photo du permis illisible."
        courier.save()

        as_courier.post(_url(), {"licence_document": _piece()}, format="multipart")

        courier.refresh_from_db()
        assert courier.verification_notes == ""

    def test_un_dossier_suspendu_le_reste(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Une suspension est une sanction, pas un défaut de pièce.

        C'est le pendant du test précédent, et la raison pour laquelle la règle
        énumère des statuts plutôt que de dire « tout sauf approuvé ». La
        machine refuse d'ailleurs `SUSPENDED → PENDING`.
        """
        courier.verification_status = VerificationStatus.SUSPENDED
        courier.is_online = False
        courier.save()

        reponse = as_courier.post(_url(), {"id_document": _piece("cni.jpg")}, format="multipart")

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        # La pièce est bien reçue — rien n'interdit de la fournir — mais elle
        # n'achète pas la levée de la sanction.
        assert courier.id_document.name
        assert courier.verification_status == VerificationStatus.SUSPENDED

    def test_un_dossier_deja_en_attente_y_reste(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Le cas nominal de l'inscription : on complète son dossier.

        Aucun effet de bord à attendre — en particulier, le livreur ne doit pas
        être basculé hors ligne à chaque pièce déposée alors qu'il n'a jamais
        pu se mettre en ligne.
        """
        courier.verification_status = VerificationStatus.PENDING
        courier.is_online = False
        courier.save()

        as_courier.post(_url(), {"vehicle_document": _piece("carte-grise.jpg")}, format="multipart")

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.PENDING

    def test_les_trois_pieces_partent_ensemble(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Le geste de l'écran : un seul envoi pour ce qu'on a sous la main."""
        reponse = as_courier.post(
            _url(),
            {
                "id_document": _piece("cni.jpg"),
                "licence_document": _piece("permis.jpg"),
                "vehicle_document": _piece("carte-grise.jpg"),
            },
            format="multipart",
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.id_document.name
        assert courier.licence_document.name
        assert courier.vehicle_document.name

    def test_un_depot_vide_est_refuse(self, as_courier: APIClient) -> None:
        """Sinon la réponse annoncerait un dépôt qui n'a rien déposé."""
        reponse = as_courier.post(_url(), {}, format="multipart")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_le_statut_ne_se_depose_pas(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """L1 — la pièce jointe ne sert pas de cheval de Troie.

        `DocumentsSerializer` est une liste blanche de trois champs : la clé
        surnuméraire est ignorée, elle n'atteint jamais le modèle.
        """
        courier.verification_status = VerificationStatus.PENDING
        courier.save()

        as_courier.post(
            _url(),
            {"id_document": _piece("cni.jpg"), "verification_status": "approved"},
            format="multipart",
        )

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.PENDING

    def test_le_dossier_d_autrui_est_hors_d_atteinte(
        self, as_courier: APIClient, courier: CourierProfile, restaurant: object
    ) -> None:
        """La route s'adresse par `me/` : il n'y a pas d'identifiant à viser."""
        autre = CourierProfile.objects.create(
            user=User.objects.create_user(
                "autre@elcorazon.test",
                "motdepasse",
                full_name="Yao Adjo",
                user_type=UserType.COURIER,
            ),
            restaurant=courier.restaurant,
            vehicle_type=VehicleType.BICYCLE,
            verification_status=VerificationStatus.APPROVED,
        )

        as_courier.post(_url(), {"id_document": _piece("cni.jpg")}, format="multipart")

        autre.refresh_from_db()
        assert not autre.id_document.name
        assert autre.verification_status == VerificationStatus.APPROVED

    def test_sans_jeton_rien_ne_passe(self) -> None:
        reponse = APIClient().post(_url(), {"id_document": _piece("cni.jpg")}, format="multipart")

        assert reponse.status_code in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }


class TestLectureDuDossier:
    """`GET /delivery/me/` — ce que le livreur relit de son propre dossier."""

    def test_les_numeros_de_pieces_sont_rendus(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Ils s'écrivaient déjà et ne se relisaient pas.

        Le back-office les saisit à l'embauche et les corrige ensuite ; aucune
        réponse ne les rendait. Son propre formulaire de correction ne pouvait
        donc pas préremplir les champs qu'il proposait de corriger.
        """
        courier.national_id_number = "TG-CNI-4417"
        courier.licence_number = "PC-2291-B"
        courier.save()

        reponse = as_courier.get(_url())

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["national_id_number"] == "TG-CNI-4417"
        assert reponse.data["licence_number"] == "PC-2291-B"


class TestCorrectionParLeLivreur:
    """`PATCH /delivery/me/` — et surtout, ce qu'il refuse."""

    def test_le_vehicule_se_corrige(self, as_courier: APIClient, courier: CourierProfile) -> None:
        reponse = as_courier.patch(
            _url(), {"vehicle_type": VehicleType.CAR, "vehicle_plate": "TG-4242-AB"}, format="json"
        )

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.vehicle_type == VehicleType.CAR
        assert courier.vehicle_plate == "TG-4242-AB"

    def test_les_numeros_de_pieces_se_corrigent(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        reponse = as_courier.patch(_url(), {"licence_number": "PC-2291-B"}, format="json")

        assert reponse.status_code == status.HTTP_200_OK
        courier.refresh_from_db()
        assert courier.licence_number == "PC-2291-B"

    def test_corriger_ne_rouvre_pas_l_instruction(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Une plaque qui perd un tiret ne suspend pas un livreur en tournée.

        C'est le **dépôt d'une pièce** qui rouvre l'examen (L5) : c'est la pièce
        qu'un instructeur lit, pas le champ texte à côté.
        """
        assert courier.verification_status == VerificationStatus.APPROVED

        as_courier.patch(_url(), {"vehicle_plate": "TG-4242-AB"}, format="json")

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.APPROVED
        assert courier.is_online is True

    def test_le_statut_de_verification_est_hors_dportee(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """L1 — sans quoi un livreur valide son propre recrutement."""
        courier.verification_status = VerificationStatus.PENDING
        courier.save()

        as_courier.patch(
            _url(),
            {"vehicle_plate": "TG-1111-AA", "verification_status": "approved"},
            format="json",
        )

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.PENDING

    def test_les_compteurs_sont_hors_de_portee(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """L4 — ce sont des agrégats de faits, pas des déclarations.

        Les rendre modifiables permettrait de se fabriquer une réputation, et
        `rating_average` décide de qui reçoit les courses.
        """
        as_courier.patch(
            _url(),
            {
                "vehicle_plate": "TG-1111-AA",
                "deliveries_completed": 900,
                "rating_average": "5.00",
                "rating_count": 900,
            },
            format="json",
        )

        courier.refresh_from_db()
        assert courier.deliveries_completed == 0
        assert float(courier.rating_average) == 0.0

    def test_la_disponibilite_ne_passe_pas_par_la(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """`is_online` a sa route, qui rend le dossier et donc `can_accept_orders`."""
        courier.is_online = False
        courier.save()

        as_courier.patch(_url(), {"vehicle_plate": "TG-1111-AA", "is_online": True}, format="json")

        courier.refresh_from_db()
        assert courier.is_online is False

    def test_le_rattachement_ne_se_change_pas(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Un livreur qui se transfère lui-même sort du périmètre de qui l'emploie."""
        origine = courier.restaurant_id

        as_courier.patch(
            _url(), {"vehicle_plate": "TG-1111-AA", "restaurant": "un-autre"}, format="json"
        )

        courier.refresh_from_db()
        assert courier.restaurant_id == origine

    def test_un_corps_vide_est_refuse(self, as_courier: APIClient) -> None:
        """Sinon l'écran annonce « enregistré » sur une requête sans écriture."""
        reponse = as_courier.patch(_url(), {}, format="json")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_vehicule_hors_enumeration_est_refuse(self, as_courier: APIClient) -> None:
        reponse = as_courier.patch(_url(), {"vehicle_type": "trottinette"}, format="json")

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_le_nom_reste_du_ressort_du_compte(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """`PATCH /auth/me/` porte déjà nom et téléphone, pour tous les comptes.

        Les dédoubler ici donnerait deux routes pour un même geste, qui
        divergeraient. La clé est donc ignorée, pas honorée.
        """
        origine = courier.user.full_name

        as_courier.patch(
            _url(), {"vehicle_plate": "TG-1111-AA", "full_name": "Nom Change"}, format="json"
        )

        courier.user.refresh_from_db()
        assert courier.user.full_name == origine
