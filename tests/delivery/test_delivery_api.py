"""API de la livraison — invariants L1, L2, L4, L5.

Le test décisif est `test_deux_livreurs_ne_prennent_pas_la_meme_course` : l'API
précédente n'avait aucun verrou à l'acceptation, si bien que deux livreurs
faisaient le même trajet et qu'un seul était payé.
"""

from __future__ import annotations

from typing import Final

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_courier(courier: CourierProfile) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(courier.user)
    return separate


@pytest.fixture
def dispatcher(restaurant: Restaurant) -> User:
    """Personnel rattaché à l'établissement, muni des droits d'affectation."""
    member = User.objects.create_user(
        "dispatch@elcorazon.test", "motdepasse", full_name="Ama Dispatch", user_type=UserType.STAFF
    )
    member.roles.add(
        Role.objects.create(
            name="Dispatch",
            permissions=[
                "orders.assign_courier",
                "couriers.read",
                "couriers.approve",
                "couriers.suspend",
            ],
        )
    )
    StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def as_dispatcher(dispatcher: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(dispatcher)
    return separate


@pytest.fixture
def as_recruteur(restaurant: Restaurant) -> APIClient:
    """Personnel muni de `couriers.write` — le droit d'ouvrir un compte livreur.

    Distinct de `dispatcher` : affecter une course et embaucher ne sont pas le
    même geste, et les tests de cloisonnement ci-dessous ont besoin que les deux
    permissions soient séparables.
    """
    recruteur = User.objects.create_user(
        "recrute@elcorazon.test", "motdepasse", full_name="Afi Recrute", user_type=UserType.STAFF
    )
    recruteur.roles.add(
        Role.objects.create(
            name="Responsable flotte", permissions=["couriers.read", "couriers.write"]
        )
    )
    StaffMembership.objects.create(user=recruteur, restaurant=restaurant)

    separate = APIClient()
    separate.force_authenticate(recruteur)
    return separate


@pytest.fixture
def ready_order(order: Order) -> Order:
    """Commande confirmée, prête à être confiée à un livreur."""
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.READY)
    order.refresh_from_db()
    return order


#: Candidature de référence. L'adresse porte volontairement des majuscules —
#: elle doit ressortir normalisée, sans quoi deux comptes ne différant que par la
#: casse rendraient l'un des deux inaccessible.
CANDIDATURE: Final[dict[str, object]] = {
    "email": "Nouveau.Livreur@elcorazon.test",
    "password": "brochette-piment-2026",
    "full_name": "Kossi Mensah",
    "vehicle_type": VehicleType.MOTORCYCLE,
    "vehicle_plate": "TG-4412-AB",
}

NOUVEAU = "nouveau.livreur@elcorazon.test"


def candidature(restaurant: Restaurant, **overrides: object) -> dict[str, object]:
    return {**CANDIDATURE, "restaurant": restaurant.slug, **overrides}


def second_courier(
    restaurant: Restaurant, email: str = "livreur.b@elcorazon.test"
) -> CourierProfile:
    return CourierProfile.objects.create(
        user=User.objects.create_user(
            email, "motdepasse", full_name="Yao Adjo", user_type=UserType.COURIER
        ),
        restaurant=restaurant,
        vehicle_type=VehicleType.SCOOTER,
        verification_status=VerificationStatus.APPROVED,
        is_online=True,
    )


class TestDossierLivreur:
    def test_le_livreur_lit_son_dossier(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        response = as_courier.get(reverse("v1:delivery:me"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["can_accept_orders"] is True
        assert response.data["verification_status"] == VerificationStatus.APPROVED

    def test_un_client_n_a_pas_de_dossier_livreur(self, client: APIClient, customer: User) -> None:
        client.force_authenticate(customer)

        assert client.get(reverse("v1:delivery:me")).status_code == status.HTTP_403_FORBIDDEN

    def test_la_bascule_hors_ligne(self, as_courier: APIClient, courier: CourierProfile) -> None:
        response = as_courier.post(
            reverse("v1:delivery:me-online"), {"is_online": False}, format="json"
        )

        assert response.data["is_online"] is False
        assert response.data["can_accept_orders"] is False

    def test_un_dossier_non_valide_ne_peut_pas_se_mettre_en_ligne(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """L1 — et le refus est explicite : un livreur qui bascule
        l'interrupteur sans rien recevoir ne doit pas avoir à deviner
        pourquoi."""
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.PENDING, is_online=False
        )

        response = as_courier.post(
            reverse("v1:delivery:me-online"), {"is_online": True}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["verification_status"] == VerificationStatus.PENDING

    def test_le_livreur_n_ecrit_pas_son_propre_statut_de_dossier(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        """Le champ n'existe dans aucun sérialiseur d'entrée : un livreur qui
        pourrait l'écrire se validerait lui-même."""
        from apps.delivery.serializers import DocumentsSerializer, OnlineSerializer

        assert "verification_status" not in OnlineSerializer().fields
        assert "verification_status" not in DocumentsSerializer().fields


class TestProvisioningLivreur:
    """Ouverture d'un compte livreur par le personnel.

    Un livreur ne s'inscrit pas — décision actée en session le 2026-07-29 : son
    dossier le rattache à un établissement, et personne ne s'attribue un
    rattachement à soi-même.
    """

    def test_le_personnel_ouvre_un_compte_livreur(
        self, as_recruteur: APIClient, restaurant: Restaurant
    ) -> None:
        response = as_recruteur.post(
            reverse("v1:delivery:courier-list"), candidature(restaurant), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["verification_status"] == VerificationStatus.PENDING
        # Le compte existe mais ne travaille pas encore : aucune pièce n'a été
        # déposée, donc rien n'a été instruit.
        assert response.data["can_accept_orders"] is False
        assert response.data["restaurant"] == restaurant.slug

        cree = User.objects.get(email=NOUVEAU)
        assert cree.user_type == UserType.COURIER
        assert cree.courier_profile.vehicle_plate == "TG-4412-AB"

    def test_le_compte_et_le_dossier_naissent_ensemble(
        self, as_recruteur: APIClient, restaurant: Restaurant
    ) -> None:
        """Un `User` livreur sans `CourierProfile` se connecte à l'application
        et n'y trouve rien — c'est l'anomalie que `courier_of` traite en 404."""
        as_recruteur.post(
            reverse("v1:delivery:courier-list"), candidature(restaurant), format="json"
        )

        assert CourierProfile.objects.filter(user__email=NOUVEAU).exists()

    def test_le_dossier_ne_naît_jamais_valide_meme_si_la_requete_le_demande(
        self, as_recruteur: APIClient, restaurant: Restaurant
    ) -> None:
        """Sinon embaucher vaudrait valider, sans qu'aucune pièce n'ait été lue."""
        response = as_recruteur.post(
            reverse("v1:delivery:courier-list"),
            candidature(
                restaurant,
                verification_status=VerificationStatus.APPROVED,
                is_online=True,
                deliveries_completed=99,
            ),
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["verification_status"] == VerificationStatus.PENDING
        assert response.data["is_online"] is False
        assert response.data["deliveries_completed"] == 0

    def test_le_type_de_compte_ne_se_choisit_pas_dans_la_requete(
        self, as_recruteur: APIClient, restaurant: Restaurant
    ) -> None:
        """C'est ce champ qui décide de ce qu'un jeton autorise : l'accepter en
        entrée ferait de cette route un chemin d'escalade."""
        as_recruteur.post(
            reverse("v1:delivery:courier-list"),
            candidature(restaurant, user_type=UserType.STAFF, is_superuser=True),
            format="json",
        )

        cree = User.objects.get(email=NOUVEAU)
        assert cree.user_type == UserType.COURIER
        assert cree.is_superuser is False

    def test_le_livreur_cree_peut_se_connecter(
        self, as_recruteur: APIClient, client: APIClient, restaurant: Restaurant
    ) -> None:
        """Le geste n'a de valeur que si le compte est réellement utilisable
        depuis l'application livreur."""
        as_recruteur.post(
            reverse("v1:delivery:courier-list"), candidature(restaurant), format="json"
        )

        response = client.post(
            reverse("v1:accounts:login"),
            {"email": NOUVEAU, "password": CANDIDATURE["password"]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["user"]["user_type"] == UserType.COURIER

    def test_on_n_embauche_pas_pour_un_etablissement_hors_perimetre(
        self, as_recruteur: APIClient, zone, restaurant: Restaurant
    ) -> None:
        """`assert_in_scope` et non le filtre de lecture : l'objet n'existe pas
        encore, l'établissement arrive du corps de la requête."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara-embauche",
            zone=zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000019",
        )

        response = as_recruteur.post(
            reverse("v1:delivery:courier-list"), candidature(ailleurs), format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not User.objects.filter(email=NOUVEAU).exists()

    def test_lire_la_flotte_ne_donne_pas_le_droit_d_embaucher(
        self, client: APIClient, restaurant: Restaurant
    ) -> None:
        observateur = User.objects.create_user(
            "observateur@elcorazon.test",
            "motdepasse",
            full_name="Lit Seulement",
            user_type=UserType.STAFF,
        )
        observateur.roles.add(
            Role.objects.create(name="Lecture seule flotte", permissions=["couriers.read"])
        )
        StaffMembership.objects.create(user=observateur, restaurant=restaurant)
        client.force_authenticate(observateur)

        response = client.post(
            reverse("v1:delivery:courier-list"), candidature(restaurant), format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_une_adresse_deja_utilisee_est_refusee(
        self, as_recruteur: APIClient, restaurant: Restaurant, customer: User
    ) -> None:
        """L'adresse est l'identifiant de connexion : un doublon rendrait l'un
        des deux comptes inaccessible."""
        response = as_recruteur.post(
            reverse("v1:delivery:courier-list"),
            candidature(restaurant, email=customer.email.upper()),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "email" in response.data["errors"]

    def test_un_mot_de_passe_faible_est_refuse(
        self, as_recruteur: APIClient, restaurant: Restaurant
    ) -> None:
        """Les règles du projet valent aussi pour le mot de passe qu'un tiers
        choisit — c'est là qu'un « livreur123 » a le plus de chances
        d'apparaître."""
        response = as_recruteur.post(
            reverse("v1:delivery:courier-list"),
            candidature(restaurant, password="livreur123"),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "password" in response.data["errors"]

    def test_un_livreur_n_embauche_pas(self, as_courier: APIClient, restaurant: Restaurant) -> None:
        response = as_courier.post(
            reverse("v1:delivery:courier-list"), candidature(restaurant), format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestValidationDeDossier:
    def test_le_personnel_valide_un_dossier(
        self, as_dispatcher: APIClient, courier: CourierProfile
    ) -> None:
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.PENDING
        )

        response = as_dispatcher.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.APPROVED, "notes": "Pièces conformes"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["verification_status"] == VerificationStatus.APPROVED
        assert response.data["verified_at"] is not None

    def test_une_suspension_remet_le_livreur_hors_ligne(
        self, as_dispatcher: APIClient, courier: CourierProfile
    ) -> None:
        """Sans cela, un livreur suspendu resterait « en ligne » et
        continuerait d'apparaître dans les listes d'affectation."""
        response = as_dispatcher.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.SUSPENDED},
            format="json",
        )

        assert response.data["is_online"] is False
        assert response.data["can_accept_orders"] is False

    def test_on_ne_suspend_pas_un_dossier_jamais_valide(
        self, as_dispatcher: APIClient, courier: CourierProfile
    ) -> None:
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.PENDING
        )

        response = as_dispatcher.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.SUSPENDED},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "illegal_transition"

    def test_un_livreur_ne_valide_pas_un_dossier(
        self, as_courier: APIClient, courier: CourierProfile
    ) -> None:
        response = as_courier.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.APPROVED},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestDeuxPermissionsPourDeuxGestes:
    """`couriers.approve` instruit un dossier, `couriers.suspend` retire du service.

    Les deux gestes n'ont ni la même urgence ni le même auteur : l'instruction
    se fait au calme sur pièces, la suspension se décide un samedi soir après
    un incident. Les confondre reviendrait à donner le second pouvoir à toute
    personne chargée du premier.
    """

    @staticmethod
    def _agent(restaurant: Restaurant, email: str, *permissions: str) -> APIClient:
        member = User.objects.create_user(
            email, "motdepasse", full_name="Agent", user_type=UserType.STAFF
        )
        member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
        StaffMembership.objects.create(user=member, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(member)
        return client

    def test_instruire_ne_donne_pas_le_droit_de_suspendre(
        self, restaurant: Restaurant, courier: CourierProfile
    ) -> None:
        client = self._agent(
            restaurant, "instructeur@elcorazon.test", "couriers.read", "couriers.approve"
        )

        response = client.post(
            reverse("v1:delivery:courier-verification", args=[courier.pk]),
            {"status": VerificationStatus.SUSPENDED},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.APPROVED

    def test_suspendre_ne_donne_pas_le_droit_d_instruire(
        self, restaurant: Restaurant, courier: CourierProfile
    ) -> None:
        """La route accepte l'une ou l'autre permission — sinon un compte
        n'ayant que `couriers.suspend` ne l'atteindrait pas — et c'est le
        statut demandé qui départage."""
        client = self._agent(
            restaurant, "astreinte@elcorazon.test", "couriers.read", "couriers.suspend"
        )
        url = reverse("v1:delivery:courier-verification", args=[courier.pk])

        suspension = client.post(url, {"status": VerificationStatus.SUSPENDED}, format="json")
        validation = client.post(url, {"status": VerificationStatus.APPROVED}, format="json")

        assert suspension.status_code == status.HTTP_200_OK
        assert validation.status_code == status.HTTP_403_FORBIDDEN


class TestAffectation:
    def test_le_personnel_propose_une_course(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile
    ) -> None:
        response = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == DeliveryStatus.OFFERED
        assert response.data["order_reference"] == ready_order.reference

    def test_un_livreur_non_valide_ne_recoit_rien(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile
    ) -> None:
        """L1 — relu depuis le dossier, jamais déduit d'un jeton."""
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.PENDING
        )
        courier.refresh_from_db()

        response = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Assignment.objects.count() == 0

    def test_une_commande_en_attente_n_est_pas_confiable(
        self, as_dispatcher: APIClient, order: Order, courier: CourierProfile
    ) -> None:
        """Proposer avant la confirmation mobiliserait un livreur pour une
        commande encore annulable sans frais."""
        response = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["current_status"] == OrderStatus.PENDING

    def test_une_seule_course_active_par_commande(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile, restaurant
    ) -> None:
        """L2 — et le refus est métier, pas une violation d'intégrité en 500."""
        as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )

        response = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(second_courier(restaurant).pk)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert Assignment.objects.count() == 1

    def test_un_refus_libere_la_commande(
        self,
        as_dispatcher: APIClient,
        as_courier: APIClient,
        ready_order: Order,
        courier: CourierProfile,
        restaurant: Restaurant,
    ) -> None:
        """Le cas nominal de la flotte : on décline, on propose à un autre."""
        premiere = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        ).data
        as_courier.post(
            reverse("v1:delivery:assignment-decline", args=[premiere["id"]]),
            {"reason": "Trop loin"},
            format="json",
        )

        seconde = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(second_courier(restaurant).pk)},
            format="json",
        )

        assert seconde.status_code == status.HTTP_201_CREATED

    def test_les_livreurs_disponibles_sont_tries_par_proximite(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile
    ) -> None:
        response = as_dispatcher.get(
            reverse("v1:delivery:courier-available", args=[ready_order.pk])
        )

        assert response.status_code == status.HTTP_200_OK
        assert [c["id"] for c in response.data] == [str(courier.pk)]

    def test_un_livreur_hors_ligne_n_est_pas_proposable(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile
    ) -> None:
        CourierProfile.objects.filter(pk=courier.pk).update(is_online=False)

        response = as_dispatcher.get(
            reverse("v1:delivery:courier-available", args=[ready_order.pk])
        )

        assert response.data == []


class TestAcceptation:
    @pytest.fixture
    def offered(
        self, as_dispatcher: APIClient, ready_order: Order, courier: CourierProfile
    ) -> Assignment:
        as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )
        return Assignment.objects.get()

    def test_le_livreur_accepte_et_sa_remuneration_est_figee(
        self, as_courier: APIClient, offered: Assignment, ready_order: Order
    ) -> None:
        """Le barème peut changer d'ici la livraison ; ce qui est dû pour cette
        course ne change plus."""
        response = as_courier.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == DeliveryStatus.ACCEPTED
        # 80 % des 500 F de frais de livraison de la commande de test.
        assert response.data["courier_fee"] == {"amount": "400", "currency": XOF}

    def test_une_livraison_offerte_au_client_reste_payee_au_livreur(
        self, as_courier: APIClient, offered: Assignment, ready_order: Order
    ) -> None:
        """Le défaut corrigé : la commission se calculait sur ce que le client
        avait payé. Sous franco, ce montant vaut zéro — et le livreur roulait
        gratuitement pour une remise qu'il n'avait pas décidée."""
        Order.objects.filter(pk=ready_order.pk).update(
            delivery_fee_minor=0, delivery_fee_gross_minor=500, delivery_fee_gross_currency=XOF
        )

        response = as_courier.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        assert response.data["courier_fee"] == {"amount": "400", "currency": XOF}

    def test_une_commande_anterieure_au_champ_retombe_sur_le_montant_facture(
        self, as_courier: APIClient, offered: Assignment, ready_order: Order
    ) -> None:
        """Les commandes créées avant `delivery_fee_gross` n'ont que le montant
        facturé : c'était alors la seule valeur connue."""
        Order.objects.filter(pk=ready_order.pk).update(
            delivery_fee_gross_minor=None, delivery_fee_gross_currency=None
        )

        response = as_courier.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        assert response.data["courier_fee"] == {"amount": "400", "currency": XOF}

    def test_deux_livreurs_ne_prennent_pas_la_meme_course(
        self, client: APIClient, offered: Assignment, restaurant: Restaurant
    ) -> None:
        """L2 — la course est proposée à un seul ; un autre livreur qui
        connaîtrait son identifiant ne peut pas la lui prendre."""
        autre = second_courier(restaurant)
        client.force_authenticate(autre.user)

        response = client.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        offered.refresh_from_db()
        assert offered.status == DeliveryStatus.OFFERED

    def test_un_livreur_suspendu_entre_temps_n_accepte_pas(
        self, as_courier: APIClient, offered: Assignment, courier: CourierProfile
    ) -> None:
        """Le dossier est relu à l'acceptation, pas seulement à l'offre."""
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.SUSPENDED
        )

        response = as_courier.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_la_commande_reste_prete_a_l_acceptation(
        self, as_courier: APIClient, offered: Assignment, ready_order: Order
    ) -> None:
        """C4 — `accepted` ne projette rien : c'est en voulant le projeter que
        l'ancien code écrivait un statut hors énumération."""
        as_courier.post(reverse("v1:delivery:assignment-accept", args=[offered.pk]))

        ready_order.refresh_from_db()
        assert ready_order.status == OrderStatus.READY


class TestProgression:
    @pytest.fixture
    def accepted(
        self,
        as_dispatcher: APIClient,
        as_courier: APIClient,
        ready_order: Order,
        courier: CourierProfile,
    ) -> Assignment:
        as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(courier.pk)},
            format="json",
        )
        assignment = Assignment.objects.get()
        as_courier.post(reverse("v1:delivery:assignment-accept", args=[assignment.pk]))
        assignment.refresh_from_db()
        return assignment

    def avance(self, client: APIClient, assignment: Assignment, cible: str) -> object:
        return client.post(
            reverse("v1:delivery:assignment-status", args=[assignment.pk]),
            {"status": cible},
            format="json",
        )

    def test_l_enlevement_fait_avancer_la_commande(
        self, as_courier: APIClient, accepted: Assignment, ready_order: Order
    ) -> None:
        response = self.avance(as_courier, accepted, DeliveryStatus.PICKED_UP)

        assert response.status_code == status.HTTP_200_OK
        ready_order.refresh_from_db()
        assert ready_order.status == OrderStatus.PICKED_UP

    def test_la_livraison_boucle_la_commande_et_credite_le_livreur(
        self, as_courier: APIClient, accepted: Assignment, ready_order: Order, courier
    ) -> None:
        """L4 — les compteurs et les gains sont incrémentés à la transition
        effective, une seule fois."""
        for cible in (
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.ON_THE_WAY,
            DeliveryStatus.DELIVERED,
        ):
            self.avance(as_courier, accepted, cible)

        ready_order.refresh_from_db()
        courier.refresh_from_db()

        assert ready_order.status == OrderStatus.DELIVERED
        assert courier.deliveries_completed == 1
        assert courier.total_earnings == Money(400, XOF)

    def test_rejouer_la_livraison_ne_recredite_pas(
        self, as_courier: APIClient, accepted: Assignment, courier: CourierProfile
    ) -> None:
        """C3 — rejouer `delivered` réincrémentait les compteurs dans
        l'implémentation précédente. Le graphe acyclique le rend
        inexprimable."""
        for cible in (
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.ON_THE_WAY,
            DeliveryStatus.DELIVERED,
        ):
            self.avance(as_courier, accepted, cible)

        rejeu = self.avance(as_courier, accepted, DeliveryStatus.DELIVERED)
        courier.refresh_from_db()

        assert rejeu.status_code == status.HTTP_200_OK
        assert courier.deliveries_completed == 1
        assert courier.total_earnings == Money(400, XOF)

    def test_un_saut_d_etape_est_refuse(self, as_courier: APIClient, accepted: Assignment) -> None:
        response = self.avance(as_courier, accepted, DeliveryStatus.DELIVERED)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "illegal_transition"

    def test_une_commande_annulee_arrete_la_course(
        self, as_courier: APIClient, accepted: Assignment, ready_order: Order
    ) -> None:
        """Sans cette garde, un livreur continuerait à faire avancer — et à se
        faire créditer — une course dont le repas ne partira jamais."""
        Order.objects.filter(pk=ready_order.pk).update(status=OrderStatus.CANCELLED)

        response = self.avance(as_courier, accepted, DeliveryStatus.PICKED_UP)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["order_status"] == OrderStatus.CANCELLED

    def test_annuler_une_course_ne_annule_pas_la_commande(
        self, as_dispatcher: APIClient, accepted: Assignment, ready_order: Order
    ) -> None:
        """Une crevaison n'annule pas le repas : la commande reste prête et
        repart chez un autre livreur."""
        response = as_dispatcher.post(
            reverse("v1:delivery:assignment-cancel", args=[accepted.pk]),
            {"reason": "Panne de moto"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        ready_order.refresh_from_db()
        assert ready_order.status == OrderStatus.READY

    def test_une_course_annulee_libere_la_commande(
        self,
        as_dispatcher: APIClient,
        accepted: Assignment,
        ready_order: Order,
        restaurant: Restaurant,
    ) -> None:
        as_dispatcher.post(
            reverse("v1:delivery:assignment-cancel", args=[accepted.pk]),
            {"reason": "Panne"},
            format="json",
        )

        reaffectation = as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(second_courier(restaurant).pk)},
            format="json",
        )

        assert reaffectation.status_code == status.HTTP_201_CREATED


class TestCloisonnement:
    def test_le_livreur_ne_voit_que_ses_courses(
        self,
        as_courier: APIClient,
        as_dispatcher: APIClient,
        ready_order: Order,
        restaurant: Restaurant,
    ) -> None:
        autre = second_courier(restaurant)
        as_dispatcher.post(
            reverse("v1:delivery:offer", args=[ready_order.pk]),
            {"courier": str(autre.pk)},
            format="json",
        )

        response = as_courier.get(reverse("v1:delivery:assignment-list"))

        assert response.data["count"] == 0

    def test_le_personnel_d_un_autre_etablissement_ne_voit_pas_la_flotte(
        self, client: APIClient, courier: CourierProfile, zone
    ) -> None:
        """Le rattachement dit sur quoi ; la permission dit seulement quoi
        faire. Sans lui, un opérateur de Kara lit la flotte de Lomé."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=zone,
            address="Kara",
            location=courier.restaurant.location,
            phone="+22890000009",
        )
        etranger = User.objects.create_user(
            "kara@elcorazon.test", "motdepasse", full_name="Kara Staff", user_type=UserType.STAFF
        )
        etranger.roles.add(
            Role.objects.create(name="Lecture flotte", permissions=["couriers.read"])
        )
        StaffMembership.objects.create(user=etranger, restaurant=ailleurs)
        client.force_authenticate(etranger)

        response = client.get(reverse("v1:delivery:courier-list"))

        assert response.data["count"] == 0

    def test_un_membre_du_personnel_sans_rattachement_ne_voit_rien(
        self, client: APIClient, courier: CourierProfile
    ) -> None:
        """L'ensemble vide est le bon défaut : un oubli de configuration
        produit une panne visible, pas un accès trop large et silencieux."""
        orphelin = User.objects.create_user(
            "orphelin@elcorazon.test",
            "motdepasse",
            full_name="Sans Poste",
            user_type=UserType.STAFF,
        )
        orphelin.roles.add(Role.objects.create(name="Lecture", permissions=["couriers.read"]))
        client.force_authenticate(orphelin)

        assert client.get(reverse("v1:delivery:courier-list")).data["count"] == 0
