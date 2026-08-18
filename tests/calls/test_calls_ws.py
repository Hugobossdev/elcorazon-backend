"""File personnelle et sonnerie d'appel — `ws/me/`.

Ce canal est le seul du projet qui ne soit pas rattaché à une ressource. Le
test qui compte est `test_l_appel_sonne_sans_ecran_ouvert_sur_la_commande` :
c'est précisément ce qu'un canal par commande ne sait pas faire, et la raison
d'être de `ws/me/`.
"""

from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.accounts.services import AuthService
from apps.calls.services import CallService
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from common.consumers import CLOSE_UNAUTHENTICATED
from config.asgi import application

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgis]


def token_for(user: User) -> str:
    return AuthService.issue_tokens(user).access


async def connect(path: str, *, token: str | None = None) -> WebsocketCommunicator:
    headers = [(b"origin", b"http://testserver")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return WebsocketCommunicator(application, path, headers=headers)


@pytest.fixture
def course(order: Order, courier: CourierProfile) -> Assignment:
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.ON_THE_WAY)
    order.refresh_from_db()
    return Assignment.objects.create(order=order, courier=courier, status=DeliveryStatus.ON_THE_WAY)


class TestAutorisation:
    @pytest.mark.asyncio
    async def test_sans_jeton_le_socket_est_refuse(self) -> None:
        communicator = await connect("/ws/me/")

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_le_groupe_vient_du_jeton_pas_de_l_url(self, customer: User) -> None:
        """Aucun identifiant dans le chemin, donc aucun à falsifier."""
        communicator = await connect(
            "/ws/me/", token=await database_sync_to_async(token_for)(customer)
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()


class TestSonnerie:
    @pytest.mark.asyncio
    async def test_l_appel_sonne_sans_ecran_ouvert_sur_la_commande(
        self, customer: User, courier: CourierProfile, course: Assignment
    ) -> None:
        """Le livreur n'écoute que sa file personnelle : il reçoit l'appel sans
        avoir rien ouvert sur la commande concernée."""
        communicator = await connect(
            "/ws/me/", token=await database_sync_to_async(token_for)(courier.user)
        )
        connected, _ = await communicator.connect()
        assert connected is True

        await database_sync_to_async(CallService.place)(order=course.order, caller=customer)

        event = await communicator.receive_json_from(timeout=5)

        assert event["type"] == "call.incoming"
        assert event["caller"] == str(customer.pk)
        assert event["order"] == str(course.order.pk)
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_l_appelant_est_prevenu_du_decrochage(
        self, customer: User, courier: CourierProfile, course: Assignment
    ) -> None:
        communicator = await connect(
            "/ws/me/", token=await database_sync_to_async(token_for)(customer)
        )
        await communicator.connect()

        call = await database_sync_to_async(CallService.place)(order=course.order, caller=customer)
        await database_sync_to_async(CallService.accept)(call=call, actor=courier.user)

        event = await communicator.receive_json_from(timeout=5)

        assert event["type"] == "call.accepted"
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_tiers_ne_recoit_pas_la_sonnerie(
        self, customer: User, courier: CourierProfile, course: Assignment
    ) -> None:
        intrus = await database_sync_to_async(User.objects.create_user)(
            "intrus.ws@elcorazon.test", "motdepasse", full_name="Kodjo Intrus"
        )
        communicator = await connect(
            "/ws/me/", token=await database_sync_to_async(token_for)(intrus)
        )
        await communicator.connect()

        await database_sync_to_async(CallService.place)(order=course.order, caller=customer)

        assert await communicator.receive_nothing(timeout=2) is True
        await communicator.disconnect()
