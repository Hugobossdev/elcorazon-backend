"""Images des catégories (lot 3, 2026-09-26).

Une catégorie porte une photo facultative, rangée au compartiment public des
plats. Le serveur borne ce qui y entre — poids et format — pour les catégories
comme pour les plats : la borne des 5 Mo ne vivait que dans l'écran du
back-office. Poser ou retirer la photo se lit au journal.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.catalog.models import Category, MenuItem
from apps.restaurants.models import Restaurant, StaffMembership
from apps.restaurants.states import RestaurantStatus
from common import uploads
from common.models import AuditEntry

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def _image(format_: str = "PNG", nom: str = "photo.png") -> SimpleUploadedFile:
    tampon = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(tampon, format=format_)
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type=f"image/{format_.lower()}")


@pytest.fixture
def siege() -> APIClient:
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege-img@elcorazon.test", "x"))
    return client


def _fiche(category: Category) -> str:
    return reverse("v1:catalog:managed-category-detail", args=[category.pk])


class TestPoser:
    def test_la_photo_est_publiee_et_journalisee(
        self, siege: APIClient, category: Category, restaurant: Restaurant
    ) -> None:
        reponse = siege.patch(_fiche(category), {"image": _image()}, format="multipart")

        assert reponse.status_code == 200, reponse.data
        assert reponse.data["image"]
        publique = APIClient().get(
            reverse("v1:catalog:category-list"), {"restaurant": restaurant.slug}
        )
        # La liste publique des catégories n'est pas paginée.
        carte = next(c for c in publique.data if c["id"] == str(category.pk))
        assert carte["image"] == reponse.data["image"]

        entree = AuditEntry.objects.get(action="category.image", target_id=str(category.pk))
        assert entree.before == {"image": None}
        assert entree.after["image"].startswith("categories/")
        assert entree.scope_restaurant_id == restaurant.pk

    def test_retirer_la_photo_se_journalise_aussi(
        self, siege: APIClient, category: Category
    ) -> None:
        siege.patch(_fiche(category), {"image": _image()}, format="multipart")

        reponse = siege.patch(_fiche(category), {"image": None}, format="json")

        assert reponse.status_code == 200
        assert reponse.data["image"] is None
        assert AuditEntry.objects.filter(action="category.image").count() == 2

    def test_changer_le_nom_seul_ne_journalise_pas_d_image(
        self, siege: APIClient, category: Category
    ) -> None:
        siege.patch(_fiche(category), {"name": "Grillades du soir"}, format="json")

        assert not AuditEntry.objects.filter(action="category.image").exists()


class TestBornes:
    def test_un_format_exotique_est_refuse(self, siege: APIClient, category: Category) -> None:
        reponse = siege.patch(
            _fiche(category), {"image": _image("GIF", "anime.gif")}, format="multipart"
        )

        assert reponse.status_code == 400
        assert "JPEG, PNG ou WebP" in str(reponse.data)
        category.refresh_from_db()
        assert not category.image

    def test_une_image_trop_lourde_est_refusee(
        self, siege: APIClient, category: Category, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(uploads, "POIDS_MAX_OCTETS", 10)

        reponse = siege.patch(_fiche(category), {"image": _image()}, format="multipart")

        assert reponse.status_code == 400
        assert "maximum" in str(reponse.data)

    def test_la_meme_borne_vaut_pour_la_photo_d_un_plat(
        self, siege: APIClient, menu_item: MenuItem
    ) -> None:
        # La règle vivait dans l'écran du back-office ; un appel direct passait.
        reponse = siege.patch(
            reverse("v1:catalog:managed-item-detail", args=[menu_item.pk]),
            {"image": _image("GIF", "anime.gif")},
            format="multipart",
        )

        assert reponse.status_code == 400


def test_une_autre_cuisine_ne_pose_pas_de_photo(restaurant: Restaurant, category: Category) -> None:
    voisine = Restaurant.objects.create(
        name="Voisine",
        slug="voisine-img",
        zone=restaurant.zone,
        address="x",
        location=restaurant.location,
        phone="+22890000007",
        status=RestaurantStatus.ACTIVE,
    )
    compte = User.objects.create_user("chef-img@elcorazon.test", "x", user_type=UserType.STAFF)
    compte.roles.add(
        Role.objects.create(name="Chef img", permissions=["catalog.read", "catalog.write"])
    )
    StaffMembership.objects.create(user=compte, restaurant=voisine)
    chef = APIClient()
    chef.force_authenticate(compte)

    reponse = chef.patch(_fiche(category), {"image": _image()}, format="multipart")

    assert reponse.status_code == 404
    category.refresh_from_db()
    assert not category.image
