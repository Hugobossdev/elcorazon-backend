"""Social — S2 (visibilité), S3 (partage de commande), S4 (post de groupe privé).

`TestCapaciteConcurrente` rejoue la même course que F1 sur la fidélité, mais
sur l'adhésion à un groupe plutôt que sur un solde de points : deux personnes
qui rejoignent la dernière place au même instant ne doivent pas passer toutes
les deux.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from django.db import connection, connections, transaction
from django.db.utils import IntegrityError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from apps.social.models import (
    GroupKind,
    GroupMembership,
    Post,
    PostComment,
    PostKind,
    PostLike,
    SocialGroup,
)
from apps.social.services import GroupFull, InvalidInviteCode, PostRefused, SocialService
from tests.fixtures import build_order

pytestmark = pytest.mark.django_db


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(customer)
    return client


@pytest.fixture
def group(customer: User) -> SocialGroup:
    return SocialService.create_group(
        creator=customer, name="Famille Koffi", kind=GroupKind.FAMILY, max_members=2
    ).group


class TestAdhesion:
    def test_le_createur_est_deja_membre(self, group: SocialGroup, customer: User) -> None:
        assert GroupMembership.objects.filter(group=group, user=customer).exists()
        assert group.member_count == 1

    def test_un_code_valide_fait_rejoindre(self, group: SocialGroup, courier_user: User) -> None:
        membership = SocialService.join(user=courier_user, invite_code=group.invite_code)

        assert membership.group_id == group.pk
        group.refresh_from_db()
        assert group.member_count == 2

    def test_un_code_inconnu_est_refuse(self, courier_user: User) -> None:
        with pytest.raises(InvalidInviteCode):
            SocialService.join(user=courier_user, invite_code="INEXISTANT")

    def test_rejoindre_deux_fois_est_sans_effet(
        self, group: SocialGroup, courier_user: User
    ) -> None:
        SocialService.join(user=courier_user, invite_code=group.invite_code)
        SocialService.join(user=courier_user, invite_code=group.invite_code)

        group.refresh_from_db()
        assert group.member_count == 2
        assert GroupMembership.objects.filter(group=group, user=courier_user).count() == 1

    def test_le_groupe_complet_refuse_un_nouveau_membre(
        self, group: SocialGroup, courier_user: User, restaurant: Restaurant
    ) -> None:
        autre = User.objects.create_user("autre@elcorazon.test", "motdepasse", full_name="Autre")
        SocialService.join(user=courier_user, invite_code=group.invite_code)  # complète à 2/2

        with pytest.raises(GroupFull):
            SocialService.join(user=autre, invite_code=group.invite_code)

    def test_partir_rend_la_place(self, group: SocialGroup, courier_user: User) -> None:
        SocialService.join(user=courier_user, invite_code=group.invite_code)

        SocialService.leave(user=courier_user, group=group)

        group.refresh_from_db()
        assert group.member_count == 1
        assert not GroupMembership.objects.get(group=group, user=courier_user).is_active

    def test_partir_deux_fois_ne_rend_pas_la_place_deux_fois(
        self, group: SocialGroup, courier_user: User
    ) -> None:
        SocialService.join(user=courier_user, invite_code=group.invite_code)
        SocialService.leave(user=courier_user, group=group)
        SocialService.leave(user=courier_user, group=group)

        group.refresh_from_db()
        assert group.member_count == 1

    def test_rejoindre_apres_avoir_quitte_reactive_l_adhesion(
        self, group: SocialGroup, courier_user: User
    ) -> None:
        """Une adhésion inactive (départ) se réactive au lieu d'en créer une
        seconde — sinon `one_progress_per_user` n'aurait plus de sens et le
        départ perdrait toute trace de l'historique du membre."""
        SocialService.join(user=courier_user, invite_code=group.invite_code)
        SocialService.leave(user=courier_user, group=group)

        membership = SocialService.join(user=courier_user, invite_code=group.invite_code)

        assert membership.is_active
        group.refresh_from_db()
        assert group.member_count == 2
        assert GroupMembership.objects.filter(group=group, user=courier_user).count() == 1


@pytest.mark.django_db(transaction=True)
class TestCapaciteConcurrente:
    """La course prouvée sur F1, rejouée ici sur l'adhésion à un groupe."""

    def test_deux_adhesions_concurrentes_pour_une_place_n_en_reussissent_qu_une(self) -> None:
        createur = User.objects.create_user("createur@elcorazon.test", "motdepasse", full_name="C")
        result = SocialService.create_group(
            creator=createur, name="Une place", kind=GroupKind.CUSTOM, max_members=2
        )
        group = result.group

        candidats = [
            User.objects.create_user(f"c{i}@elcorazon.test", "motdepasse", full_name=f"C{i}")
            for i in range(2)
        ]

        def rejoindre(user: User) -> str:
            try:
                SocialService.join(user=user, invite_code=group.invite_code)
                return "ok"
            except GroupFull:
                return "complet"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = sorted(f.result() for f in [pool.submit(rejoindre, c) for c in candidats])

        assert resultats == ["complet", "ok"]
        group.refresh_from_db()
        assert group.member_count == 2


class TestPublications:
    def test_un_post_public_est_visible_de_tous(self, customer: User, courier_user: User) -> None:
        SocialService.create_post(author=customer, content="Bonjour")

        vus = Post.objects.filter(author=customer)
        assert vus.count() == 1

    def test_un_post_de_groupe_n_est_jamais_public(
        self, group: SocialGroup, customer: User
    ) -> None:
        post = SocialService.create_post(author=customer, content="Réunion", group=group)

        assert post.is_public is False

    def test_un_non_membre_ne_peut_pas_poster_dans_le_groupe(
        self, group: SocialGroup, courier_user: User
    ) -> None:
        with pytest.raises(PostRefused):
            SocialService.create_post(author=courier_user, content="Intrus", group=group)

    def test_partager_sa_propre_commande_est_permis(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        commande = build_order(restaurant, customer, reference="EC000001")

        post = SocialService.create_post(
            author=customer, content="Miam", kind=PostKind.ORDER_SHARE, order=commande
        )

        assert post.order_id == commande.pk

    def test_partager_la_commande_d_autrui_est_refuse(
        self, customer: User, courier_user: User, restaurant: Restaurant
    ) -> None:
        """S3 — l'implémentation précédente validait `order_id` par simple
        existence, exposant l'adresse de livraison d'un autre client."""
        commande_d_autrui = build_order(restaurant, courier_user, reference="EC000002")

        with pytest.raises(PostRefused):
            SocialService.create_post(
                author=customer, content="Vol", kind=PostKind.ORDER_SHARE, order=commande_d_autrui
            )

    def test_un_partage_sans_commande_est_refuse(self, customer: User) -> None:
        with pytest.raises(PostRefused):
            SocialService.create_post(author=customer, content="?", kind=PostKind.ORDER_SHARE)

    def test_le_modele_corrige_un_post_de_groupe_marque_public(
        self, group: SocialGroup, customer: User
    ) -> None:
        """S4 — la visibilité est dérivée du groupe, sur tout chemin d'écriture.

        Le service n'est pas le seul à créer des posts (back-office, admin,
        commande d'exploitation). Un `is_public=True` explicite sur un post de
        groupe heurtait `group_post_not_public` et rendait un `IntegrityError` ;
        il est maintenant corrigé avant l'insertion.
        """
        post = Post.objects.create(author=customer, content="X", group=group, is_public=True)

        assert post.is_public is False
        post.refresh_from_db()
        assert post.is_public is False

    def test_rattacher_un_post_existant_a_un_groupe_le_rend_prive(
        self, group: SocialGroup, customer: User
    ) -> None:
        """Le déplacement vers un groupe est aussi un chemin vers la violation."""
        post = Post.objects.create(author=customer, content="X")
        assert post.is_public is True

        post.group = group
        post.save(update_fields=["group"])

        post.refresh_from_db()
        assert post.is_public is False

    def test_la_base_refuse_un_post_de_groupe_marque_public(
        self, group: SocialGroup, customer: User
    ) -> None:
        """S4, en dernier ressort (ADR-010) : la contrainte tient toujours pour
        les chemins qui contournent `save()`, comme `bulk_create`."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Post.objects.bulk_create(
                [Post(author=customer, content="X", group=group, is_public=True)]
            )


class TestVisibilite:
    """S2 — un post de groupe est invisible, jamais refusé, à qui n'en est pas membre."""

    def test_le_fil_d_un_membre_montre_son_post_de_groupe_et_le_public(
        self, as_customer: APIClient, customer: User, group: SocialGroup
    ) -> None:
        SocialService.create_post(author=customer, content="Public")
        SocialService.create_post(author=customer, content="Privé", group=group)

        response = as_customer.get(reverse("v1:social:post-list"))

        contenus = {p["content"] for p in response.data["results"]}
        assert contenus == {"Public", "Privé"}  # l'auteur est membre du groupe

    def test_un_non_membre_ne_voit_pas_le_post_du_groupe(self, group: SocialGroup) -> None:
        SocialService.create_post(author=group.creator, content="Réunion de famille", group=group)
        tiers = User.objects.create_user("tiers@elcorazon.test", "motdepasse", full_name="Tiers")
        client = APIClient()
        client.force_authenticate(tiers)

        response = client.get(reverse("v1:social:post-list"))

        assert response.data["results"] == []

    def test_un_non_membre_ne_peut_pas_atteindre_le_post_par_son_id(
        self, group: SocialGroup
    ) -> None:
        post = SocialService.create_post(author=group.creator, content="Réunion", group=group)
        tiers = User.objects.create_user("tiers@elcorazon.test", "motdepasse", full_name="Tiers")
        client = APIClient()
        client.force_authenticate(tiers)

        response = client.get(reverse("v1:social:post-detail", args=[post.pk]))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestLikesEtCommentaires:
    def test_aimer_incremente_le_compteur(self, customer: User, courier_user: User) -> None:
        post = SocialService.create_post(author=customer, content="Bonjour")

        liked = SocialService.toggle_like(user=courier_user, post=post)

        assert liked is True
        post.refresh_from_db()
        assert post.likes_count == 1

    def test_aimer_deux_fois_retire_le_j_aime(self, customer: User, courier_user: User) -> None:
        post = SocialService.create_post(author=customer, content="Bonjour")

        SocialService.toggle_like(user=courier_user, post=post)
        liked = SocialService.toggle_like(user=courier_user, post=post)

        assert liked is False
        post.refresh_from_db()
        assert post.likes_count == 0
        assert not PostLike.objects.filter(post=post, user=courier_user).exists()

    @pytest.mark.django_db(transaction=True)
    def test_deux_j_aimes_concurrents_ne_comptent_qu_une_fois(
        self, customer: User, courier_user: User
    ) -> None:
        """Même course que `TestCapaciteConcurrente`, sur le j'aime plutôt que
        sur la place : la contrainte d'unicité tranche, et la requête qui la
        heurte doit lire « déjà aimé », pas planter."""
        post = SocialService.create_post(author=customer, content="Bonjour")

        def aimer() -> str:
            try:
                aime = SocialService.toggle_like(user=courier_user, post=post)
                return "aimé" if aime else "retiré"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultats = [f.result() for f in [pool.submit(aimer) for _ in range(2)]]

        assert resultats == ["aimé", "aimé"]
        assert PostLike.objects.filter(post=post, user=courier_user).count() == 1
        post.refresh_from_db()
        assert post.likes_count == 1

    def test_un_j_aime_concurrent_est_reutilise_sans_heurter_la_contrainte(
        self, customer: User, courier_user: User
    ) -> None:
        """La course de `test_deux_j_aimes_concurrents`, rendue déterministe.

        Le test concurrent ne l'attrape qu'avec de la chance : il faut que les
        deux fils tombent exactement dans la fenêtre. Ici la fenêtre est forcée
        — le j'aime existe déjà, mais la lecture initiale ne le voit pas —, ce
        qui reproduit à coup sûr le chemin qui posait problème.

        Le `create` rattrapé qui précédait écrivait d'abord et avalait
        l'erreur : chaque passage laissait un `duplicate key value violates
        unique constraint "one_like_per_user"` dans le journal PostgreSQL, et
        posait `needs_rollback` sur la connexion — d'où une sortie du bloc
        `atomic` par un rollback silencieux plutôt que par un commit.

        L'assertion porte donc sur les requêtes réellement émises, seule trace
        observable de la différence : `get_or_create` résout le cas par un
        `SELECT`, sans tenter l'insertion vouée à échouer.
        """
        post = SocialService.create_post(author=customer, content="Bonjour")

        # Le concurrent a gagné la course, et le compteur porte déjà son j'aime.
        PostLike.objects.create(post=post, user=courier_user)
        Post.objects.filter(pk=post.pk).update(likes_count=1)

        vrai_filter = PostLike.objects.filter
        appels = {"n": 0}

        def lecture_aveugle(*args: object, **kwargs: object) -> object:
            appels["n"] += 1
            if appels["n"] == 1:
                # La lecture initiale de `toggle_like` : elle ne voit rien.
                return mock.Mock(first=mock.Mock(return_value=None))
            return vrai_filter(*args, **kwargs)

        with (
            mock.patch.object(PostLike.objects, "filter", side_effect=lecture_aveugle),
            CaptureQueriesContext(connection) as capture,
        ):
            resultat = SocialService.toggle_like(user=courier_user, post=post)

        assert resultat is True

        inserts = [
            q["sql"]
            for q in capture.captured_queries
            if "INSERT" in q["sql"] and "social_postlike" in q["sql"]
        ]
        assert not inserts, f"Insertion vouée à violer one_like_per_user : {inserts}"

        # Le j'aime concurrent est réutilisé, pas dupliqué…
        assert PostLike.objects.filter(post=post, user=courier_user).count() == 1
        # …et le compteur n'a pas compté deux fois un unique j'aime.
        post.refresh_from_db()
        assert post.likes_count == 1

    def test_commenter_incremente_le_compteur(self, customer: User, courier_user: User) -> None:
        post = SocialService.create_post(author=customer, content="Bonjour")

        SocialService.add_comment(user=courier_user, post=post, content="Superbe photo")

        post.refresh_from_db()
        assert post.comments_count == 1
        assert PostComment.objects.filter(post=post, content="Superbe photo").exists()


class TestRoutes:
    def test_creer_un_groupe(self, as_customer: APIClient) -> None:
        response = as_customer.post(
            reverse("v1:social:group-list"), {"name": "Amis", "kind": GroupKind.FRIENDS}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["member_count"] == 1
        assert "invite_code" in response.data

    def test_rejoindre_par_le_code(self, group: SocialGroup) -> None:
        tiers = User.objects.create_user("tiers@elcorazon.test", "motdepasse", full_name="Tiers")
        client = APIClient()
        client.force_authenticate(tiers)

        response = client.post(reverse("v1:social:group-join"), {"invite_code": group.invite_code})

        assert response.status_code == status.HTTP_200_OK
        assert response.data["member_count"] == 2

    def test_publier_puis_aimer(self, as_customer: APIClient, customer: User) -> None:
        creation = as_customer.post(reverse("v1:social:post-list"), {"content": "Bonjour"})
        post_id = creation.data["id"]

        response = as_customer.post(reverse("v1:social:post-like", args=[post_id]))

        assert response.data == {"liked": True, "likes_count": 1}

    def test_un_anonyme_n_a_rien(self) -> None:
        response = APIClient().get(reverse("v1:social:post-list"))

        assert response.status_code in (401, 403)

    def test_lister_ne_montre_que_ses_propres_groupes(
        self, as_customer: APIClient, group: SocialGroup, restaurant: Restaurant
    ) -> None:
        """`SocialGroupViewSet.get_queryset` filtre par adhésion active — un
        groupe où le client n'est pas membre n'a rien à montrer ici, pas même
        son existence."""
        autre_createur = User.objects.create_user(
            "autre-groupe@elcorazon.test", "motdepasse", full_name="Autre"
        )
        SocialService.create_group(
            creator=autre_createur, name="Pas les miens", kind=GroupKind.FRIENDS, max_members=5
        )

        response = as_customer.get(reverse("v1:social:group-list"))

        noms = {g["name"] for g in response.data["results"]}
        assert noms == {"Famille Koffi"}

    def test_quitter_par_la_route(self, as_customer: APIClient, group: SocialGroup) -> None:
        response = as_customer.post(reverse("v1:social:group-leave", args=[group.pk]))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not GroupMembership.objects.get(group=group, user=group.creator).is_active

    def test_lister_les_commentaires_par_la_route(
        self, as_customer: APIClient, customer: User, courier_user: User
    ) -> None:
        post = SocialService.create_post(author=customer, content="Bonjour")
        SocialService.add_comment(user=courier_user, post=post, content="Superbe photo")

        response = as_customer.get(reverse("v1:social:post-comments", args=[post.pk]))

        assert response.status_code == status.HTTP_200_OK
        assert [c["content"] for c in response.data["results"]] == ["Superbe photo"]

    def test_commenter_par_la_route(self, as_customer: APIClient, customer: User) -> None:
        post = SocialService.create_post(author=customer, content="Bonjour")

        response = as_customer.post(
            reverse("v1:social:post-comments", args=[post.pk]), {"content": "Joli !"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        post.refresh_from_db()
        assert post.comments_count == 1
