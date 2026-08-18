"""Notifications et push — ADR-002, ADR-008.

Deux tests portent cette suite :

* `test_un_appareil_desinstalle_est_supprime` — l'implémentation précédente
  retentait trois fois un appareil injoignable, à chaque notification,
  indéfiniment. Un utilisateur parti coûtait du quota pour toujours.
* `test_orders_ne_connait_pas_notifications` — la flèche du graphe de
  dépendances. Un appel direct la retournerait, et à la quatrième app abonnée
  on aurait reconstitué le monolithe que le découpage évite.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Device, DevicePlatform, User
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.services import AssignmentService
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.push import PushDeliveryIncomplete, PushMessage, PushResult
from apps.notifications.services import notify
from apps.notifications.tasks import purge_unregistered_devices, send_push
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.profiles.models import CustomerPreference

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


class RecordingBackend:
    """Service push qui note ce qu'on lui demande.

    Déclaré au module et non en fermeture : `PUSH_BACKEND` est un chemin
    d'import, et une classe locale à un test ne s'importe pas.
    """

    sent: ClassVar[list[tuple[list[str], PushMessage]]] = []
    unregistered: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()

    def send(self, tokens: list[str], message: PushMessage) -> PushResult:
        RecordingBackend.sent.append((tokens, message))
        morts = tuple(t for t in tokens if t in RecordingBackend.unregistered)
        rates = tuple(t for t in tokens if t in RecordingBackend.failing)
        vivants = tuple(t for t in tokens if t not in morts and t not in rates)
        return PushResult(delivered=vivants, unregistered=morts, failed=rates)


@pytest.fixture
def recorder(settings: object) -> type[RecordingBackend]:
    RecordingBackend.sent = []
    RecordingBackend.unregistered = ()
    RecordingBackend.failing = ()
    settings.PUSH_BACKEND = "tests.notifications.test_notifications.RecordingBackend"  # type: ignore[attr-defined]
    return RecordingBackend


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def device(customer: User) -> Device:
    return Device.objects.create(
        user=customer, token="jeton-appareil-1", platform=DevicePlatform.ANDROID
    )


class TestEmission:
    def test_une_notification_est_enregistree(self, customer: User) -> None:
        notification = notify(
            user=customer,
            kind=NotificationKind.ORDER_STATUS,
            title="Commande confirmée",
            body="Votre commande EC000001 est confirmée.",
            data={"order": "abc"},
        )

        assert notification is not None
        assert Notification.objects.count() == 1
        assert notification.is_read is False

    def test_le_transactionnel_ignore_le_refus_du_marketing(self, customer: User) -> None:
        """« Votre livreur arrive » n'est pas une sollicitation commerciale :
        le couper produirait un client planté devant sa porte."""
        CustomerPreference.objects.create(user=customer, marketing_push_enabled=False)

        notification = notify(
            user=customer,
            kind=NotificationKind.ORDER_STATUS,
            title="En route",
            body="Votre commande arrive.",
        )

        assert notification is not None

    def test_le_marketing_respecte_le_refus(self, customer: User) -> None:
        CustomerPreference.objects.create(user=customer, marketing_push_enabled=False)

        notification = notify(
            user=customer,
            kind=NotificationKind.MARKETING,
            title="−20 % ce week-end",
            body="Profitez-en.",
        )

        assert notification is None
        assert Notification.objects.count() == 0

    def test_sans_preferences_enregistrees_le_marketing_passe(self, customer: User) -> None:
        """Le refuser ferait taire toute communication tant que l'utilisateur
        n'a pas visité un écran de réglages qu'il ne visitera jamais."""
        assert (
            notify(
                user=customer,
                kind=NotificationKind.MARKETING,
                title="Nouveauté",
                body="Le burger du mois.",
            )
            is not None
        )


class TestEnvoiPush:
    def test_la_notification_part_vers_les_appareils(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        notification = notify(
            user=customer,
            kind=NotificationKind.ORDER_STATUS,
            title="Livrée",
            body="Bon appétit !",
            data={"order": "abc"},
        )
        assert notification is not None

        send_push(str(notification.pk))

        tokens, message = recorder.sent[-1]
        assert tokens == [device.token]
        assert message.title == "Livrée"
        assert message.data == {"kind": NotificationKind.ORDER_STATUS, "order": "abc"}

    def test_un_appareil_desinstalle_est_supprime(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        """La moitié qui manquait : distinguer l'échec transitoire du
        définitif. Sans elle, l'appareil parti est retenté à chaque
        notification, indéfiniment."""
        recorder.unregistered = (device.token,)
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None

        resultat = send_push(str(notification.pk))

        assert resultat["purged"] == 1
        assert Device.objects.count() == 0

    def test_sans_appareil_rien_n_est_tente(
        self, customer: User, recorder: type[RecordingBackend]
    ) -> None:
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None

        assert send_push(str(notification.pk)) == {"delivered": 0, "purged": 0, "failed": 0}
        assert recorder.sent == []

    def test_une_notification_disparue_n_est_pas_une_erreur(
        self, recorder: type[RecordingBackend]
    ) -> None:
        """Un compte effacé entre la programmation et l'exécution : la tâche
        n'a rien à faire, et surtout rien à retenter."""
        import uuid

        assert send_push(str(uuid.uuid4())) == {"delivered": 0, "purged": 0, "failed": 0}

    def test_la_charge_utile_est_convertie_en_chaines(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        """FCM refuse un entier dans `data`, et l'erreur ne se voit qu'en
        production sur un type de notification qu'on n'a pas testé."""
        notification = notify(
            user=customer,
            kind=NotificationKind.PAYMENT,
            title="Paiement reçu",
            body=".",
            data={"amount": 4000, "retries": 0},
        )
        assert notification is not None

        send_push(str(notification.pk))

        _, message = recorder.sent[-1]
        assert message.data == {
            "kind": NotificationKind.PAYMENT,
            "amount": "4000",
            "retries": "0",
        }


class TestRepriseSurEchecPassager:
    """L'autre moitié de la distinction transitoire / définitif.

    Le décorateur annonçait `max_retries` et un report exponentiel, et rien
    n'appelait jamais `retry` : la configuration était décorative, et un push
    perdu pour cause de quota l'était définitivement.
    """

    def test_un_echec_passager_demande_une_reprise(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        recorder.failing = (device.token,)
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None

        # Appelée directement, la tâche ne peut pas se replanifier : Celery
        # relève alors l'exception passée à `retry`, ce qui prouve que la
        # reprise a bien été demandée.
        with pytest.raises(PushDeliveryIncomplete):
            send_push(str(notification.pk))

        assert Device.objects.count() == 1, "un échec passager ne supprime pas l'appareil"

    def test_la_reprise_ne_porte_que_sur_les_jetons_en_echec(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        """Reprendre toute la liste ferait vibrer deux fois le téléphone de
        ceux qui avaient reçu."""
        second = Device.objects.create(
            user=customer, token="jeton-appareil-2", platform=DevicePlatform.IOS
        )
        recorder.failing = (second.token,)
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None

        with pytest.raises(PushDeliveryIncomplete) as leve:
            send_push(str(notification.pk))

        assert leve.value.tokens == (second.token,)

        # La reprise elle-même : seul le jeton en échec est réadressé.
        recorder.failing = ()
        send_push(str(notification.pk), [second.token])

        assert recorder.sent[-1][0] == [second.token]

    def test_un_jeton_purge_entre_temps_n_est_pas_readresse(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        """Réessayer sur un appareil supprimé rejouerait la boucle qu'on
        cherche justement à casser."""
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None
        Device.objects.filter(pk=device.pk).delete()

        assert send_push(str(notification.pk), [device.token]) == {
            "delivered": 0,
            "purged": 0,
            "failed": 0,
        }
        assert recorder.sent == []

    def test_un_appareil_mort_et_un_vivant_sont_traites_separement(
        self, customer: User, device: Device, recorder: type[RecordingBackend]
    ) -> None:
        vivant = Device.objects.create(
            user=customer, token="jeton-appareil-3", platform=DevicePlatform.IOS
        )
        recorder.unregistered = (device.token,)
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body="."
        )
        assert notification is not None

        resultat = send_push(str(notification.pk))

        assert resultat == {"delivered": 1, "purged": 1, "failed": 0}
        assert Device.objects.get().pk == vivant.pk


class TestPurgePlanifiee:
    def test_les_appareils_muets_sont_supprimes(self, customer: User, device: Device) -> None:
        """Un téléphone perdu ne se signale jamais comme injoignable ; six mois
        sans un seul rafraîchissement suffisent à conclure."""
        Device.objects.filter(pk=device.pk).update(
            last_used_at=timezone.now() - dt.timedelta(days=200)
        )

        assert purge_unregistered_devices(days=180) == 1
        assert Device.objects.count() == 0

    def test_un_appareil_actif_survit(self, customer: User, device: Device) -> None:
        assert purge_unregistered_devices(days=180) == 0
        assert Device.objects.count() == 1


class TestAbonnementsAuxEvenements:
    def test_une_transition_de_commande_notifie_le_client(
        self, order: Order, customer: User
    ) -> None:
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)

        notification = Notification.objects.get(user=customer)
        assert notification.kind == NotificationKind.ORDER_STATUS
        assert "EC000001" in notification.body

    def test_les_etapes_de_cuisine_ne_notifient_pas(self, order: Order, customer: User) -> None:
        """Notifier chaque transition est le meilleur moyen de se faire couper
        les notifications, et de perdre celles qui comptent."""
        OrderService.transition_to(order=order, target=OrderStatus.CONFIRMED)
        OrderService.transition_to(order=order, target=OrderStatus.PREPARING)
        OrderService.transition_to(order=order, target=OrderStatus.READY)

        assert Notification.objects.filter(user=customer).count() == 1

    def test_une_course_proposee_notifie_le_livreur(
        self, order: Order, courier: CourierProfile
    ) -> None:
        """Le seul flux où rater un événement a un coût métier direct : une
        course non vue est un repas qui refroidit."""
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.READY)
        order.refresh_from_db()

        AssignmentService.offer(order=order, courier=courier)

        notification = Notification.objects.get(user=courier.user)
        assert notification.kind == NotificationKind.DELIVERY_OFFER
        assert Assignment.objects.count() == 1

    def test_orders_ne_connait_pas_notifications(self) -> None:
        """ADR-002 — la flèche va de `notifications` vers `orders`, jamais
        l'inverse. Un import direct la retournerait, et le graphe cesserait
        d'être ce qu'il déclare être."""
        from pathlib import Path

        import apps.orders.services as orders_services

        # Le fichier est localisé par le module lui-même : un chemin relatif au
        # répertoire courant ferait passer ce test selon l'endroit d'où pytest
        # est lancé, ce qui est exactement l'inverse de ce qu'on veut d'une
        # règle d'architecture.
        source = Path(orders_services.__file__).read_text(encoding="utf-8")

        assert "apps.notifications" not in source
        assert "order_status_changed" in source


class TestApiDeLecture:
    def test_le_client_lit_ses_notifications(self, as_customer: APIClient, customer: User) -> None:
        notify(user=customer, kind=NotificationKind.ORDER_STATUS, title="Livrée", body=".")

        response = as_customer.get(reverse("v1:notifications:notification-list"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1

    def test_il_ne_voit_pas_celles_des_autres(
        self, as_customer: APIClient, courier_user: User
    ) -> None:
        notify(user=courier_user, kind=NotificationKind.DELIVERY_OFFER, title="Course", body=".")

        response = as_customer.get(reverse("v1:notifications:notification-list"))

        assert response.data["count"] == 0

    def test_le_compteur_de_non_lues(self, as_customer: APIClient, customer: User) -> None:
        """Compter côté client donnerait un nombre faux dès la
        vingt-et-unième, la liste étant paginée."""
        for _ in range(3):
            notify(user=customer, kind=NotificationKind.ORDER_STATUS, title="X", body=".")

        response = as_customer.get(reverse("v1:notifications:notification-unread-count"))

        assert response.data["unread"] == 3

    def test_le_marquage_est_idempotent(self, as_customer: APIClient, customer: User) -> None:
        """Relire n'écrase pas la date de première lecture : c'est elle qui a
        un sens."""
        notification = notify(
            user=customer, kind=NotificationKind.ORDER_STATUS, title="X", body="."
        )
        assert notification is not None
        url = reverse("v1:notifications:notification-mark-read", args=[notification.pk])

        premier = as_customer.post(url).data["read_at"]
        second = as_customer.post(url).data["read_at"]

        assert premier == second

    def test_tout_marquer_lu(self, as_customer: APIClient, customer: User) -> None:
        for _ in range(3):
            notify(user=customer, kind=NotificationKind.ORDER_STATUS, title="X", body=".")

        as_customer.post(reverse("v1:notifications:notification-mark-all-read"))

        assert Notification.objects.filter(user=customer, read_at__isnull=True).count() == 0

    def test_une_notification_ne_se_cree_pas_depuis_l_api(self, as_customer: APIClient) -> None:
        """Une notification est produite par le serveur ; l'accepter du client
        laisserait n'importe qui écrire dans la boîte de n'importe qui."""
        response = as_customer.post(
            reverse("v1:notifications:notification-list"),
            {"title": "Faux", "body": "."},
            format="json",
        )

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
