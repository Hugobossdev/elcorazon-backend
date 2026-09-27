"""Qui est prévenu de quoi — les abonnés aux événements de domaine.

Cette suite couvre ce qui manquait au module : le **personnel** n'était prévenu
de rien, un **paiement refusé** ne disait rien à personne, et l'acceptation
d'une course par un livreur laissait le client sans nouvelle entre « confirmée »
et « en route ».

Trois tests portent le poids du fichier :

* `test_un_client_ne_recoit_jamais_une_alerte_du_personnel` — la séparation des
  publics, qui est la promesse de sécurité de ce module ;
* `test_seul_le_personnel_de_l_etablissement_est_prevenu` — le cloisonnement,
  qui vaut ici exactement ce qu'il vaut dans les vues ;
* `test_un_paiement_refuse_previent_les_deux_publics` — le seul événement qui
  s'adresse aux deux, et pour deux raisons différentes.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService
from apps.delivery.states import DeliveryStatus
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import staff_to_alert
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
from apps.payments.services import RefundService
from apps.payments.signals import payment_transaction_failed, payment_transaction_settled
from apps.restaurants.models import AreaMembership, Restaurant, StaffMembership
from apps.restaurants.signals import restaurant_status_changed
from apps.restaurants.states import RestaurantStatus
from common.money import Money

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def notifications_de(user: User, kind: str | None = None) -> list[Notification]:
    lot = Notification.objects.filter(user=user)
    if kind is not None:
        lot = lot.filter(kind=kind)
    return list(lot)


@pytest.fixture
def operateur(restaurant: Restaurant) -> User:
    """Personnel rattaché à l'établissement, habilité à lire les commandes."""
    membre = User.objects.create_user(
        "operateur@elcorazon.test",
        "motdepasse",
        full_name="Afi Opératrice",
        user_type=UserType.STAFF,
    )
    membre.roles.add(Role.objects.create(name="Opérateur commandes", permissions=["orders.read"]))
    StaffMembership.objects.create(user=membre, restaurant=restaurant)
    return membre


class TestQuiEstPrevenu:
    def test_seul_le_personnel_de_l_etablissement_est_prevenu(
        self, restaurant: Restaurant, operateur: User, zone: Any
    ) -> None:
        """Le cloisonnement vaut ici ce qu'il vaut dans les vues : un opérateur
        de Kara n'a pas à être réveillé par les commandes de Lomé, qu'il ne peut
        ni voir ni traiter."""
        ailleurs = Restaurant.objects.create(
            name="El Corazón Kara",
            slug="el-corazon-kara",
            zone=zone,
            address="Kara",
            location=restaurant.location,
            phone="+22890000001",
        )
        etranger = User.objects.create_user(
            "kara@elcorazon.test",
            "motdepasse",
            full_name="Kodjo Kara",
            user_type=UserType.STAFF,
        )
        etranger.roles.add(Role.objects.create(name="Opérateur Kara", permissions=["orders.read"]))
        StaffMembership.objects.create(user=etranger, restaurant=ailleurs)

        prevenus = list(staff_to_alert(restaurant_id=restaurant.pk, permission="orders.read"))

        assert operateur in prevenus
        assert etranger not in prevenus

    def test_la_permission_est_exigee_en_plus_du_rattachement(self, restaurant: Restaurant) -> None:
        """Alerter d'une commande quelqu'un à qui l'API la refusera ensuite en
        403 produit une notification qui ne mène nulle part."""
        sans_droit = User.objects.create_user(
            "cuisine@elcorazon.test",
            "motdepasse",
            full_name="Sans Droit",
            user_type=UserType.STAFF,
        )
        StaffMembership.objects.create(user=sans_droit, restaurant=restaurant)

        prevenus = list(staff_to_alert(restaurant_id=restaurant.pk, permission="orders.read"))

        assert sans_droit not in prevenus

    def test_un_compte_desactive_n_est_pas_prevenu(
        self, restaurant: Restaurant, operateur: User
    ) -> None:
        operateur.is_active = False
        operateur.save(update_fields=["is_active"])

        prevenus = list(staff_to_alert(restaurant_id=restaurant.pk, permission="orders.read"))

        assert operateur not in prevenus

    def test_un_membre_a_deux_roles_n_est_prevenu_qu_une_fois(
        self, restaurant: Restaurant, operateur: User
    ) -> None:
        """Sans `distinct`, la jointure le rendrait deux fois — et il recevrait
        deux notifications pour un seul événement."""
        # `orders.update_status` et non `orders.write` : cette seconde n'existe
        # pas au registre (`apps/accounts/permissions.py`), et
        # `validate_permissions` refuse toute permission hors registre — le rôle
        # ne pouvait donc pas être créé, et le test échouait à sa deuxième ligne
        # sans jamais atteindre ce qu'il vérifie.
        #
        # Ce que le test exerce est le `distinct` de `staff_to_alert` : il suffit
        # que le second rôle porte lui aussi la permission interrogée
        # (`orders.read`) pour que la jointure rende le membre deux fois.
        operateur.roles.add(
            Role.objects.create(
                name="Superviseur",
                permissions=["orders.read", "orders.update_status"],
            )
        )

        prevenus = list(staff_to_alert(restaurant_id=restaurant.pk, permission="orders.read"))

        assert prevenus.count(operateur) == 1


class TestCommande:
    def test_une_commande_confirmee_previent_le_client_et_le_personnel(
        self, order: Order, operateur: User
    ) -> None:
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)

        assert notifications_de(order.customer, NotificationKind.ORDER_STATUS)
        assert notifications_de(operateur, NotificationKind.ORDER_STATUS)

    def test_un_client_ne_recoit_jamais_une_alerte_du_personnel(
        self, order: Order, operateur: User
    ) -> None:
        """La promesse de sécurité du module : les deux publics ne se croisent
        pas. La population du personnel est filtrée sur `user_type=staff`, et
        celle du client est le titulaire de la commande — nommé, pas cherché."""
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)

        recues = notifications_de(order.customer)
        assert all(notification.user_id == order.customer_id for notification in recues)
        # Le libellé du personnel — « à préparer » — ne doit jamais atteindre
        # le client, qui n'a rien à préparer.
        assert not any("préparer" in notification.body for notification in recues)

    def test_une_livraison_normale_ne_previent_pas_le_personnel(
        self, order: Order, operateur: User
    ) -> None:
        """Le cas nominal ne s'annonce pas au back-office : le notifier noierait
        les deux étapes qui comptent."""
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        avant = len(notifications_de(operateur))

        OrderService.transition_to(order=order, target=OrderStatus.PICKED_UP)
        OrderService.transition_to(order=order, target=OrderStatus.ON_THE_WAY)
        OrderService.transition_to(order=order, target=OrderStatus.DELIVERED)

        assert len(notifications_de(operateur)) == avant

    def test_une_annulation_previent_le_personnel(self, order: Order, operateur: User) -> None:
        OrderService.transition_to(order=order, target=OrderStatus.CANCELLED)

        recues = notifications_de(operateur, NotificationKind.ORDER_STATUS)
        assert any("annulée" in notification.body for notification in recues)


class TestPaiementRefuse:
    def test_un_paiement_refuse_previent_les_deux_publics(
        self, order: Order, operateur: User
    ) -> None:
        """Le seul événement adressé aux deux, et pour deux raisons : le client
        seul peut reprendre le paiement, l'exploitation seule peut relancer ou
        libérer la commande.

        Avant, il était **entièrement muet** : la transaction passait en
        `failed`, la commande restait où elle était, et `NotificationKind.PAYMENT`
        n'était émis nulle part.
        """
        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-ECHEC-001",
            amount=order.total,
        )

        payment_transaction_failed.send(sender=Transaction, transaction=txn)

        assert notifications_de(order.customer, NotificationKind.PAYMENT)
        assert notifications_de(operateur, NotificationKind.PAYMENT)

    def test_un_encaissement_sans_commande_ne_notifie_rien(
        self, customer: User, operateur: User
    ) -> None:
        """Un abonnement, un rechargement : aucune commande à désigner, et rien
        de plus à dire que ce que l'écran de paiement montre déjà."""
        txn = Transaction.objects.create(
            order=None,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-ABONNEMENT-001",
            amount=Money(1_000, "XOF"),
        )

        payment_transaction_failed.send(sender=Transaction, transaction=txn)

        assert not notifications_de(customer, NotificationKind.PAYMENT)
        assert not notifications_de(operateur, NotificationKind.PAYMENT)


class TestEncaissementARembourser:
    """Un encaissement que la commande n'appelait pas — « un paiement est reçu
    deux fois », ou reçu pour une commande annulée.

    L'argent est pris chez le prestataire : le refuser n'est pas possible, et le
    taire le laisserait chez nous. Seule l'exploitation peut rembourser ; c'est
    donc elle qu'on prévient, et elle seule.
    """

    @staticmethod
    def _encaisser(order: Order, reference: str) -> Transaction:
        return Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference=reference,
            amount=order.total,
        )

    def test_un_trop_percu_previent_l_exploitation(self, order: Order, operateur: User) -> None:
        Order.objects.filter(pk=order.pk).update(
            amount_paid_minor=order.total.amount_minor * 2,
            amount_paid_currency=order.total.currency,
        )
        order.refresh_from_db()
        txn = self._encaisser(order, "PD-DOUBLE-002")

        payment_transaction_settled.send(sender=Transaction, transaction=txn)

        alertes = notifications_de(operateur, NotificationKind.PAYMENT)
        assert len(alertes) == 1
        assert order.reference in alertes[0].body
        assert alertes[0].data["transaction"] == str(txn.pk)
        assert not notifications_de(order.customer, NotificationKind.PAYMENT)

    def test_un_encaissement_sur_commande_annulee_previent_l_exploitation(
        self, order: Order, operateur: User
    ) -> None:
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.CANCELLED,
            amount_paid_minor=order.total.amount_minor,
            amount_paid_currency=order.total.currency,
        )
        order.refresh_from_db()
        txn = self._encaisser(order, "PD-ANNULEE-001")

        payment_transaction_settled.send(sender=Transaction, transaction=txn)

        assert len(notifications_de(operateur, NotificationKind.PAYMENT)) == 1

    def test_un_encaissement_ordinaire_ne_derange_personne(
        self, order: Order, operateur: User
    ) -> None:
        Order.objects.filter(pk=order.pk).update(
            amount_paid_minor=order.total.amount_minor,
            amount_paid_currency=order.total.currency,
        )
        order.refresh_from_db()
        txn = self._encaisser(order, "PD-NORMAL-001")

        payment_transaction_settled.send(sender=Transaction, transaction=txn)

        assert not notifications_de(operateur, NotificationKind.PAYMENT)


class TestRemboursementVerse:
    """Le client apprend que son argent lui est rendu.

    Un remboursement constaté par l'exploitation ne disait rien au client :
    l'annulation lui avait été notifiée, le remboursement jamais. Il restait à
    guetter son compte mobile money sans savoir si quelque chose était parti.
    """

    def test_le_versement_constate_previent_le_client(self, order: Order, operateur: User) -> None:
        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-REMB-001",
            amount=order.total,
            status=PaymentStatus.COMPLETED,
        )
        remboursement = RefundService.refund(
            order=order,
            transaction_id=str(txn.pk),
            amount=order.total,
            reason="Commande annulée",
            actor=operateur,
        )
        assert not notifications_de(order.customer, NotificationKind.PAYMENT)

        RefundService.settle(refund=remboursement, actor=operateur)

        [avis] = notifications_de(order.customer, NotificationKind.PAYMENT)
        assert order.reference in avis.body
        assert avis.data == {"order": str(order.pk), "refund": str(remboursement.pk)}


class TestCourseAcceptee:
    def test_l_acceptation_previent_le_client(self, order: Order, courier: CourierProfile) -> None:
        """L'intervalle où le client n'apprenait rien : `accepted` n'est pas
        projeté sur le statut de la commande — le repas n'est pas parti — si
        bien qu'entre « confirmée » et « en route » aucun signe de vie
        n'arrivait."""
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        assignment = AssignmentService.offer(order=order, courier=courier)
        avant = len(notifications_de(order.customer, NotificationKind.ORDER_STATUS))

        AssignmentService.accept(assignment=assignment, courier=courier)

        recues = notifications_de(order.customer, NotificationKind.ORDER_STATUS)
        assert len(recues) == avant + 1
        assert any(courier.user.full_name in notification.body for notification in recues)

    def test_une_proposition_seule_ne_previent_pas_le_client(
        self, order: Order, courier: CourierProfile
    ) -> None:
        """Une course proposée peut être refusée : annoncer un livreur qui ne
        viendra pas est pire que de ne rien dire."""
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        avant = len(notifications_de(order.customer, NotificationKind.ORDER_STATUS))

        AssignmentService.offer(order=order, courier=courier)

        assert len(notifications_de(order.customer, NotificationKind.ORDER_STATUS)) == avant

    def test_le_livreur_est_prevenu_de_la_proposition(
        self, order: Order, courier: CourierProfile
    ) -> None:
        """L'existant, gardé : c'est le seul flux où rater un événement a un
        coût métier direct (ADR-008)."""
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)

        AssignmentService.offer(order=order, courier=courier)

        assert notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)


class TestAnnulationDeCourse:
    """Le pendant de la proposition, qui manquait.

    L'annulation par le personnel diffusait bien un événement — mais sur le
    canal de la **commande**, celui que le client écoute. Le livreur n'écoute
    que sa propre file. Il continuait donc vers le restaurant et l'apprenait de
    la cuisine en arrivant.
    """

    @staticmethod
    def _course_engagee(order: Order, courier: CourierProfile) -> Assignment:
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        assignment: Assignment = AssignmentService.offer(order=order, courier=courier)
        return AssignmentService.accept(assignment=assignment, courier=courier)

    def test_le_livreur_est_prevenu(self, order: Order, courier: CourierProfile) -> None:
        assignment = self._course_engagee(order, courier)

        AssignmentService.transition_to(
            assignment=assignment,
            target=DeliveryStatus.CANCELLED,
            actor=None,
            reason="Le client a annulé.",
        )

        recues = notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)
        assert any(n.title == "Course annulée" for n in recues)

    def test_le_motif_est_repris(self, order: Order, courier: CourierProfile) -> None:
        """Une annulation sans raison se lit comme une sanction."""
        assignment = self._course_engagee(order, courier)

        AssignmentService.transition_to(
            assignment=assignment,
            target=DeliveryStatus.CANCELLED,
            actor=None,
            reason="Panne du véhicule signalée par téléphone.",
        )

        annulation = next(
            n
            for n in notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)
            if n.title == "Course annulée"
        )
        assert "Panne du véhicule" in annulation.body

    def test_sans_motif_on_ne_pretend_pas_en_avoir_un(
        self, order: Order, courier: CourierProfile
    ) -> None:
        assignment = self._course_engagee(order, courier)

        AssignmentService.transition_to(
            assignment=assignment, target=DeliveryStatus.CANCELLED, actor=None, reason=""
        )

        annulation = next(
            n
            for n in notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)
            if n.title == "Course annulée"
        )
        assert "retirée" in annulation.body

    def test_la_charge_ouvre_la_course_et_la_commande(
        self, order: Order, courier: CourierProfile
    ) -> None:
        assignment = self._course_engagee(order, courier)

        AssignmentService.transition_to(
            assignment=assignment, target=DeliveryStatus.CANCELLED, actor=None, reason="x"
        )

        annulation = next(
            n
            for n in notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)
            if n.title == "Course annulée"
        )
        assert annulation.data == {"assignment": str(assignment.pk), "order": str(order.pk)}

    def test_un_refus_du_livreur_ne_le_notifie_pas(
        self, order: Order, courier: CourierProfile
    ) -> None:
        """Personne n'a besoin d'être prévenu de sa propre décision.

        C'est la distinction que porte le signal : `declined` est le geste du
        livreur, `cancelled` celui du personnel.
        """
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        assignment = AssignmentService.offer(order=order, courier=courier)

        AssignmentService.decline(assignment=assignment, courier=courier, reason="Trop loin.")

        recues = notifications_de(courier.user, NotificationKind.DELIVERY_OFFER)
        assert not any(n.title == "Course annulée" for n in recues)


class TestChargeUtile:
    def test_la_charge_ne_porte_que_de_quoi_ouvrir_un_ecran(
        self, order: Order, operateur: User
    ) -> None:
        """ADR-008 : jamais une copie de l'objet métier, qui aura changé d'ici
        la lecture. Le client recharge, et reçoit l'état du moment."""
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)

        notification = notifications_de(order.customer, NotificationKind.ORDER_STATUS)[0]

        assert set(notification.data) == {"order", "status"}
        assert notification.data["order"] == str(order.pk)

    def test_l_assignation_designe_la_course_et_la_commande(
        self, order: Order, courier: CourierProfile
    ) -> None:
        for etape in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            OrderService.transition_to(order=order, target=etape)
        assignment: Assignment = AssignmentService.offer(order=order, courier=courier)
        AssignmentService.accept(assignment=assignment, courier=courier)

        notification = (
            Notification.objects.filter(user=order.customer, kind=NotificationKind.ORDER_STATUS)
            .order_by("-created_at")
            .first()
        )

        assert notification is not None
        assert notification.data["assignment"] == str(assignment.pk)
        assert notification.data["order"] == str(order.pk)


class TestCycleDeVieEtablissement:
    """Suspendre un établissement est une décision, pas un incident.

    Personne n'était prévenu : l'équipe l'apprenait en constatant que les
    commandes ne rentraient plus, et cherchait la panne du côté du réseau.
    """

    def _gerant(self, restaurant: Restaurant) -> User:
        membre = User.objects.create_user(
            "gerant-cycle@elcorazon.test",
            "motdepasse",
            full_name="Gérante",
            user_type=UserType.STAFF,
        )
        membre.roles.add(
            Role.objects.create(name="Exploitation cycle", permissions=["restaurants.read"])
        )
        StaffMembership.objects.create(user=membre, restaurant=restaurant)
        return membre

    def test_une_suspension_previent_le_personnel(self, restaurant: Restaurant) -> None:
        gerant = self._gerant(restaurant)

        restaurant.transition_to(RestaurantStatus.INACTIVE)

        notification = Notification.objects.filter(user=gerant).latest("created_at")
        assert "suspendu" in notification.title.lower()
        assert notification.data["slug"] == restaurant.slug

    def test_une_reouverture_ne_se_dit_pas_comme_une_inauguration(
        self, restaurant: Restaurant
    ) -> None:
        """Le sens du geste est dans la paire (précédent, cible).

        « suspendu → en service » est une réouverture ; le mot compte pour qui
        relit ses notifications après coup.
        """
        gerant = self._gerant(restaurant)

        # Le signal est émis directement plutôt que par `transition_to` : la
        # remise en service exige une configuration complète — carte, horaires,
        # flotte — et monter ce décor ici ne testerait pas la règle visée, qui
        # est le **choix du mot** selon l'état précédent. La complétude a ses
        # propres tests, dans `tests/restaurants`.
        restaurant_status_changed.send(
            sender=Restaurant,
            restaurant=restaurant,
            previous=RestaurantStatus.INACTIVE,
            target=RestaurantStatus.ACTIVE,
        )

        notification = Notification.objects.filter(user=gerant).latest("created_at")
        assert "rouvre" in notification.title.lower()

    def test_le_client_n_est_pas_prevenu(self, restaurant: Restaurant, customer: User) -> None:
        """Il voit la conséquence dans l'application.

        Une notification « le restaurant est suspendu » sur le téléphone de
        quelqu'un qui n'y commande pas serait du bruit.
        """
        self._gerant(restaurant)

        restaurant.transition_to(RestaurantStatus.INACTIVE)

        assert not Notification.objects.filter(user=customer).exists()

    def test_le_personnel_d_un_autre_etablissement_n_est_pas_prevenu(
        self, restaurant: Restaurant
    ) -> None:
        etranger = User.objects.create_user(
            "ailleurs@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        etranger.roles.add(Role.objects.create(name="Ailleurs", permissions=["restaurants.read"]))

        restaurant.transition_to(RestaurantStatus.INACTIVE)

        assert not Notification.objects.filter(user=etranger).exists()

    def test_un_directeur_de_marche_est_prevenu(self, restaurant: Restaurant) -> None:
        """Le palier pays/ville doit être suivi par les alertes comme par les écrans.

        Sinon le directeur voit les commandes de son marché au back-office sans
        jamais être prévenu de ce qui s'y passe.
        """
        directeur = User.objects.create_user(
            "directeur-cycle@elcorazon.test", "motdepasse", user_type=UserType.STAFF
        )
        directeur.roles.add(
            Role.objects.create(name="Marché cycle", permissions=["restaurants.read"])
        )
        AreaMembership.objects.create(user=directeur, country=restaurant.zone.city.country)

        restaurant.transition_to(RestaurantStatus.INACTIVE)

        assert Notification.objects.filter(user=directeur).exists()

    def test_un_etablissement_neuf_n_a_pas_deja_ete_configure(self, restaurant: Restaurant) -> None:
        """« Repasse en configuration » sur un brouillon ferait croire à un retour.

        Le sens du geste est dans la paire (précédent, cible) : « brouillon →
        en configuration » est un début, « prêt → en configuration » un retour
        en arrière. Le même mot pour les deux se relit mal trois jours plus
        tard.
        """
        gerant = self._gerant(restaurant)

        restaurant_status_changed.send(
            sender=Restaurant,
            restaurant=restaurant,
            previous=RestaurantStatus.DRAFT,
            target=RestaurantStatus.CONFIGURING,
        )

        notification = Notification.objects.filter(user=gerant).latest("created_at")
        assert "entre en configuration" in notification.title
        assert "repasse" not in notification.title

    def test_un_retour_en_arriere_se_dit_comme_un_retour(self, restaurant: Restaurant) -> None:
        gerant = self._gerant(restaurant)

        restaurant_status_changed.send(
            sender=Restaurant,
            restaurant=restaurant,
            previous=RestaurantStatus.READY,
            target=RestaurantStatus.CONFIGURING,
        )

        notification = Notification.objects.filter(user=gerant).latest("created_at")
        assert "repasse en configuration" in notification.title
