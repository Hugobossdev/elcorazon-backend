"""API des avis — invariants S1 et S5.

Deux tests portent cette suite :

* `test_le_client_ne_peut_pas_se_declarer_acheteur_verifie` — S1. Dans
  l'implémentation précédente, la colonne existait au schéma et n'était jamais
  remplie ; ici elle est décidée par le serveur, et le champ est absent du
  sérialiseur d'entrée.
* `test_un_seul_avis_par_article_et_par_client` — S5, tenu par une contrainte
  d'unicité en base, pas par une vérification qu'on peut oublier.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import MenuItem, Review
from apps.catalog.services import ReviewService, record_purchase

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    """Client HTTP **distinct** de la fixture `client`.

    Les réutiliser ferait qu'un `force_authenticate` plus loin dans le test
    changerait aussi l'identité de celui-ci — deux acteurs qui n'en font qu'un,
    et un test qui ne vérifie plus ce qu'il annonce.
    """
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


def payload(menu_item: MenuItem, **overrides: object) -> dict[str, object]:
    return {"menu_item": str(menu_item.pk), "rating": 5, "comment": "Excellent.", **overrides}


class TestDepot:
    def test_un_client_authentifie_depose_un_avis(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        response = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["rating"] == 5
        assert response.data["user"]["full_name"] == "Ama Koffi"

    def test_un_visiteur_anonyme_ne_peut_pas_deposer(
        self, client: APIClient, menu_item: MenuItem
    ) -> None:
        response = client.post(reverse("v1:catalog:review-list"), payload(menu_item), format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_un_livreur_ne_note_pas_un_plat(
        self, client: APIClient, courier_user: User, menu_item: MenuItem
    ) -> None:
        """L'avis est un geste de client. Ni le personnel ni un livreur ne
        notent au nom de la clientèle."""
        client.force_authenticate(courier_user)

        response = client.post(reverse("v1:catalog:review-list"), payload(menu_item), format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("note", [0, 6, -1])
    def test_une_note_hors_bornes_est_refusee(
        self, as_customer: APIClient, menu_item: MenuItem, note: int
    ) -> None:
        response = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item, rating=note), format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_article_supprime_n_accepte_plus_d_avis(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        """Il reste lisible dans les commandes passées, mais il a quitté le
        catalogue : on ne note pas un plat qui n'y est plus."""
        menu_item.delete()

        response = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item), format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_seul_avis_par_article_et_par_client(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        """S5 — le refus est lisible (409 et code stable) plutôt qu'une
        violation d'intégrité remontée en 500."""
        as_customer.post(reverse("v1:catalog:review-list"), payload(menu_item), format="json")

        second = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item, rating=1), format="json"
        )

        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.data["code"] == "business_rule_violation"
        assert Review.objects.filter(menu_item=menu_item).count() == 1


class TestAchatVerifie:
    def test_sans_achat_l_avis_reste_autorise_mais_non_verifie(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        """Choix produit hérité : l'avis sans achat est permis, simplement pas
        marqué. Le durcir est une décision à prendre, pas un correctif."""
        response = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item), format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["is_verified_purchase"] is False

    def test_un_achat_enregistre_marque_l_avis(
        self, as_customer: APIClient, customer: User, menu_item: MenuItem
    ) -> None:
        record_purchase(user=customer, menu_item=menu_item, moment=timezone.now())

        response = as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item), format="json"
        )

        assert response.data["is_verified_purchase"] is True

    def test_le_client_ne_peut_pas_se_declarer_acheteur_verifie(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        """S1 — le champ est absent du sérialiseur d'entrée. Ce n'est pas une
        validation qu'on peut oublier d'écrire, c'est un champ qu'aucune
        requête ne peut porter."""
        response = as_customer.post(
            reverse("v1:catalog:review-list"),
            payload(menu_item, is_verified_purchase=True),
            format="json",
        )

        assert response.data["is_verified_purchase"] is False

    def test_l_achat_est_idempotent(self, customer: User, menu_item: MenuItem) -> None:
        """Dix commandes du même burger n'écrivent qu'une ligne, dont seule la
        date se rafraîchit."""
        premier = record_purchase(user=customer, menu_item=menu_item, moment=timezone.now())
        second = record_purchase(user=customer, menu_item=menu_item, moment=timezone.now())

        assert premier.pk == second.pk
        assert second.last_purchased_at > premier.last_purchased_at


class TestAgregatsDeNote:
    def test_la_note_de_l_article_suit_les_avis(
        self, as_customer: APIClient, customer: User, menu_item: MenuItem
    ) -> None:
        autre = User.objects.create_user("bea@elcorazon.test", "motdepasse", full_name="Béa")
        ReviewService.submit(user=autre, menu_item=menu_item, rating=4)

        as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item, rating=5), format="json"
        )
        menu_item.refresh_from_db()

        assert menu_item.rating_count == 2
        assert float(menu_item.rating_average) == 4.5

    def test_le_recalcul_est_complet_et_non_glissant(
        self, customer: User, menu_item: MenuItem
    ) -> None:
        """Une moyenne entretenue par incréments dérive dès qu'un avis est
        supprimé, et l'écart ne se voit jamais."""
        avis = ReviewService.submit(user=customer, menu_item=menu_item, rating=1)
        avis.delete()

        ReviewService.refresh_rating(menu_item)
        menu_item.refresh_from_db()

        assert menu_item.rating_count == 0
        assert float(menu_item.rating_average) == 0

    def test_l_article_rendu_porte_deja_la_note_a_jour(
        self, as_customer: APIClient, menu_item: MenuItem
    ) -> None:
        as_customer.post(
            reverse("v1:catalog:review-list"), payload(menu_item, rating=3), format="json"
        )

        fiche = as_customer.get(reverse("v1:catalog:item-detail", args=[menu_item.pk])).data

        assert fiche["rating_count"] == 1
        assert float(fiche["rating_average"]) == 3.0


class TestLecture:
    def test_les_avis_sont_lisibles_sans_compte(
        self, client: APIClient, customer: User, menu_item: MenuItem
    ) -> None:
        ReviewService.submit(user=customer, menu_item=menu_item, rating=5, comment="Parfait.")

        response = client.get(reverse("v1:catalog:review-list"), {"menu_item": str(menu_item.pk)})

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1

    def test_l_auteur_est_reduit_a_ce_qu_un_ecran_public_montre(
        self, client: APIClient, customer: User, menu_item: MenuItem
    ) -> None:
        """Y joindre le contact de l'auteur transformerait la page menu en
        annuaire de clients."""
        ReviewService.submit(user=customer, menu_item=menu_item, rating=5)

        auteur = client.get(reverse("v1:catalog:review-list")).data["results"][0]["user"]

        assert set(auteur) == {"id", "full_name", "avatar"}

    def test_ni_modification_ni_suppression(
        self, as_customer: APIClient, customer: User, menu_item: MenuItem
    ) -> None:
        """Ce sont des gestes de modération : ils appellent une trace d'audit
        et une permission dédiée, pas un verbe ouvert au client."""
        avis = ReviewService.submit(user=customer, menu_item=menu_item, rating=5)
        url = f"{reverse('v1:catalog:review-list')}{avis.pk}/"

        assert as_customer.patch(url, {"rating": 1}, format="json").status_code == (
            status.HTTP_404_NOT_FOUND
        )
        assert as_customer.delete(url).status_code == status.HTTP_404_NOT_FOUND
