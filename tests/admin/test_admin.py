"""Back-office — ce qu'il montre, et surtout ce qu'il refuse.

Un back-office est le chemin le plus court pour contourner ses propres règles.
Django admin propose par défaut un formulaire d'édition sur chaque champ : une
liste déroulante sur `status` suffirait à écrire `delivered` sur une commande
jamais partie, sans machine à états, sans journal, sans créditer le livreur.
Ce serait rouvrir C3 et C4 par la porte de service.

Cette suite vérifie donc deux choses de nature différente : que les écrans
s'affichent — un `list_display` mal orthographié ne se voit qu'à l'ouverture —
et que les portes dérobées sont fermées.
"""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import site
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.catalog.models import MenuItem
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import VerificationStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.restaurants.models import Restaurant
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


@pytest.fixture
def admin_user() -> User:
    return User.objects.create_superuser("admin@elcorazon.test", "MotDePasseSolide!42")


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


def changelist_url(model: type) -> str:
    meta = model._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")


def change_url(instance: object) -> str:
    meta = instance._meta  # type: ignore[attr-defined]
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[instance.pk])  # type: ignore[attr-defined]


class TestTousLesEcransSAffichent:
    """Un `list_display` fautif ne se voit qu'à l'ouverture de la page.

    `django check` attrape les erreurs de configuration déclarées ; il
    n'exécute pas les colonnes calculées ni les `select_related`. Ouvrir chaque
    liste est le seul moyen de savoir qu'elles fonctionnent.
    """

    @pytest.mark.parametrize("model", sorted(site._registry, key=lambda m: m.__name__))
    def test_la_liste_s_ouvre(self, admin_client: Client, model: type) -> None:
        response = admin_client.get(changelist_url(model))

        assert response.status_code == 200, f"{model.__name__} : {response.status_code}"

    def test_la_fiche_d_une_commande_s_ouvre(self, admin_client: Client, order: Order) -> None:
        """La plus riche du back-office : deux inlines, quatre montants
        calculés et une colonne de transitions possibles."""
        assert admin_client.get(change_url(order)).status_code == 200

    def test_la_fiche_d_un_livreur_s_ouvre(
        self, admin_client: Client, courier: CourierProfile
    ) -> None:
        assert admin_client.get(change_url(courier)).status_code == 200

    def test_la_fiche_d_un_article_s_ouvre(self, admin_client: Client, menu_item: MenuItem) -> None:
        assert admin_client.get(change_url(menu_item)).status_code == 200

    def test_la_fiche_d_un_restaurant_s_ouvre(
        self, admin_client: Client, restaurant: Restaurant
    ) -> None:
        assert admin_client.get(change_url(restaurant)).status_code == 200


class TestStatutsNonEditables:
    """La règle qui structure tout le back-office."""

    def test_une_commande_n_a_pas_de_formulaire(self, admin_client: Client, order: Order) -> None:
        """Consultable, jamais modifiable par formulaire : ce qui doit changer
        passe par une action, donc par le service."""
        from apps.orders.models import Order as OrderModel

        assert site._registry[OrderModel].has_change_permission(None) is False  # type: ignore[arg-type]

    def test_le_statut_de_commande_est_en_lecture_seule(self) -> None:
        from apps.orders.models import Order as OrderModel

        assert "status" in site._registry[OrderModel].readonly_fields

    def test_le_statut_de_dossier_livreur_est_en_lecture_seule(self) -> None:
        """Un formulaire validerait un dossier sans horodater la décision, sans
        l'attribuer, et sans remettre hors ligne un livreur suspendu."""
        assert "verification_status" in site._registry[CourierProfile].readonly_fields

    def test_les_montants_de_commande_sont_en_lecture_seule(self) -> None:
        """C2 — recomposés serveur. Un total saisi à la main serait un total
        faux qui a l'air juste."""
        from apps.orders.models import Order as OrderModel

        readonly = site._registry[OrderModel].readonly_fields
        for champ in ("subtotal_display", "total_display", "delivery_fee_display"):
            assert champ in readonly


class TestActionsQuiPassentParLeService:
    def test_confirmer_une_commande_ecrit_le_journal(
        self, admin_client: Client, order: Order, admin_user: User
    ) -> None:
        """Preuve que l'action ne contourne pas la machine : l'événement de
        transition est écrit, avec son auteur."""
        response = admin_client.post(
            changelist_url(Order),
            {"action": "passer_en_confirmed", "_selected_action": [str(order.pk)]},
            follow=True,
        )

        assert response.status_code == 200
        order.refresh_from_db()
        assert order.status == OrderStatus.CONFIRMED

        evenement = order.status_events.get()
        assert evenement.to_status == OrderStatus.CONFIRMED
        assert evenement.actor == admin_user

    def test_une_transition_impossible_est_refusee_et_signalee(
        self, admin_client: Client, order: Order
    ) -> None:
        """La machine décide ici comme dans l'API. L'action ne plante pas :
        elle rapporte ce qu'elle n'a pas pu faire."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.DELIVERED)

        response = admin_client.post(
            changelist_url(Order),
            {"action": "passer_en_confirmed", "_selected_action": [str(order.pk)]},
            follow=True,
        )

        assert response.status_code == 200
        order.refresh_from_db()
        assert order.status == OrderStatus.DELIVERED

    def test_valider_un_dossier_horodate_la_decision(
        self, admin_client: Client, courier: CourierProfile, admin_user: User
    ) -> None:
        CourierProfile.objects.filter(pk=courier.pk).update(
            verification_status=VerificationStatus.PENDING
        )

        admin_client.post(
            changelist_url(CourierProfile),
            {"action": "marquer_approved", "_selected_action": [str(courier.pk)]},
            follow=True,
        )

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.APPROVED
        assert courier.verified_by == admin_user
        assert courier.verified_at is not None

    def test_suspendre_remet_le_livreur_hors_ligne(
        self, admin_client: Client, courier: CourierProfile
    ) -> None:
        """Sans cela, un livreur suspendu resterait « en ligne » et
        continuerait d'apparaître dans les listes d'affectation."""
        admin_client.post(
            changelist_url(CourierProfile),
            {"action": "marquer_suspended", "_selected_action": [str(courier.pk)]},
            follow=True,
        )

        courier.refresh_from_db()
        assert courier.verification_status == VerificationStatus.SUSPENDED
        assert courier.is_online is False

    def test_revoquer_les_sessions_d_un_compte(self, admin_client: Client, customer: User) -> None:
        """T2 — le geste que réclame un client qui signale un accès suspect."""
        from apps.accounts.services import AuthService

        AuthService.issue_tokens(customer)

        response = admin_client.post(
            changelist_url(User),
            {"action": "revoke_sessions", "_selected_action": [str(customer.pk)]},
            follow=True,
        )

        assert response.status_code == 200

    def test_desactiver_un_compte(self, admin_client: Client, customer: User) -> None:
        admin_client.post(
            changelist_url(User),
            {"action": "deactivate", "_selected_action": [str(customer.pk)]},
            follow=True,
        )

        customer.refresh_from_db()
        assert customer.is_active is False


class TestPortesFermees:
    """Ce que le back-office ne doit surtout pas permettre."""

    def test_un_encaissement_ne_se_saisit_pas(self, admin_client: Client, order: Order) -> None:
        """P2 — la faille la plus grave de l'implémentation précédente était
        de pouvoir se déclarer payé. Son correctif d'alors restreignait
        l'action aux administrateurs, c'est-à-dire la laissait ouverte ici.
        """
        transaction = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="ADMIN-TEST",
            amount=Money(4_000, XOF),
            status=PaymentStatus.PENDING,
        )

        admin_transaction = site._registry[Transaction]
        assert admin_transaction.has_add_permission(None) is False  # type: ignore[arg-type]
        assert admin_transaction.has_change_permission(None) is False  # type: ignore[arg-type]
        assert admin_transaction.has_delete_permission(None) is False  # type: ignore[arg-type]
        assert transaction.status == PaymentStatus.PENDING

    def test_une_commande_ne_se_supprime_pas(self) -> None:
        """Écriture comptable : la supprimer ferait disparaître un chiffre
        d'affaires et les lignes qui l'expliquent."""
        from apps.orders.models import Order as OrderModel

        assert site._registry[OrderModel].has_delete_permission(None) is False  # type: ignore[arg-type]

    def test_une_course_ne_se_cree_pas_a_la_main(self) -> None:
        """Elle contournerait L1 et L2 : un livreur non validé affecté à une
        commande qui a déjà une course active."""
        assert site._registry[Assignment].has_add_permission(None) is False  # type: ignore[arg-type]

    def test_un_avis_ne_se_reecrit_pas(self) -> None:
        """Corriger le texte d'un avis reviendrait à faire dire à un client ce
        qu'il n'a pas écrit."""
        from apps.catalog.models import Review

        assert site._registry[Review].has_change_permission(None) is False  # type: ignore[arg-type]

    def test_un_achat_verifie_ne_se_declare_pas(self) -> None:
        """S1 — le champ existe précisément pour que personne ne décide qui a
        droit à la mention « achat vérifié »."""
        from apps.catalog.models import VerifiedPurchase

        assert site._registry[VerifiedPurchase].has_add_permission(None) is False  # type: ignore[arg-type]

    def test_un_role_systeme_ne_se_supprime_pas(self) -> None:
        """`Super Admin` retiré par mégarde, et plus personne ne peut rendre
        ses droits à qui que ce soit."""
        from apps.accounts.models import Role

        systeme = Role.objects.create(name="Système test", is_system=True)
        ordinaire = Role.objects.create(name="Ordinaire test")

        admin_role = site._registry[Role]
        assert admin_role.has_delete_permission(None, systeme) is False  # type: ignore[arg-type]
        assert admin_role.has_delete_permission(None, ordinaire) is True  # type: ignore[arg-type]


class TestAccesAuBackOffice:
    def test_un_client_n_entre_pas(self, customer: User) -> None:
        """`is_staff` est dérivé du type de compte : un client authentifié est
        redirigé vers l'écran de connexion, pas accueilli."""
        client = Client()
        client.force_login(customer)

        response = client.get(reverse("admin:index"))

        assert response.status_code == 302

    def test_un_livreur_non_plus(self, courier_user: User) -> None:
        client = Client()
        client.force_login(courier_user)

        assert client.get(reverse("admin:index")).status_code == 302
