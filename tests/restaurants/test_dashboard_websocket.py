"""Tableau de bord temps réel du personnel — ADR-008.

Même exigence que le suivi de commande : le rattachement à l'établissement
fait le droit, pas le type de compte. Un opérateur d'un autre établissement ne
doit rien recevoir, même en connaissant l'identifiant du restaurant.
"""

from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.accounts.models import Role, User, UserType
from apps.accounts.services import AuthService
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.orders.states import OrderStatus
from apps.restaurants.models import Restaurant, StaffMembership
from common.consumers import CLOSE_FORBIDDEN, CLOSE_UNAUTHENTICATED
from config.asgi import application

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgis]


def token_for(user: User) -> str:
    return AuthService.issue_tokens(user).access


async def connect(path: str, *, token: str | None = None) -> WebsocketCommunicator:
    headers = [(b"origin", b"http://testserver")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return WebsocketCommunicator(application, path, headers=headers)


@database_sync_to_async
def staff_for(restaurant: Restaurant, *, permission: str = "orders.read") -> User:
    user = User.objects.create_user(
        "staff@elcorazon.test", "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name="Lecture", permissions=[permission]))
    StaffMembership.objects.create(user=user, restaurant=restaurant)
    return user


class TestAutorisationALaConnexion:
    @pytest.mark.asyncio
    async def test_sans_jeton_le_socket_est_ferme(self, restaurant: Restaurant) -> None:
        communicator = await connect(f"/ws/restaurants/{restaurant.pk}/dashboard/")

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_le_personnel_rattache_est_accepte(self, restaurant: Restaurant) -> None:
        user = await staff_for(restaurant)
        communicator = await connect(
            f"/ws/restaurants/{restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_client_est_refuse(self, restaurant: Restaurant, customer: User) -> None:
        communicator = await connect(
            f"/ws/restaurants/{restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_le_personnel_d_un_autre_etablissement_est_refuse(
        self, restaurant: Restaurant, zone: object
    ) -> None:
        """La faille en une phrase : connaître l'identifiant d'un restaurant ne
        doit pas suffire à en surveiller les commandes."""

        @database_sync_to_async
        def ailleurs() -> Restaurant:
            return Restaurant.objects.create(
                name="El Corazón Kara",
                slug="ec-kara",
                zone=zone,
                address="Kara",
                location=restaurant.location,
                phone="+22890000021",
            )

        autre_restaurant = await ailleurs()
        user = await staff_for(autre_restaurant)

        communicator = await connect(
            f"/ws/restaurants/{restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_sans_la_permission_orders_read_le_personnel_est_refuse(
        self, restaurant: Restaurant
    ) -> None:
        user = await staff_for(restaurant, permission="catalog.write")

        communicator = await connect(
            f"/ws/restaurants/{restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(user),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_un_etablissement_inexistant_ferme_le_socket(self, customer: User) -> None:
        # `customer` sert uniquement à obtenir un jeton valide ; le refus tient
        # à l'établissement, vérifié avant tout rattachement.
        communicator = await connect(
            "/ws/restaurants/00000000-0000-0000-0000-000000000000/dashboard/",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN


class TestDiffusionDesCommandes:
    @pytest.mark.asyncio
    async def test_un_changement_de_statut_atteint_le_tableau_de_bord(
        self, order: Order, restaurant: Restaurant
    ) -> None:
        """C'est le lien qui manquait : `OrderService.transition_to` ne
        parlait qu'au client et au livreur (`order.{id}.tracking`). Le
        personnel doit apprendre la même chose sans recharger l'écran."""
        user = await staff_for(restaurant)
        dashboard = await connect(
            f"/ws/restaurants/{restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(user),
        )
        await dashboard.connect()

        await database_sync_to_async(OrderService.transition_to)(
            order=order, target=OrderStatus.CONFIRMED
        )

        recu = await dashboard.receive_json_from(timeout=5)

        assert recu["type"] == "order.status"
        assert recu["order"] == str(order.pk)
        assert recu["status"] == OrderStatus.CONFIRMED

        await dashboard.disconnect()

    @pytest.mark.asyncio
    async def test_le_personnel_d_un_autre_etablissement_ne_recoit_rien(
        self, order: Order, restaurant: Restaurant, zone: object
    ) -> None:
        @database_sync_to_async
        def ailleurs() -> Restaurant:
            return Restaurant.objects.create(
                name="El Corazón Kara",
                slug="ec-kara-2",
                zone=zone,
                address="Kara",
                location=restaurant.location,
                phone="+22890000022",
            )

        autre_restaurant = await ailleurs()
        user = await staff_for(autre_restaurant)
        dashboard = await connect(
            f"/ws/restaurants/{autre_restaurant.pk}/dashboard/",
            token=await database_sync_to_async(token_for)(user),
        )
        await dashboard.connect()

        await database_sync_to_async(OrderService.transition_to)(
            order=order, target=OrderStatus.CONFIRMED
        )

        assert await dashboard.receive_nothing(timeout=0.5)

        await dashboard.disconnect()
