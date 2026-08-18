"""Suivi temps réel — ADR-008, invariant L3.

Cette suite est écrite comme une suite d'attaques sur la connexion. C'est là
que l'implémentation précédente échouait : l'abonnement Supabase donnait accès
à des lignes, jamais à un périmètre métier, si bien qu'un livreur pouvait
publier des positions sur la course d'un autre.

Le socket est refusé **avant** d'être accepté. Il n'existe pas d'état
« connecté mais pas encore autorisé » pendant lequel un message passerait.
"""

from __future__ import annotations

import datetime as dt

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from apps.accounts.models import Role, User, UserType
from apps.accounts.services import AuthService
from apps.delivery.models import Assignment, CourierProfile, VehicleType
from apps.delivery.states import DeliveryStatus, VerificationStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from common.consumers import CLOSE_FORBIDDEN, CLOSE_STALE, CLOSE_UNAUTHENTICATED
from config.asgi import application

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgis]

LOME = {"lat": 6.1319, "lon": 1.2255}


def token_for(user: User) -> str:
    return AuthService.issue_tokens(user).access


async def connect(
    path: str, *, token: str | None = None, since: int | None = None
) -> WebsocketCommunicator:
    """Ouvre un socket avec le jeton en en-tête, comme un client natif."""
    query = f"?since={since}" if since is not None else ""
    # `Origin` est exigé par `AllowedHostsOriginValidator` : un socket sans
    # origine reconnue est fermé avant d'atteindre le consommateur. Le poser
    # ici fait de ce test un client réaliste plutôt qu'un cas privilégié.
    headers = [(b"origin", b"http://testserver")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    return WebsocketCommunicator(application, f"{path}{query}", headers=headers)


@pytest.fixture
def tracked_order(order: Order) -> Order:
    """Commande dans un état où un suivi a du sens."""
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.READY)
    order.refresh_from_db()
    return order


@pytest.fixture
def course(tracked_order: Order, courier: CourierProfile) -> Assignment:
    return Assignment.objects.create(
        order=tracked_order, courier=courier, status=DeliveryStatus.ACCEPTED
    )


class TestAutorisationALaConnexion:
    @pytest.mark.asyncio
    async def test_sans_jeton_le_socket_est_ferme(self, tracked_order: Order) -> None:
        communicator = await connect(f"/ws/orders/{tracked_order.pk}/tracking/")

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_un_jeton_invalide_ferme_aussi(self, tracked_order: Order) -> None:
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/", token="pas-un-jeton"
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_le_client_de_la_commande_est_accepte(
        self, tracked_order: Order, customer: User
    ) -> None:
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_autre_client_est_refuse(self, tracked_order: Order) -> None:
        """La faille en une phrase : connaître l'identifiant d'une commande ne
        doit pas suffire à suivre le repas de quelqu'un d'autre."""
        intrus = await database_sync_to_async(User.objects.create_user)(
            "intrus@elcorazon.test", "motdepasse", full_name="Intrus"
        )
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/",
            token=await database_sync_to_async(token_for)(intrus),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_le_livreur_assigne_est_accepte(
        self, course: Assignment, courier_user: User
    ) -> None:
        communicator = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(courier_user),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_livreur_non_assigne_est_refuse(
        self, tracked_order: Order, restaurant: Restaurant
    ) -> None:
        """L3 — c'est l'affectation qui donne le droit, pas le type de compte."""

        @database_sync_to_async
        def autre_livreur() -> User:
            user = User.objects.create_user(
                "libre@elcorazon.test",
                "motdepasse",
                full_name="Libre",
                user_type=UserType.COURIER,
            )
            CourierProfile.objects.create(
                user=user,
                restaurant=restaurant,
                vehicle_type=VehicleType.BICYCLE,
                verification_status=VerificationStatus.APPROVED,
                is_online=True,
            )
            return user

        user = await autre_livreur()
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_le_personnel_d_un_autre_etablissement_est_refuse(
        self, tracked_order: Order, zone: object
    ) -> None:
        @database_sync_to_async
        def personnel_ailleurs() -> User:
            ailleurs = Restaurant.objects.create(
                name="El Corazón Kara",
                slug="ec-kara",
                zone=zone,
                address="Kara",
                location=tracked_order.restaurant.location,
                phone="+22890000021",
            )
            user = User.objects.create_user(
                "kara@elcorazon.test", "motdepasse", full_name="Kara", user_type=UserType.STAFF
            )
            user.roles.add(Role.objects.create(name="Lecture", permissions=["orders.read"]))
            StaffMembership.objects.create(user=user, restaurant=ailleurs)
            return user

        user = await personnel_ailleurs()
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_une_commande_livree_n_ouvre_plus_de_socket(
        self, tracked_order: Order, customer: User
    ) -> None:
        """Laisser le socket s'ouvrir entretiendrait une connexion que plus
        rien n'alimente."""
        await database_sync_to_async(
            lambda: Order.objects.filter(pk=tracked_order.pk).update(status=OrderStatus.DELIVERED)
        )()

        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN


class TestPublicationDePosition:
    @pytest.mark.asyncio
    async def test_le_livreur_publie_et_le_client_recoit(
        self, course: Assignment, courier_user: User, customer: User
    ) -> None:
        livreur = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(courier_user),
        )
        client = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )
        await livreur.connect()
        await client.connect()

        await livreur.send_json_to({"point": LOME, "recorded_at": timezone.now().isoformat()})
        recu = await client.receive_json_from(timeout=5)

        assert recu["type"] == "tracking.position"
        assert recu["lat"] == pytest.approx(6.1319)
        assert recu["persisted"] is True
        assert recu["seq"] == 1

        await livreur.disconnect()
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_le_client_qui_publie_est_ferme(self, course: Assignment, customer: User) -> None:
        """Répondre par une erreur laisserait le socket ouvert à qui vient de
        montrer qu'il essaie autre chose que ce pour quoi il est là."""
        client = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )
        await client.connect()

        await client.send_json_to({"point": LOME, "recorded_at": timezone.now().isoformat()})
        message = await client.receive_output(timeout=5)

        assert message["type"] == "websocket.close"
        assert message["code"] == CLOSE_STALE

    @pytest.mark.asyncio
    async def test_la_diffusion_est_integrale_meme_sans_ecriture(
        self, course: Assignment, courier_user: User, customer: User
    ) -> None:
        """L'échantillonnage porte sur la persistance, pas sur la diffusion :
        c'est elle qui fait l'expérience de suivi."""
        livreur = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(courier_user),
        )
        client = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )
        await livreur.connect()
        await client.connect()

        moment = timezone.now()
        await livreur.send_json_to({"point": LOME, "recorded_at": moment.isoformat()})
        await client.receive_json_from(timeout=5)

        await livreur.send_json_to(
            {"point": LOME, "recorded_at": (moment + dt.timedelta(seconds=2)).isoformat()}
        )
        second = await client.receive_json_from(timeout=5)

        assert second["persisted"] is False
        assert second["seq"] == 2

        await livreur.disconnect()
        await client.disconnect()


class TestRattrapage:
    @pytest.mark.asyncio
    async def test_une_reconnexion_rejoue_ce_qui_a_ete_manque(
        self, course: Assignment, courier_user: User, customer: User
    ) -> None:
        """Sans rattrapage, un tunnel produit une carte figée qui ne se répare
        jamais."""
        livreur = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(courier_user),
        )
        await livreur.connect()

        moment = timezone.now()
        for index in range(3):
            await livreur.send_json_to(
                {
                    "point": LOME,
                    "recorded_at": (moment + dt.timedelta(minutes=index)).isoformat(),
                }
            )
            await livreur.receive_nothing(timeout=0.2)

        # Le client arrive après coup et annonce d'où il repart.
        client = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
            since=1,
        )
        await client.connect()

        rejoues = [await client.receive_json_from(timeout=5) for _ in range(2)]

        assert [event["seq"] for event in rejoues] == [2, 3]

        await livreur.disconnect()
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_sans_since_rien_n_est_rejoue(
        self, course: Assignment, courier_user: User, customer: User
    ) -> None:
        """Un client qui se connecte pour la première fois n'a pas d'historique
        à rattraper ; lui déverser le journal lui ferait afficher un trajet
        passé."""
        livreur = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(courier_user),
        )
        await livreur.connect()
        await livreur.send_json_to({"point": LOME, "recorded_at": timezone.now().isoformat()})
        await livreur.receive_nothing(timeout=0.2)

        client = await connect(
            f"/ws/orders/{course.order_id}/tracking/",
            token=await database_sync_to_async(token_for)(customer),
        )
        await client.connect()

        assert await client.receive_nothing(timeout=0.5)

        await livreur.disconnect()
        await client.disconnect()


class TestFileDuLivreur:
    @pytest.mark.asyncio
    async def test_le_livreur_ouvre_sa_file(self, courier: CourierProfile) -> None:
        communicator = await connect(
            "/ws/couriers/me/",
            token=await database_sync_to_async(token_for)(courier.user),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_client_n_a_pas_de_file(self, customer: User) -> None:
        communicator = await connect(
            "/ws/couriers/me/", token=await database_sync_to_async(token_for)(customer)
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_un_dossier_suspendu_n_ouvre_pas_de_file(self, courier: CourierProfile) -> None:
        """L1 — laisser la file ouverte ferait croire au livreur qu'il est
        joignable alors qu'aucune course ne lui sera proposée."""
        await database_sync_to_async(
            lambda: CourierProfile.objects.filter(pk=courier.pk).update(
                verification_status=VerificationStatus.SUSPENDED
            )
        )()

        communicator = await connect(
            "/ws/couriers/me/",
            token=await database_sync_to_async(token_for)(courier.user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN


class TestChat:
    """`ws/orders/{id}/chat/` — relais client ↔ livreur, ADR-008.

    Même périmètre que le suivi, moins le personnel : ADR-008 ne cite que le
    client et le livreur pour ce groupe.
    """

    @pytest.mark.asyncio
    async def test_sans_jeton_le_socket_est_ferme(self, tracked_order: Order) -> None:
        communicator = await connect(f"/ws/orders/{tracked_order.pk}/chat/")

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_le_client_est_accepte(self, tracked_order: Order, customer: User) -> None:
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/chat/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_le_livreur_assigne_est_accepte(
        self, course: Assignment, courier_user: User
    ) -> None:
        communicator = await connect(
            f"/ws/orders/{course.order_id}/chat/",
            token=await database_sync_to_async(token_for)(courier_user),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_le_personnel_n_a_pas_acces_au_chat(
        self, tracked_order: Order, restaurant: Restaurant
    ) -> None:
        """ADR-008 ne cite que le client et le livreur pour ce groupe — pas le
        personnel, contrairement au suivi de position."""

        @database_sync_to_async
        def personnel() -> User:
            user = User.objects.create_user(
                "staff-chat@elcorazon.test",
                "motdepasse",
                full_name="Staff",
                user_type=UserType.STAFF,
            )
            user.roles.add(Role.objects.create(name="Lecture", permissions=["orders.read"]))
            StaffMembership.objects.create(user=user, restaurant=restaurant)
            return user

        user = await personnel()
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/chat/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_un_autre_client_est_refuse(self, tracked_order: Order) -> None:
        intrus = await database_sync_to_async(User.objects.create_user)(
            "intrus-chat@elcorazon.test", "motdepasse", full_name="Intrus"
        )
        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/chat/",
            token=await database_sync_to_async(token_for)(intrus),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_une_commande_livree_n_ouvre_plus_de_chat(
        self, tracked_order: Order, customer: User
    ) -> None:
        await database_sync_to_async(
            lambda: Order.objects.filter(pk=tracked_order.pk).update(status=OrderStatus.DELIVERED)
        )()

        communicator = await connect(
            f"/ws/orders/{tracked_order.pk}/chat/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_le_client_et_le_livreur_se_parlent(
        self, course: Assignment, courier_user: User, customer: User
    ) -> None:
        client = await connect(
            f"/ws/orders/{course.order_id}/chat/",
            token=await database_sync_to_async(token_for)(customer),
        )
        livreur = await connect(
            f"/ws/orders/{course.order_id}/chat/",
            token=await database_sync_to_async(token_for)(courier_user),
        )
        await client.connect()
        await livreur.connect()

        # La diffusion touche tout le groupe, l'émetteur compris — comme pour
        # le suivi de position. Chacun reçoit donc aussi l'écho de son propre
        # message ; c'est ce qui permet à l'application d'afficher le message
        # envoyé sans jamais désynchroniser deux appareils du même compte.
        await client.send_json_to({"text": "J'arrive dans 5 minutes ?"})
        echo_client = await client.receive_json_from(timeout=5)
        recu = await livreur.receive_json_from(timeout=5)

        assert echo_client == recu
        assert recu["type"] == "chat.message"
        assert recu["sender"] == "customer"
        assert recu["text"] == "J'arrive dans 5 minutes ?"

        await livreur.send_json_to({"text": "Oui, j'arrive."})
        await livreur.receive_json_from(timeout=5)  # écho du livreur
        recu2 = await client.receive_json_from(timeout=5)

        assert recu2["type"] == "chat.message"
        assert recu2["sender"] == "courier"
        assert recu2["text"] == "Oui, j'arrive."

        await client.disconnect()
        await livreur.disconnect()

    @pytest.mark.asyncio
    async def test_un_message_invalide_ne_ferme_pas_le_socket(
        self, course: Assignment, customer: User
    ) -> None:
        """Répondre par une erreur, contrairement au suivi de position : ici,
        les deux parties ont le droit d'écrire, un message mal formé n'est
        donc pas le signe de quelqu'un qui outrepasse son rôle."""
        client = await connect(
            f"/ws/orders/{course.order_id}/chat/",
            token=await database_sync_to_async(token_for)(customer),
        )
        await client.connect()

        await client.send_json_to({"text": ""})
        recu = await client.receive_json_from(timeout=5)

        assert recu["type"] == "error"

        await client.disconnect()
