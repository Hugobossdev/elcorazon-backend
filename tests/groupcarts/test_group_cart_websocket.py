"""Synchronisation temps réel du panier collaboratif — ADR-008.

C'est le cas d'usage qui justifie le plus directement un WebSocket dans ce
projet : plusieurs personnes modifient la même chose en même temps.

Le test décisif est `test_un_non_membre_est_refuse` : dans l'implémentation
précédente, l'abonnement temps réel donnait accès à des *lignes* et non à un
périmètre métier, si bien que connaître un identifiant suffisait à écouter — voire
à écrire dans — le panier d'un autre groupe.
"""

from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User, UserType
from apps.accounts.services import AuthService
from apps.catalog.models import MenuItem
from apps.groupcarts.models import GroupCart
from apps.groupcarts.services import GroupCartService
from apps.restaurants.models import Restaurant
from common.consumers import CLOSE_FORBIDDEN, CLOSE_UNAUTHENTICATED
from config.asgi import application

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgis]


def token_for(user: User) -> str:
    return AuthService.issue_tokens(user).access


async def connect(group_cart_id: object, *, token: str | None = None) -> WebsocketCommunicator:
    headers = [(b"origin", b"http://testserver")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return WebsocketCommunicator(application, f"/ws/group-carts/{group_cart_id}/", headers=headers)


@database_sync_to_async
def open_cart(host: User, restaurant: Restaurant) -> GroupCart:
    return GroupCartService.open(host=host, restaurant=restaurant, title="Déjeuner d'équipe")


@database_sync_to_async
def make_user(email: str, *, user_type: str = UserType.CUSTOMER) -> User:
    return User.objects.create_user(
        email, "motdepasse", full_name="Participante", user_type=user_type
    )


class TestAutorisationALaConnexion:
    @pytest.mark.asyncio
    async def test_sans_jeton_le_socket_est_ferme(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        group_cart = await open_cart(customer, restaurant)

        connected, code = await (await connect(group_cart.pk)).connect()

        assert connected is False
        assert code == CLOSE_UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_l_hote_est_accepte(self, customer: User, restaurant: Restaurant) -> None:
        """L'hôte n'est pas traité à part : il est membre comme les autres,
        inscrit à l'ouverture."""
        group_cart = await open_cart(customer, restaurant)
        communicator = await connect(
            group_cart.pk, token=await database_sync_to_async(token_for)(customer)
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_participant_invite_est_accepte(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        group_cart = await open_cart(customer, restaurant)
        invitee = await make_user("invite.ws@elcorazon.test")
        await database_sync_to_async(GroupCartService.join)(group_cart=group_cart, user=invitee)

        communicator = await connect(
            group_cart.pk, token=await database_sync_to_async(token_for)(invitee)
        )

        connected, _ = await communicator.connect()

        assert connected is True
        await communicator.disconnect()

    @pytest.mark.asyncio
    async def test_un_non_membre_est_refuse(self, customer: User, restaurant: Restaurant) -> None:
        """Connaître l'identifiant d'un panier ne doit pas suffire à écouter le
        déjeuner d'un autre groupe."""
        group_cart = await open_cart(customer, restaurant)
        etrangere = await make_user("etrangere.ws@elcorazon.test")

        communicator = await connect(
            group_cart.pk, token=await database_sync_to_async(token_for)(etrangere)
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_un_panier_inexistant_ferme_le_socket(self, customer: User) -> None:
        communicator = await connect(
            "00000000-0000-0000-0000-000000000000",
            token=await database_sync_to_async(token_for)(customer),
        )

        connected, code = await communicator.connect()

        assert connected is False
        assert code == CLOSE_FORBIDDEN


class TestDiffusionDesContributions:
    @pytest.mark.asyncio
    async def test_l_ajout_d_un_participant_atteint_les_autres(
        self, customer: User, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """Sans cette diffusion, chacun devrait interroger l'API en boucle pendant
        tout le temps que met un groupe à se décider — c'est-à-dire longtemps."""
        group_cart = await open_cart(customer, restaurant)
        invitee = await make_user("invite.diffusion@elcorazon.test")
        await database_sync_to_async(GroupCartService.join)(group_cart=group_cart, user=invitee)

        ecoute = await connect(
            group_cart.pk, token=await database_sync_to_async(token_for)(customer)
        )
        await ecoute.connect()

        await database_sync_to_async(GroupCartService.add_line)(
            group_cart=group_cart, member=invitee, menu_item=menu_item, quantity=2, options=[]
        )

        recu = await ecoute.receive_json_from(timeout=5)

        assert recu["type"] == "groupcart.line_added"
        assert recu["member"] == str(invitee.pk)
        assert recu["item_name"] == menu_item.name
        assert recu["quantity"] == 2

        await ecoute.disconnect()

    @pytest.mark.asyncio
    async def test_l_arrivee_d_un_participant_est_annoncee(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        group_cart = await open_cart(customer, restaurant)
        ecoute = await connect(
            group_cart.pk, token=await database_sync_to_async(token_for)(customer)
        )
        await ecoute.connect()

        invitee = await make_user("invite.arrivee@elcorazon.test")
        await database_sync_to_async(GroupCartService.join)(group_cart=group_cart, user=invitee)

        recu = await ecoute.receive_json_from(timeout=5)

        assert recu["type"] == "groupcart.member_joined"
        assert recu["member"] == str(invitee.pk)

        await ecoute.disconnect()

    @pytest.mark.asyncio
    async def test_un_autre_panier_ne_recoit_rien(
        self, customer: User, restaurant: Restaurant, menu_item: MenuItem
    ) -> None:
        """Deux déjeuners simultanés ne doivent pas se voir l'un l'autre."""
        mien = await open_cart(customer, restaurant)
        autre_hote = await make_user("hote.autre@elcorazon.test")
        autre = await open_cart(autre_hote, restaurant)

        ecoute = await connect(autre.pk, token=await database_sync_to_async(token_for)(autre_hote))
        await ecoute.connect()

        await database_sync_to_async(GroupCartService.add_line)(
            group_cart=mien, member=customer, menu_item=menu_item, quantity=1, options=[]
        )

        assert await ecoute.receive_nothing(timeout=0.5)

        await ecoute.disconnect()
