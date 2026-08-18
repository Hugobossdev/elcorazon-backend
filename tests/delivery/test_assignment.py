"""Invariants de la livraison — L1, L2, L4, L5.

L2 est le plus important : l'implémentation précédente n'avait aucun verrou à
l'acceptation, si bien que deux livreurs pouvaient prendre la même course. Le
service posera un `SELECT FOR UPDATE`, mais c'est la contrainte de base qui est
testée ici — elle tient encore quand le service est contourné.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User, UserType
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.services import CourierApplication, CourierService
from apps.delivery.states import (
    DELIVERY_MACHINE,
    ORDER_STATUS_PROJECTION,
    VERIFICATION_MACHINE,
    DeliveryStatus,
    VerificationStatus,
)
from apps.orders.states import ORDER_MACHINE
from apps.restaurants.models import Restaurant

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


class TestEligibilite:
    """L1 — seul un dossier validé et en ligne prend une course."""

    def test_le_cas_nominal(self, courier: CourierProfile) -> None:
        assert courier.can_accept_orders

    @pytest.mark.parametrize(
        "status",
        [VerificationStatus.PENDING, VerificationStatus.REJECTED, VerificationStatus.SUSPENDED],
    )
    def test_un_dossier_non_valide_est_ecarte(self, courier: CourierProfile, status: str) -> None:
        """L'ancien code ignorait `verification_status` : un livreur non validé
        prenait des courses."""
        courier.verification_status = status
        assert not courier.can_accept_orders

    def test_un_livreur_hors_ligne_est_ecarte(self, courier: CourierProfile) -> None:
        courier.is_online = False
        assert not courier.can_accept_orders

    def test_un_compte_desactive_est_ecarte(self, courier: CourierProfile) -> None:
        """Désactiver le compte doit suffire — sans avoir à penser aussi à
        repasser le dossier ou à le mettre hors ligne."""
        courier.user.is_active = False
        assert not courier.can_accept_orders


class TestExclusiviteDeLaCourse:
    """L2 — une seule course active par commande, garantie par la base."""

    def test_deux_courses_actives_sont_impossibles(self, order, courier, restaurant) -> None:
        Assignment.objects.create(order=order, courier=courier)

        second = CourierProfile.objects.create(
            user=User.objects.create_user(
                "livreur2@elcorazon.test",
                "motdepasse",
                full_name="Yao Adjo",
                user_type=UserType.COURIER,
            ),
            restaurant=restaurant,
            vehicle_type=VehicleType.SCOOTER,
            verification_status=VerificationStatus.APPROVED,
            is_online=True,
        )

        with pytest.raises(IntegrityError, match="one_active_assignment"), transaction.atomic():
            Assignment.objects.create(order=order, courier=second)

    def test_une_course_refusee_libere_la_commande(self, order, courier, restaurant) -> None:
        """Le cas métier normal : un livreur décline, on propose à un autre."""
        first = Assignment.objects.create(order=order, courier=courier)
        Assignment.objects.filter(pk=first.pk).update(status=DeliveryStatus.DECLINED)

        second = CourierProfile.objects.create(
            user=User.objects.create_user(
                "livreur3@elcorazon.test",
                "motdepasse",
                full_name="Afi Sena",
                user_type=UserType.COURIER,
            ),
            restaurant=restaurant,
            vehicle_type=VehicleType.BICYCLE,
            verification_status=VerificationStatus.APPROVED,
            is_online=True,
        )

        Assignment.objects.create(order=order, courier=second)
        assert order.assignments.count() == 2

    def test_une_course_livree_ne_bloque_pas_une_reaffectation(self, order, courier) -> None:
        """Le statut terminal sort de la contrainte : un retour ultérieur —
        second passage, litige — reste possible sans toucher au schéma."""
        first = Assignment.objects.create(order=order, courier=courier)
        Assignment.objects.filter(pk=first.pk).update(status=DeliveryStatus.DELIVERED)

        Assignment.objects.create(order=order, courier=courier)


class TestEnumerationDesStatuts:
    def test_tous_les_statuts_declares_sont_acceptes(self, order, courier) -> None:
        assignment = Assignment.objects.create(order=order, courier=courier)
        for status in DELIVERY_MACHINE.states:
            Assignment.objects.filter(pk=assignment.pk).update(status=status)

    def test_un_statut_hors_enumeration_est_rejete(self, order, courier) -> None:
        assignment = Assignment.objects.create(order=order, courier=courier)
        with pytest.raises(IntegrityError, match="assignment_status_in_enum"), transaction.atomic():
            Assignment.objects.filter(pk=assignment.pk).update(status="en_route")


class TestProjectionVersLaCommande:
    """C4 — la projection est déclarée, pas écrite à la main."""

    def test_toutes_les_cibles_existent_dans_la_machine_de_commande(self) -> None:
        assert set(ORDER_STATUS_PROJECTION.values()) <= ORDER_MACHINE.states

    def test_les_etapes_internes_ne_projettent_rien(self) -> None:
        """`offered`, `accepted` et `declined` sont des événements
        d'affectation : la commande reste `ready` tant que le repas n'est pas
        parti. C'est en voulant projeter `accepted` que l'ancien code écrivait
        un statut inexistant."""
        for interne in (DeliveryStatus.OFFERED, DeliveryStatus.ACCEPTED, DeliveryStatus.DECLINED):
            assert interne not in ORDER_STATUS_PROJECTION


class TestMonotonieDeLaCourse:
    """L4 — les compteurs ne peuvent pas être réincrémentés."""

    def test_livree_est_terminal(self) -> None:
        assert DELIVERY_MACHINE.is_terminal(DeliveryStatus.DELIVERED)

    def test_aucun_retour_en_arriere(self) -> None:
        for state in DELIVERY_MACHINE.states:
            assert not DELIVERY_MACHINE.can(DeliveryStatus.DELIVERED, state)


class TestDossierLivreur:
    """L5 — le dossier, lui, est délibérément cyclique."""

    def test_modifier_ses_pieces_apres_validation_remet_en_attente(self) -> None:
        assert VERIFICATION_MACHINE.can(VerificationStatus.APPROVED, VerificationStatus.PENDING)

    def test_un_dossier_rejete_peut_etre_represente(self) -> None:
        assert VERIFICATION_MACHINE.can(VerificationStatus.REJECTED, VerificationStatus.PENDING)

    def test_un_statut_hors_enumeration_est_rejete(self, courier: CourierProfile) -> None:
        with pytest.raises(IntegrityError, match="courier_verification"), transaction.atomic():
            CourierProfile.objects.filter(pk=courier.pk).update(verification_status="valide")


class TestOuvertureDeCompte:
    """Le compte et le dossier, ou ni l'un ni l'autre."""

    def test_l_echec_du_dossier_ne_laisse_pas_de_compte_orphelin(
        self, restaurant: Restaurant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sans la transaction, l'échec laisserait un compte de type livreur sans
        dossier : quelqu'un qui se connecte à l'application livreur et n'y trouve
        rien, avec une adresse désormais prise qu'on ne peut pas réutiliser pour
        recommencer."""

        def echoue(*args: object, **kwargs: object) -> CourierProfile:
            raise RuntimeError("stockage indisponible")

        monkeypatch.setattr(CourierProfile.objects, "create", echoue)

        with pytest.raises(RuntimeError):
            CourierService.provision(
                application=CourierApplication(
                    email="orphelin@elcorazon.test",
                    password="brochette-piment-2026",
                    full_name="Compte Orphelin",
                    restaurant=restaurant,
                    vehicle_type=VehicleType.BICYCLE,
                )
            )

        assert not User.objects.filter(email="orphelin@elcorazon.test").exists()
