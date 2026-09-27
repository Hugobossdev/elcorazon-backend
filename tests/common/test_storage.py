"""Stockage objet — ce qui est public, ce qui ne l'est jamais, et la seule
porte par laquelle on y accède.

L'enjeu n'est pas la couverture : c'est la **frontière**. Une image de burger
servie sans signature est une bonne chose ; une pièce d'identité servie sans
signature est un incident. Ces tests fixent laquelle est laquelle, et vérifient
qu'aucun site d'appel ne peut inverser les deux par étourderie.

Aucun octet ne part sur le réseau : la suite substitue un stockage en mémoire
(`config/settings/test.py`), et les URL de Cloudinary sont des calculs de
signature, faits hors ligne. Ce qui est vérifié ici est la **décision** — quel
identifiant, quel type de livraison, signé ou non — pas l'implémentation du SDK.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

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
    _identifiants,
    bucket_name,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]

STOCKAGE_REEL = {
    "CLOUDINARY_CLOUD_NAME": "compte-de-test",
    "CLOUDINARY_API_KEY": "123456789",
    "CLOUDINARY_API_SECRET": "secret-de-test",
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
            url = classe().url("dossier/fichier.jpg")

        # Sans signature : une URL d'image doit pouvoir être mise en cache par
        # un navigateur, un CDN, et retrouvée dans un favori le lendemain.
        assert "signature=" not in url
        assert "expires_at=" not in url
        assert "/image/upload/" in url

    def test_les_documents_livreurs_sont_prives(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            url = CourierDocumentStorage().url("couriers/id/cni.pdf")

        # Chaque lecture est signée et expire. C'est ce qui manquait à
        # l'implémentation précédente, où les pièces d'identité vivaient dans
        # un espace public — lisibles indéfiniment par qui connaissait
        # l'adresse.
        assert "signature=" in url
        assert "expires_at=" in url

    def test_une_url_de_document_n_est_jamais_servie_par_le_cdn(self) -> None:
        """La distinction est plus forte qu'une signature : un document ne passe
        pas par l'adresse de livraison publique du tout."""
        with override_settings(**STOCKAGE_REEL):
            url = CourierDocumentStorage().url("couriers/id/cni.pdf")

        assert not url.startswith("https://res.cloudinary.com/")

    def test_les_documents_sont_deposes_en_type_prive(self) -> None:
        """`type=private` : Cloudinary refuse alors tout accès anonyme, quelle
        que soit la connaissance qu'on a de l'identifiant."""
        from common.storage import _type_livraison

        assert _type_livraison("documents") == "private"
        for alias in PUBLIC_BUCKETS:
            assert _type_livraison(alias) == "upload"

    def test_le_stockage_par_defaut_est_prive(self) -> None:
        """Sécurité par défaut : un champ fichier ajouté sans stockage
        explicite atterrit dans l'espace signé, pas en libre accès.

        Vérifié sur la source des réglages communs : la suite substitue un
        stockage en mémoire, donc `settings.STORAGES` ne dit rien de ce qui
        sert en production."""
        source = (BACKEND_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        assert '"default": {"BACKEND": "common.storage.CourierDocumentStorage"}' in source

    def test_aucun_dossier_n_est_a_la_fois_public_et_prive(self) -> None:
        assert not set(PUBLIC_BUCKETS) & set(PRIVATE_BUCKETS)

    def test_les_documents_ne_sont_pas_dans_un_dossier_public(self) -> None:
        # La séparation est portée par le dossier, parce que c'est de lui que
        # `common/storage.py` déduit le type de livraison à l'envoi. Ranger les
        # documents avec les images rendrait la frontière inexprimable.
        assert "documents" in PRIVATE_BUCKETS
        assert "documents" not in PUBLIC_BUCKETS


class TestDossiers:
    def test_les_noms_viennent_des_reglages(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            assert bucket_name("products") == "test-products"
            assert bucket_name("documents") == "test-documents"

    def test_un_alias_inconnu_echoue_bruyamment(self) -> None:
        """Plutôt qu'un dossier inventé, dans lequel des fichiers partiraient
        sans que personne ne les retrouve."""
        with override_settings(**STOCKAGE_REEL), pytest.raises(UnknownBucket):
            bucket_name("inexistant")

    def test_chaque_domaine_a_son_dossier(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            noms = {
                bucket_name(ProductImageStorage.bucket_alias),
                bucket_name(BannerStorage.bucket_alias),
                bucket_name(UserMediaStorage.bucket_alias),
                bucket_name(CourierDocumentStorage.bucket_alias),
            }

        assert len(noms) == 4


class TestConventionDeNommage:
    """Un dépôt et une lecture doivent viser le **même** identifiant.

    C'est la faute la plus coûteuse à diagnostiquer : l'envoi réussit, l'URL est
    bien formée, et le fichier répond 404 parce que les deux côtés ont calculé
    l'identifiant différemment.
    """

    def test_une_image_publique_porte_son_extension_en_format(self) -> None:
        """Sans quoi l'adresse finirait en `brownie.jpg.jpg` : Cloudinary
        réaccole le format à l'identifiant."""
        with override_settings(**STOCKAGE_REEL):
            public_id, extension, ressource = _identifiants("products", "menu/brownie.jpg")

        assert public_id == "test-products/menu/brownie"
        assert extension == "jpg"
        assert ressource == "image"

    def test_un_document_prive_garde_son_extension_dans_l_identifiant(self) -> None:
        """Convention inverse, imposée par Cloudinary : une ressource `raw` est
        rangée sous son nom de fichier complet."""
        with override_settings(**STOCKAGE_REEL):
            public_id, extension, ressource = _identifiants("documents", "couriers/id/cni.pdf")

        assert public_id == "test-documents/couriers/id/cni.pdf"
        assert extension == ""
        assert ressource == "raw"

    def test_un_pdf_dans_un_dossier_public_reste_livre_tel_quel(self) -> None:
        """Un PDF ne se redimensionne pas : le traiter comme une image le ferait
        refuser à l'envoi."""
        with override_settings(**STOCKAGE_REEL):
            _, _, ressource = _identifiants("products", "fiches/carte.pdf")

        assert ressource == "raw"

    def test_le_dossier_prefixe_toujours_l_identifiant(self) -> None:
        """C'est ce préfixe qui sépare les domaines à l'intérieur d'un compte
        Cloudinary unique — l'équivalent de ce que faisait le compartiment."""
        with override_settings(**STOCKAGE_REEL):
            for alias, attendu in [
                ("products", "test-products/"),
                ("banners", "test-banners/"),
                ("users", "test-users/"),
                ("documents", "test-documents/"),
            ]:
                public_id, _, _ = _identifiants(alias, "un/chemin.bin")
                assert public_id.startswith(attendu)


class TestUrlPublique:
    def test_l_adresse_est_absolue_et_directement_exploitable(self) -> None:
        """C'est ce que reçoit l'application Flutter. Une URL relative, ou
        pointant sur un hôte que seul le réseau interne résout, afficherait des
        cadres vides sans que l'API n'ait rien signalé."""
        with override_settings(**STOCKAGE_REEL):
            url = ProductImageStorage().url("menu/burger.jpg")

        assert url == (
            "https://res.cloudinary.com/compte-de-test/image/upload/v1/"
            "test-products/menu/burger.jpg"
        )

    def test_l_adresse_publique_ne_porte_jamais_de_signature(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            url = ProductImageStorage().url("menu/burger.jpg")

        assert "signature=" not in url
        assert "?" not in url

    def test_l_adresse_est_toujours_en_https(self) -> None:
        """Une image servie en clair depuis une page en HTTPS est bloquée par le
        navigateur, et signalée par Android comme trafic non chiffré."""
        with override_settings(**STOCKAGE_REEL):
            for stockage in (ProductImageStorage(), BannerStorage(), UserMediaStorage()):
                assert stockage.url("un/fichier.png").startswith("https://")

    def test_les_espaces_et_accents_sont_encodes(self) -> None:
        with override_settings(**STOCKAGE_REEL):
            url = ProductImageStorage().url("menu/poulet braisé.jpg")

        assert " " not in url


class TestUrlSignee:
    def test_la_signature_expire(self) -> None:
        """Une URL signée qui n'expire pas est un lien permanent déguisé : celui
        qui l'a reçue une fois garde l'accès pour toujours."""
        with override_settings(**STOCKAGE_REEL):
            url = StorageService.presigned_url("documents", "couriers/id/cni.pdf")

        assert "expires_at=" in url

    def test_le_delai_par_defaut_vient_des_reglages(self) -> None:
        import time

        with override_settings(**STOCKAGE_REEL):
            url = StorageService.presigned_url("documents", "couriers/id/cni.pdf")

        expiration = int(url.split("expires_at=")[1].split("&")[0])

        # 900 s ± la seconde d'exécution du test.
        assert 895 <= expiration - int(time.time()) <= 905

    def test_un_delai_explicite_prime(self) -> None:
        import time

        with override_settings(**STOCKAGE_REEL):
            url = StorageService.presigned_url("documents", "couriers/id/cni.pdf", expire=60)

        expiration = int(url.split("expires_at=")[1].split("&")[0])

        assert 55 <= expiration - int(time.time()) <= 65

    def test_deux_documents_differents_ont_des_signatures_differentes(self) -> None:
        """La signature couvre l'identifiant : elle ne peut pas être rejouée sur
        un autre document."""
        with override_settings(**STOCKAGE_REEL):
            une = StorageService.presigned_url("documents", "couriers/id/a.pdf")
            autre = StorageService.presigned_url("documents", "couriers/id/b.pdf")

        assert une.split("signature=")[1] != autre.split("signature=")[1]


class TestProvisionnement:
    """Chez Cloudinary il n'y a rien à provisionner — et c'est un résultat."""

    def test_ensure_buckets_ne_cree_rien(self) -> None:
        """Un dossier n'existe pas en tant qu'objet : c'est un préfixe dans
        l'identifiant d'une ressource, né au premier dépôt."""
        with override_settings(**STOCKAGE_REEL):
            assert StorageService.ensure_buckets() == []

    def test_la_visibilite_ne_depend_d_aucune_etape_prealable(self) -> None:
        """La différence de fond avec S3, et un risque de moins : la visibilité
        n'est plus une politique posée sur un contenant — qu'on pouvait oublier
        de poser — mais une propriété de chaque objet, fixée à l'envoi."""
        from common.storage import _type_livraison

        assert _type_livraison("documents") == "private"


class TestServiceDeStockage:
    """La façade — c'est par elle que passe tout le reste du projet."""

    def test_enregistre_et_rend_le_chemin_reellement_ecrit(self) -> None:
        from io import BytesIO

        chemin = StorageService.save("products", "menu/test.txt", BytesIO(b"contenu"))

        assert StorageService.exists("products", chemin)

    def test_deux_fichiers_de_meme_nom_ne_s_ecrasent_pas(self) -> None:
        """Sans quoi deux clients envoyant chacun un `photo.jpg` auraient
        partagé le même objet : le second aurait remplacé le premier, qui aurait
        vu la photo d'un inconnu apparaître sur son profil."""
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

    def test_la_base_ne_contient_que_le_chemin_relatif(self) -> None:
        """C'est ce qui rend le fournisseur remplaçable. Une colonne qui
        contiendrait `https://res.cloudinary.com/...` figerait Cloudinary dans
        les données — la migration qu'on vient de faire aurait été impossible."""
        from io import BytesIO

        chemin = StorageService.save("products", "menu/relatif.txt", BytesIO(b"x"))

        assert not chemin.startswith("http")
        assert chemin.startswith("menu/")


class TestCommandeDeVerification:
    """`python manage.py ensure_storage_buckets` — appelée au démarrage."""

    def test_recapitule_les_dossiers(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        sortie = StringIO()

        with override_settings(**STOCKAGE_REEL):
            call_command("ensure_storage_buckets", stdout=sortie)

        texte = sortie.getvalue()
        assert "test-products" in texte
        assert "test-documents" in texte

    def test_dry_run_ne_verifie_pas_la_configuration(self) -> None:
        """Utilisable sans compte, pour lire la correspondance alias → dossier."""
        from io import StringIO

        from django.core.management import call_command

        sortie = StringIO()

        with override_settings(
            **{**STOCKAGE_REEL, "CLOUDINARY_API_SECRET": ""},
        ):
            call_command("ensure_storage_buckets", "--dry-run", stdout=sortie)

        assert "test-products" in sortie.getvalue()

    def test_une_configuration_incomplete_echoue_au_demarrage(self) -> None:
        """Plutôt que de laisser le premier envoi de fichier d'un utilisateur
        découvrir le problème en production, sur une erreur d'authentification
        que personne ne rapproche d'une variable oubliée."""
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with (
            override_settings(**{**STOCKAGE_REEL, "CLOUDINARY_API_SECRET": ""}),
            pytest.raises(CommandError, match="CLOUDINARY_API_SECRET"),
        ):
            call_command("ensure_storage_buckets", stdout=StringIO())


class TestUnePorteUnique:
    """« Ne jamais accéder directement au stockage depuis le reste de l'application. »

    La règle vaut ce que vaut sa vérification. Elle est donc exécutable : le
    jour où l'on ajoute le chiffrement au repos ou une politique de rétention,
    il n'y a qu'un fichier à ouvrir — à condition que personne n'ait ouvert une
    seconde porte entre-temps.

    C'est cette règle qui a rendu le passage de MinIO à Cloudinary possible sans
    toucher à un seul modèle, sérialiseur ou point d'API.
    """

    INTERDITS: ClassVar[set[str]] = {"cloudinary", "boto3", "botocore", "storages"}
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

    def test_seul_common_storage_parle_au_fournisseur(self) -> None:
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

    def test_les_reglages_ne_nomment_aucun_dossier_en_dur(self) -> None:
        """Un nom de dossier écrit dans le code se retrouve identique en
        développement, en recette et en production — trois environnements qui
        écriraient au même endroit."""
        source = (BACKEND_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        # Les valeurs par défaut sont admises (elles servent au développement),
        # mais chacune doit passer par une variable d'environnement.
        for variable in (
            "CLOUDINARY_FOLDER_PRODUCTS",
            "CLOUDINARY_FOLDER_BANNERS",
            "CLOUDINARY_FOLDER_USERS",
            "CLOUDINARY_FOLDER_DOCUMENTS",
        ):
            assert f'config("{variable}"' in source

    def test_aucun_identifiant_n_est_ecrit_en_dur(self) -> None:
        """Les trois valeurs du compte ne doivent exister que dans
        l'environnement — jamais dans le dépôt."""
        source = (BACKEND_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        for variable in (
            "CLOUDINARY_CLOUD_NAME",
            "CLOUDINARY_API_KEY",
            "CLOUDINARY_API_SECRET",
        ):
            assert f'config("{variable}", default="")' in source
