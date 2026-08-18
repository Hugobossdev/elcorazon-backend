"""Cycle de vie des fichiers attachés aux modèles.

Django n'efface pas le fichier qu'un `FileField` vient de remplacer. Sur le
catalogue, l'oubli coûte du stockage ; sur les pièces des livreurs, il coûte
autre chose : un dossier rejeté est redéposé (invariant L5), si bien que chaque
pièce d'identité jamais envoyée s'accumulait dans le compartiment privé. Ces
tests tiennent les deux règles qui évitent cela — et surtout celle qui évite le
zèle inverse, l'effacement d'un fichier qu'on voulait garder.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from apps.catalog.models import MenuItem
from apps.delivery.models import CourierProfile
from common.money import Money

XOF = "XOF"

pytestmark = pytest.mark.django_db


def _image(nom: str = "burger.jpg", contenu: bytes = b"des-octets") -> ContentFile:
    return ContentFile(contenu, name=nom)


class TestRemplacement:
    """Le fichier remplacé s'en va, celui qu'on garde reste."""

    def test_l_ancienne_image_est_effacee(self, menu_item: MenuItem) -> None:
        menu_item.image = _image("premiere.jpg")
        menu_item.save()
        ancienne = menu_item.image.name

        menu_item.image = _image("seconde.jpg")
        menu_item.save()

        assert not storages["products"].exists(ancienne)
        assert storages["products"].exists(menu_item.image.name)

    def test_une_sauvegarde_sans_changement_ne_touche_pas_au_fichier(
        self, menu_item: MenuItem
    ) -> None:
        # Le piège de cette fonctionnalité : comparer autre chose que le nom du
        # fichier ferait disparaître l'image à la première modification du prix.
        menu_item.image = _image()
        menu_item.save()
        chemin = menu_item.image.name

        menu_item.name = "Burger royal"
        menu_item.save()

        assert menu_item.image.name == chemin
        assert storages["products"].exists(chemin)

    def test_retirer_l_image_efface_le_fichier(self, menu_item: MenuItem) -> None:
        menu_item.image = _image()
        menu_item.save()
        chemin = menu_item.image.name

        menu_item.image = None
        menu_item.save()

        assert not storages["products"].exists(chemin)

    def test_une_creation_n_efface_rien(self, category, restaurant) -> None:
        # `pk` est nul : il n'y a pas de ligne précédente à relire, et la
        # tentative de le faire lèverait `DoesNotExist`.
        article = MenuItem(
            restaurant=restaurant,
            category=category,
            name="Nouveau",
            slug="nouveau",
            price=Money(2_500, XOF),
            image=_image(),
        )
        article.save()

        assert storages["products"].exists(article.image.name)


class TestPiecesDesLivreurs:
    """Le cas qui motive tout ce fichier.

    Un dossier rejeté est redéposé (L5) : sans effacement du remplacé, chaque
    pièce d'identité jamais envoyée s'accumule dans le compartiment privé.
    """

    def test_une_piece_redeposee_efface_la_precedente(self, courier: CourierProfile) -> None:
        courier.id_document = _image("cni-v1.pdf")
        courier.save()
        premiere = courier.id_document.name

        courier.id_document = _image("cni-v2.pdf")
        courier.save()

        assert not storages["documents"].exists(premiere)
        assert storages["documents"].exists(courier.id_document.name)

    def test_les_trois_pieces_sont_suivies_independamment(
        self, courier: CourierProfile
    ) -> None:
        courier.id_document = _image("cni.pdf")
        courier.licence_document = _image("permis.pdf")
        courier.save()
        cni, permis = courier.id_document.name, courier.licence_document.name

        # Le livreur ne redépose que son permis : sa pièce d'identité n'a
        # aucune raison de disparaître.
        courier.licence_document = _image("permis-v2.pdf")
        courier.save()

        assert storages["documents"].exists(cni)
        assert not storages["documents"].exists(permis)

    def test_la_suppression_du_dossier_emporte_les_pieces(
        self, courier: CourierProfile
    ) -> None:
        # `CourierProfile` n'est pas à suppression logique : la ligne part
        # réellement, et ses pièces d'identité n'ont aucune raison de rester.
        courier.id_document = _image("cni.pdf")
        courier.save()
        chemin = courier.id_document.name

        courier.delete()

        assert not storages["documents"].exists(chemin)


class TestSuppressionLogique:
    """Un article retiré du catalogue **garde** son image, et c'est voulu."""

    def test_un_article_retire_garde_son_image(self, menu_item: MenuItem) -> None:
        # `SoftDeleteQuerySet.delete()` fait un `update(deleted_at=…)` : la
        # ligne reste, et une commande de l'an dernier continue de l'afficher.
        # Effacer le fichier ici afficherait un cadre vide dans un historique
        # que le modèle s'attache justement à préserver.
        menu_item.image = _image()
        menu_item.save()
        chemin = menu_item.image.name

        MenuItem.objects.filter(pk=menu_item.pk).delete()

        assert storages["products"].exists(chemin)
