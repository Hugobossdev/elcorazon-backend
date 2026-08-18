"""Stockage objet — ce qui est public, ce qui ne l'est jamais, et la seule
porte par laquelle on y accède.

L'enjeu n'est pas la couverture : c'est la **frontière**. Une image de burger
servie sans signature est une bonne chose ; une pièce d'identité servie sans
signature est un incident. Ces tests fixent laquelle est laquelle, et vérifient
qu'aucun site d'appel ne peut inverser les deux par étourderie.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from django.test import override_settings

from common.storage import (
    PRIVATE_BUCKETS,
    PUBLIC_BUCKETS,
    BannerStorage,
    CourierDocumentStorage,
    ProductImageStorage,
    StorageService,
    UnknownBucket,
    UserMediaStorage,
    bucket_name,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]

STOCKAGE_REEL = {
    "STORAGE_ENDPOINT_URL": "http://minio:9000",
    "STORAGE_REGION": "us-east-1",
    "STORAGE_ACCESS_KEY": "cle",
    "STORAGE_SECRET_KEY": "secret",
    "STORAGE_USE_SSL": False,
    "STORAGE_ADDRESSING_STYLE": "path",
    "STORAGE_SIGNED_URL_EXPIRE": 900,
    "STORAGE_BUCKETS": {
        "products": "test-products",
        "banners": "test-banners",
        "users": "test-users",
        "documents": "test-documents",
    },
}


class TestVisibilite:
    """Public ou privé — décidé par la classe, jamais par l'appelant."""

    @pytest.mark.parametrize(
        "classe",
        [ProductImageStorage, BannerStorage, UserMediaStorage],
    )
    def test_les_medias_du_catalogue_sont_publics(self, classe: type) -> None:
        with override_settings(**STOCKAGE_REEL):
            stockage = classe()

        # Sans signature : une URL d'image doit pouvoir être mise en cache par
        # un navigateur, un CDN, et retrouvée dans un favori le lendemain.
        assert stockage.querystring_auth is False

    def test_les_documents_livreurs_sont_prives(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            stockage = CourierDocumentStorage()

        # Chaque lecture est signée et expire. C'est ce qui manquait à
        # l'implémentation précédente, où les pièces d'identité vivaient dans
        # un compartiment public — lisibles indéfiniment par qui connaissait
        # l'adresse.
        assert stockage.querystring_auth is True
        assert stockage.querystring_expire == 900

    def test_le_stockage_par_defaut_est_prive(self) -> None:
        """Sécurité par défaut : un champ fichier ajouté sans stockage
        explicite atterrit dans le compartiment signé, pas en libre accès.

        Vérifié sur la source des réglages communs : la suite substitue un
        stockage en mémoire, donc `settings.STORAGES` ne dit rien de ce qui
        sert en production."""
        source = (BACKEND_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        assert '"default": {"BACKEND": "common.storage.CourierDocumentStorage"}' in source

    def test_aucun_compartiment_n_est_a_la_fois_public_et_prive(self) -> None:
        assert not set(PUBLIC_BUCKETS) & set(PRIVATE_BUCKETS)

    def test_les_documents_ne_sont_pas_dans_un_compartiment_public(self) -> None:
        # La séparation est portée par le compartiment, parce que c'est lui qui
        # porte la politique de lecture. Ranger les documents avec les images
        # rendrait la frontière inexprimable.
        assert "documents" in PRIVATE_BUCKETS
        assert "documents" not in PUBLIC_BUCKETS


class TestCompartiments:
    def test_les_noms_viennent_des_reglages(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            assert bucket_name("products") == "test-products"
            assert bucket_name("documents") == "test-documents"

    def test_un_alias_inconnu_echoue_bruyamment(self) -> None:
        """Plutôt qu'un compartiment inventé, dans lequel des fichiers
        partiraient sans que personne ne les retrouve."""
        with override_settings(**STOCKAGE_REEL), pytest.raises(UnknownBucket):
            bucket_name("inexistant")

    def test_chaque_domaine_a_son_compartiment(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            noms = {
                ProductImageStorage().bucket_name,
                BannerStorage().bucket_name,
                UserMediaStorage().bucket_name,
                CourierDocumentStorage().bucket_name,
            }

        assert len(noms) == 4


class TestUrlPublique:
    def test_l_adresse_publique_prime_sur_le_point_d_acces_interne(self) -> None:
        """En production, l'API parle à MinIO par le réseau Docker
        (`http://minio:9000`), que personne d'autre n'atteint. Sans cette
        substitution, les applications recevraient des URL d'images
        injoignables depuis un téléphone."""
        with override_settings(**STOCKAGE_REEL, STORAGE_PUBLIC_BASE_URL="https://cdn.example.com"):
            url = ProductImageStorage().url("menu/burger.jpg")

        assert url == "https://cdn.example.com/test-products/menu/burger.jpg"

    def test_l_adresse_publique_ne_porte_jamais_de_signature(self) -> None:
        with override_settings(**STOCKAGE_REEL, STORAGE_PUBLIC_BASE_URL="https://cdn.example.com"):
            url = ProductImageStorage().url("menu/burger.jpg")

        assert "X-Amz-Signature" not in url
        assert "?" not in url

    def test_les_espaces_et_accents_sont_encodes(self) -> None:
        with override_settings(**STOCKAGE_REEL, STORAGE_PUBLIC_BASE_URL="https://cdn.example.com"):
            url = ProductImageStorage().url("menu/poulet braisé.jpg")

        assert " " not in url
        assert url.startswith("https://cdn.example.com/test-products/menu/")

    def test_la_barre_finale_de_l_adresse_ne_double_pas(self) -> None:
        with override_settings(**STOCKAGE_REEL, STORAGE_PUBLIC_BASE_URL="https://cdn.example.com/"):
            url = ProductImageStorage().url("menu/burger.jpg")

        assert "//test-products" not in url

    def test_un_document_prive_ignore_l_adresse_publique(self) -> None:
        """Même configurée, elle ne s'applique pas : un document se lit par une
        URL signée ou pas du tout."""
        with override_settings(**STOCKAGE_REEL, STORAGE_PUBLIC_BASE_URL="https://cdn.example.com"):
            stockage = CourierDocumentStorage()

        assert stockage.is_public is False
        assert stockage.querystring_auth is True


class _ClientFactice:
    """Serveur S3 simulé — de quoi vérifier ce qui lui est demandé.

    Aucun octet ne part sur le réseau : ce qui est testé ici est la **décision**
    (créer ou non, quelle politique), pas l'implémentation de boto3.
    """

    def __init__(self, existants: set[str] | None = None) -> None:
        self.existants = existants or set()
        self.crees: list[str] = []
        self.politiques: dict[str, dict[str, Any]] = {}

    def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - signature boto3
        if Bucket not in self.existants:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadBucket",
            )

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803 - signature boto3
        self.crees.append(Bucket)
        self.existants.add(Bucket)

    def put_bucket_policy(self, Bucket: str, Policy: str) -> None:  # noqa: N803
        self.politiques[Bucket] = json.loads(Policy)


class TestCreationDesCompartiments:
    def test_cree_les_quatre_compartiments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _ClientFactice()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL):
            crees = StorageService.ensure_buckets()

        assert sorted(crees) == [
            "test-banners",
            "test-documents",
            "test-products",
            "test-users",
        ]

    def test_est_idempotente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Appelée à chaque démarrage : la seconde fois ne doit rien faire.
        C'est ce qui permet de la mettre dans la commande de lancement plutôt
        que dans une procédure manuelle — les procédures manuelles se sautent."""
        client = _ClientFactice(
            existants={"test-products", "test-banners", "test-users", "test-documents"}
        )
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL):
            crees = StorageService.ensure_buckets()

        assert crees == []
        assert client.crees == []

    def test_seuls_les_compartiments_publics_recoivent_une_politique(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ClientFactice()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL):
            StorageService.ensure_buckets()

        assert set(client.politiques) == {"test-products", "test-banners", "test-users"}
        # Le compartiment privé n'en reçoit aucune : sans politique, S3 refuse
        # tout ce qui n'est pas signé — exactement le comportement voulu.
        assert "test-documents" not in client.politiques

    def test_la_politique_publique_autorise_la_lecture_et_rien_d_autre(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ClientFactice()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL):
            StorageService.ensure_buckets()

        instruction = client.politiques["test-products"]["Statement"][0]

        assert instruction["Action"] == ["s3:GetObject"]
        # Ni `ListBucket` — qui reviendrait à publier l'inventaire des fichiers —
        # ni `PutObject`, qui laisserait n'importe qui écrire dans le catalogue.
        assert "s3:ListBucket" not in instruction["Action"]
        assert "s3:PutObject" not in instruction["Action"]
        assert instruction["Resource"] == ["arn:aws:s3:::test-products/*"]

    def test_un_compartiment_appartenant_a_un_autre_compte_est_signale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`403` veut dire « il existe et il n'est pas à vous ». Le créer
        échouerait ; le dire au démarrage vaut mieux qu'un envoi refusé plus
        tard, en production."""
        from botocore.exceptions import ClientError

        class _Interdit(_ClientFactice):
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803
                raise ClientError(
                    {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                    "HeadBucket",
                )

        client = _Interdit()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL), pytest.raises(PermissionError):
            StorageService.ensure_buckets()


class TestServiceDeStockage:
    """La façade — c'est par elle que passe tout le reste du projet."""

    def test_enregistre_et_rend_le_chemin_reellement_ecrit(self) -> None:
        from io import BytesIO

        chemin = StorageService.save("products", "menu/test.txt", BytesIO(b"contenu"))

        assert StorageService.exists("products", chemin)

    def test_deux_fichiers_de_meme_nom_ne_s_ecrasent_pas(self) -> None:
        """Avec l'écrasement — le défaut de django-storages — deux clients
        envoyant chacun un `photo.jpg` auraient partagé le même objet : le
        second aurait remplacé le premier, qui aurait vu la photo d'un inconnu
        apparaître sur son profil."""
        from io import BytesIO

        premier = StorageService.save("users", "avatars/photo.jpg", BytesIO(b"un"))
        second = StorageService.save("users", "avatars/photo.jpg", BytesIO(b"deux"))

        assert premier != second

    def test_effacer_deux_fois_n_est_pas_une_erreur(self) -> None:
        from io import BytesIO

        chemin = StorageService.save("products", "menu/ephemere.txt", BytesIO(b"x"))

        StorageService.delete("products", chemin)
        StorageService.delete("products", chemin)

        assert not StorageService.exists("products", chemin)

    def test_effacer_un_chemin_vide_ne_fait_rien(self) -> None:
        # Un champ fichier non renseigné rend une chaîne vide ; la suppression
        # est appelée sans condition sur des chemins de nettoyage.
        StorageService.delete("documents", "")


class TestCommandeDeProvisionnement:
    """`python manage.py ensure_storage_buckets` — appelée au démarrage."""

    def test_cree_les_compartiments_manquants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from io import StringIO

        from django.core.management import call_command

        client = _ClientFactice()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))
        sortie = StringIO()

        with override_settings(**STOCKAGE_REEL):
            call_command("ensure_storage_buckets", stdout=sortie)

        assert "test-products" in sortie.getvalue()
        assert len(client.crees) == 4

    def test_dry_run_ne_touche_a_rien(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from io import StringIO

        from django.core.management import call_command

        client = _ClientFactice()
        monkeypatch.setattr(StorageService, "_client", staticmethod(lambda: client))

        with override_settings(**STOCKAGE_REEL):
            call_command("ensure_storage_buckets", "--dry-run", stdout=StringIO())

        assert client.crees == []

    def test_un_stockage_injoignable_echoue_au_demarrage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plutôt que de laisser le premier envoi de fichier d'un utilisateur
        découvrir le problème en production."""
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        def _injoignable() -> None:
            raise OSError("connexion refusée")

        monkeypatch.setattr(StorageService, "_client", staticmethod(_injoignable))

        with override_settings(**STOCKAGE_REEL), pytest.raises(CommandError):
            call_command("ensure_storage_buckets", stdout=StringIO())


class TestUnePorteUnique:
    """« Ne jamais accéder directement à MinIO depuis le reste de l'application. »

    La règle vaut ce que vaut sa vérification. Elle est donc exécutable : le
    jour où l'on ajoute le chiffrement au repos ou une politique de rétention,
    il n'y a qu'un fichier à ouvrir — à condition que personne n'ait ouvert une
    seconde porte entre-temps.
    """

    INTERDITS: ClassVar[set[str]] = {"boto3", "botocore", "storages"}
    AUTORISE: ClassVar[Path] = BACKEND_ROOT / "common" / "storage.py"

    def _modules(self) -> list[Path]:
        fichiers: list[Path] = []
        for racine in ("apps", "common", "config"):
            fichiers.extend(
                chemin
                for chemin in (BACKEND_ROOT / racine).rglob("*.py")
                if "migrations" not in chemin.parts
            )
        return fichiers

    def test_seul_common_storage_parle_a_s3(self) -> None:
        coupables: list[str] = []

        for chemin in self._modules():
            if chemin == self.AUTORISE:
                continue

            arbre = ast.parse(chemin.read_text(encoding="utf-8"))
            for nœud in ast.walk(arbre):
                if isinstance(nœud, ast.Import):
                    noms = [alias.name.split(".")[0] for alias in nœud.names]
                elif isinstance(nœud, ast.ImportFrom):
                    noms = [(nœud.module or "").split(".")[0]]
                else:
                    continue

                if self.INTERDITS & set(noms):
                    coupables.append(str(chemin.relative_to(BACKEND_ROOT)))

        assert not coupables, (
            "Ces modules parlent au stockage sans passer par common/storage.py : "
            + ", ".join(sorted(set(coupables)))
        )

    def test_les_reglages_ne_nomment_aucun_compartiment_en_dur(self) -> None:
        """Un nom de compartiment écrit dans le code se retrouve identique en
        développement, en recette et en production — trois environnements qui
        écriraient dans le même seau."""
        source = (BACKEND_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        # Les valeurs par défaut sont admises (elles servent au développement),
        # mais chacune doit passer par une variable d'environnement.
        for variable in (
            "S3_BUCKET_PRODUCTS",
            "S3_BUCKET_BANNERS",
            "S3_BUCKET_USERS",
            "S3_BUCKET_DOCUMENTS",
        ):
            assert f'config("{variable}"' in source
