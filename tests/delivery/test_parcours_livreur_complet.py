"""Le parcours d'un livreur, d'un bout à l'autre, par l'API seule.

## Ce que ce fichier vérifie, et pourquoi il n'est pas redondant

Chaque étape ci-dessous est déjà couverte isolément. Ce qui ne l'était pas,
c'est **la chaîne** — et c'est précisément dans un maillon de la chaîne que se
tenait le défaut le plus coûteux du parcours : le dépôt de pièces existait côté
serveur, l'instruction existait côté back-office, mais rien ne les reliait.
Aucune application ne savait téléverser une pièce, si bien que le dossier de
tout livreur restait vide, et qu'un dossier **refusé** ne repassait jamais en
instruction quoi qu'on y dépose. Les deux bouts étaient verts ; le trajet ne
passait pas.

Un test par étape ne l'aurait pas montré. Celui-ci suit un même livreur du
formulaire d'inscription au versement de sa première course, sans jamais écrire
en base autrement que par une requête HTTP — chaque `objects.get` n'y sert qu'à
constater.

## Le scénario

    candidature → code reçu → session ouverte → dossier vide
    → dépôt des trois pièces → instruction → **refus motivé**
    → correction d'une pièce → retour dans la file → validation
    → passage en ligne → course proposée → acceptée → récupérée
    → en route → livrée → gains crédités

Le refus au milieu n'est pas décoratif : c'est la boucle que le parcours ne
savait pas fermer, et le seul endroit où un livreur pouvait rester bloqué
indéfiniment sans qu'aucun écran ne le dise.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType, VerificationCode, VerificationPurpose
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

MOT_DE_PASSE = "MotDePasseSolide!42"
ADRESSE = "kodjo.candidat@elcorazon.test"


def _piece(nom: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(nom, b"\xff\xd8\xff\xe0 piece", content_type="image/jpeg")


def _code_connu(email: str, valeur: str = "123456") -> str:
    """Repose une empreinte connue : le code n'est pas stocké en clair.

    Même procédé que `tests/accounts/test_verification.py` — le serveur ne
    range que l'empreinte, et un test ne peut pas lire le courriel envoyé.
    """
    record = VerificationCode.objects.filter(
        sent_to=email,
        purpose=VerificationPurpose.ACCOUNT_VERIFICATION,
        consumed_at__isnull=True,
    ).first()
    assert record is not None, "aucun code vivant n'a été émis pour cette adresse"
    record.code_hash = make_password(valeur)
    record.save(update_fields=["code_hash"])
    return valeur


@pytest.fixture
def siege(restaurant: Restaurant) -> APIClient:
    """Le personnel qui instruit les dossiers et confie les courses.

    Trois permissions nommées, et pas une de plus : ce sont exactement celles
    que le scénario emprunte.
    """
    membre = User.objects.create_user(
        "instructeur@elcorazon.test",
        MOT_DE_PASSE,
        full_name="Afi Responsable",
        user_type=UserType.STAFF,
    )
    membre.roles.add(
        Role.objects.create(
            name="Instruction de la flotte",
            permissions=["couriers.read", "couriers.approve", "orders.assign_courier"],
        )
    )
    StaffMembership.objects.create(user=membre, restaurant=restaurant)

    client = APIClient()
    client.force_authenticate(membre)
    return client


class TestParcoursLivreurComplet:
    def test_de_la_candidature_au_premier_gain(
        self,
        restaurant: Restaurant,
        order: Order,
        siege: APIClient,
    ) -> None:
        candidat = APIClient()

        # ------------------------------------------------ 1. la candidature
        depot = candidat.post(
            reverse("v1:delivery:apply"),
            {
                "email": ADRESSE,
                "password": MOT_DE_PASSE,
                "full_name": "Kodjo Mensah",
                "phone": "+22890445566",
                "restaurant": restaurant.slug,
                "vehicle_type": VehicleType.MOTORCYCLE,
                "vehicle_plate": "TG-7788-CD",
            },
            format="json",
        )
        assert depot.status_code == status.HTTP_201_CREATED
        # Aucun jeton : la saisie du code n'est pas une étape que le client
        # pourrait sauter.
        assert "access" not in depot.data

        # ---------------------------------------- 2. le code ouvre la session
        session = candidat.post(
            reverse("v1:accounts:verify"),
            {"email": ADRESSE, "code": _code_connu(ADRESSE)},
            format="json",
        )
        assert session.status_code == status.HTTP_200_OK
        livreur = APIClient()
        livreur.credentials(HTTP_AUTHORIZATION=f"Bearer {session.data['access']}")

        # ------------------------------------- 3. le dossier existe, et il est vide
        dossier = livreur.get(reverse("v1:delivery:me"))
        assert dossier.status_code == status.HTTP_200_OK
        assert dossier.data["verification_status"] == VerificationStatus.PENDING
        # Le point de départ du défaut : trois emplacements vides, et jusqu'ici
        # aucun moyen de les remplir.
        assert dossier.data["id_document"] is None
        assert dossier.data["licence_document"] is None
        assert dossier.data["vehicle_document"] is None
        assert dossier.data["can_accept_orders"] is False

        # ------------------------------------------------ 4. le dépôt des pièces
        depose = livreur.post(
            reverse("v1:delivery:me"),
            {
                "id_document": _piece("cni.jpg"),
                "licence_document": _piece("permis.jpg"),
                "vehicle_document": _piece("carte-grise.jpg"),
            },
            format="multipart",
        )
        assert depose.status_code == status.HTTP_200_OK
        assert depose.data["id_document"]
        assert depose.data["licence_document"]
        assert depose.data["vehicle_document"]

        courier = CourierProfile.objects.get(user__email=ADRESSE)

        # ------------------------------- 5. le siège lit le dossier, et le refuse
        # Il apparaît dans la file : c'est `pending` que le back-office liste.
        file_attente = siege.get(
            reverse("v1:delivery:courier-list"), {"verification_status": "pending"}
        )
        assert file_attente.status_code == status.HTTP_200_OK
        assert str(courier.pk) in [ligne["id"] for ligne in file_attente.data["results"]]

        refus = siege.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.REJECTED, "notes": "Photo du permis illisible."},
            format="json",
        )
        assert refus.status_code == status.HTTP_200_OK

        # Le livreur lit le motif — sans lui, « Rejeté » ne dit pas quoi refaire.
        refuse = livreur.get(reverse("v1:delivery:me"))
        assert refuse.data["verification_status"] == VerificationStatus.REJECTED
        assert refuse.data["verification_notes"] == "Photo du permis illisible."

        # Et il ne peut pas travailler, même en se déclarant en ligne (L1).
        en_ligne = livreur.post(
            reverse("v1:delivery:me-online"), {"is_online": True}, format="json"
        )
        assert en_ligne.data["can_accept_orders"] is False

        # ------------------------------------------ 6. la correction, et son effet
        # **Le maillon qui manquait.** Redéposer laissait le dossier `rejected`,
        # donc hors de la file d'instruction : le livreur corrigeait dans le vide.
        correction = livreur.post(
            reverse("v1:delivery:me"),
            {"licence_document": _piece("permis-net.jpg")},
            format="multipart",
        )
        assert correction.status_code == status.HTTP_200_OK
        assert correction.data["verification_status"] == VerificationStatus.PENDING
        # Le reproche auquel il vient de répondre ne lui est plus opposé.
        assert correction.data["verification_notes"] == ""

        assert str(courier.pk) in [
            ligne["id"]
            for ligne in siege.get(
                reverse("v1:delivery:courier-list"), {"verification_status": "pending"}
            ).data["results"]
        ]

        # ------------------------------------------------- 7. la validation
        validation = siege.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.APPROVED, "notes": ""},
            format="json",
        )
        assert validation.status_code == status.HTTP_200_OK

        # ------------------------------------------------ 8. le passage en ligne
        # Le dépôt de pièces l'avait remis hors ligne : c'est une nouvelle
        # déclaration, et c'est le serveur qui dit si elle suffit.
        disponible = livreur.post(
            reverse("v1:delivery:me-online"), {"is_online": True}, format="json"
        )
        assert disponible.data["is_online"] is True
        assert disponible.data["can_accept_orders"] is True

        # ------------------------------------------------- 9. la course
        order.status = OrderStatus.READY
        order.save(update_fields=["status"])

        proposition = siege.post(
            reverse("v1:delivery:offer", args=[order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )
        assert proposition.status_code == status.HTTP_201_CREATED
        course = proposition.data["id"]

        # Elle apparaît dans ses propres courses, pas ailleurs.
        proposees = livreur.get(
            reverse("v1:delivery:assignment-list"), {"status": DeliveryStatus.OFFERED}
        )
        assert [ligne["id"] for ligne in proposees.data["results"]] == [course]

        acceptee = livreur.post(reverse("v1:delivery:assignment-accept", args=[course]))
        assert acceptee.status_code == status.HTTP_200_OK
        assert acceptee.data["status"] == DeliveryStatus.ACCEPTED
        # La rémunération est figée à l'acceptation : le barème peut changer,
        # ce qui est dû pour cette course ne change pas.
        assert acceptee.data["courier_fee"] is not None

        # --------------------------------- 10. les étapes, et la commande qui suit
        for etape, statut_commande in [
            (DeliveryStatus.PICKED_UP, OrderStatus.PICKED_UP),
            (DeliveryStatus.ON_THE_WAY, OrderStatus.ON_THE_WAY),
            (DeliveryStatus.DELIVERED, OrderStatus.DELIVERED),
        ]:
            avance = livreur.post(
                reverse("v1:delivery:assignment-status", args=[course]),
                {"status": etape, "reason": ""},
                format="json",
            )
            assert avance.status_code == status.HTTP_200_OK, avance.data
            assert avance.data["status"] == etape

            order.refresh_from_db()
            # La commande suit par projection déclarée côté serveur, jamais par
            # une écriture du client — c'est une projection à la main qui avait
            # produit C4.
            assert order.status == statut_commande

        # --------------------------------------------- 11. les gains, et le compteur
        gains = livreur.get(reverse("v1:delivery:me-earnings"))
        assert gains.status_code == status.HTTP_200_OK
        assert gains.data["today"]["deliveries"] == 1
        assert int(gains.data["today"]["earned"]["amount"]) > 0
        assert gains.data["lifetime"]["deliveries"] == 1

        courier.refresh_from_db()
        # L4 — le compteur n'est incrémenté qu'à la transition vers `delivered`,
        # qui est terminale : le graphe rend le rejeu inexprimable.
        assert courier.deliveries_completed == 1
        assert courier.total_earnings is not None

        assignment = Assignment.objects.get(pk=course)
        assert assignment.delivered_at is not None
