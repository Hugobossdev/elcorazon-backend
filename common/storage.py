"""Stockage objet — Cloudinary, en développement comme en production.

**Cloudinary n'est pas une dépendance diffuse, c'est un fournisseur.** Rien
ailleurs dans le projet ne le connaît : le reste du code passe par
[`StorageService`] ou par les fabriques rattachées aux champs des modèles, et un
test d'architecture le vérifie. Changer de fournisseur demain ne demande que de
réécrire ce fichier — c'est précisément ce qui vient d'être fait, en venant de
S3/MinIO, sans qu'aucun modèle, sérialiseur ni migration n'ait bougé.

## Ce qui change en venant de S3, et ce qui ne change pas

Cloudinary n'a pas de compartiments : il a des **dossiers**, qui ne sont qu'un
préfixe dans l'identifiant public d'un objet. `STORAGE_BUCKETS` garde donc son
nom et son rôle — un alias fonctionnel vers un espace de rangement — mais la
valeur désigne un dossier, plus un compartiment. Le vocabulaire du code reste
celui du domaine ; seule l'implémentation sait ce qu'il recouvre.

**Ce que la base de données contient ne change pas.** Un champ fichier stocke
toujours le chemin relatif — `menu/brownie-chocolat.jpg` — et jamais l'URL
complète. C'est ce qui rend un changement de fournisseur possible sans toucher
aux lignes existantes, et c'est aussi pourquoi `upload_to` continue de
fonctionner tel quel.

## Deux visibilités, et c'est la seule chose qui compte

Un catalogue se regarde ; une pièce d'identité ne se regarde pas. La séparation
n'est donc pas un rangement, c'est une frontière :

* **dossiers publics** (`products`, `banners`, `users`) — images de produits,
  bannières, avatars. Livrés en `type=upload`, l'adresse publique de Cloudinary,
  sans signature et avec une mise en cache longue par son CDN. Ce sont des
  fichiers destinés à être vus ;
* **dossier privé** (`documents`) — pièces d'identité, permis, cartes grises,
  preuves de livraison. Déposés en `type=private`, que Cloudinary ne sert
  **jamais** en accès anonyme. Chaque lecture passe par une URL signée que le
  serveur émet, qui expire (`STORAGE_SIGNED_URL_EXPIRE`), et qui n'est produite
  qu'après avoir vérifié qui demande.

## Pourquoi le SDK et non `django-cloudinary-storage`

Le paquet d'intégration lève `ImproperlyConfigured` **à l'import de son module**
quand les identifiants manquent. Or ce fichier est importé par quatre modèles et
par quatre migrations : sans compte Cloudinary, ni `migrate`, ni les tests, ni
`collectstatic` ne démarreraient — alors que la suite de tests utilise justement
un stockage en mémoire pour ne dépendre d'aucun fournisseur. Le SDK, lui,
s'importe sans identifiants et ne les réclame qu'au premier appel réseau.
"""

from __future__ import annotations

import io
import posixpath
import time
from typing import IO, Any, ClassVar
from urllib.parse import urlencode

import cloudinary
import cloudinary.api
import cloudinary.uploader
import cloudinary.utils
import httpx
from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import Storage, storages

__all__ = [
    "PRIVATE_BUCKETS",
    "PUBLIC_BUCKETS",
    "BannerStorage",
    "CourierDocumentStorage",
    "ObjectStorage",
    "ProductImageStorage",
    "StorageService",
    "UnknownBucket",
    "UserMediaStorage",
    "banners",
    "courier_documents",
    "product_images",
    "user_media",
]

#: Dossiers dont le contenu est destiné à être vu de tous. Servis sans
#: signature : une URL de photo de burger n'a aucune raison d'expirer.
PUBLIC_BUCKETS = ("products", "banners", "users")

#: Dossiers dont chaque lecture est autorisée une par une. Jamais servis en
#: direct, jamais mis en cache par un intermédiaire.
PRIVATE_BUCKETS = ("documents",)

#: Extensions que Cloudinary sait traiter comme des **images** — donc
#: redimensionner, recadrer, convertir en WebP à la volée. Les autres partent en
#: `raw`, c'est-à-dire livrées telles qu'elles ont été déposées.
#:
#: La liste est explicite plutôt que devinée : un PDF déposé dans un dossier
#: public doit rester lisible, pas être refusé par un traitement d'image.
IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic", "tif", "tiff"}
)


class UnknownBucket(RuntimeError):
    """Alias de dossier absent de `STORAGE_BUCKETS`."""

    def __init__(self, alias: str) -> None:
        super().__init__(
            f"Aucun dossier configuré pour l'alias {alias!r}. "
            f"Complétez STORAGE_BUCKETS dans les réglages."
        )


def bucket_name(alias: str) -> str:
    """Nom réel du dossier derrière un alias fonctionnel.

    Le code désigne « les images de produits » ; le déploiement décide que cela
    s'appelle `elcorazon-products`, `elcorazon-prod-products` ou autre chose.
    Aucun nom de dossier n'est écrit en dur.
    """
    try:
        return str(settings.STORAGE_BUCKETS[alias])
    except KeyError as exc:
        raise UnknownBucket(alias) from exc


def _configure() -> None:
    """Arme le SDK depuis les réglages, juste avant de s'en servir.

    Appelé à chaque accès plutôt qu'une fois pour toutes à l'import : c'est ce
    qui permet aux tests de substituer un compte factice avec `override_settings`
    sans réimporter le module, et c'est bon marché — `cloudinary.config()` ne
    fait qu'écrire des attributs sur un singleton.
    """
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )


def _identifiants(alias: str, chemin: str) -> tuple[str, str, str]:
    """Traduit un (alias, chemin) en triplet Cloudinary.

    Rend `(public_id, format, resource_type)`. C'est **le seul endroit** où la
    convention de nommage est décidée, pour que le dépôt, la lecture, la
    suppression et la signature ne puissent pas diverger — une URL signée sur un
    identifiant que l'envoi n'a pas utilisé produirait un 404 que rien
    n'expliquerait.

    Deux conventions, imposées par Cloudinary lui-même :

    * **image** — l'extension n'appartient pas à l'identifiant, elle est le
      `format`. La conserver donnerait des adresses en `brownie.jpg.jpg` ;
    * **raw** — l'extension fait partie de l'identifiant, et le format reste
      vide. C'est ainsi que Cloudinary range un fichier livré tel quel.
    """
    complet = posixpath.join(bucket_name(alias), chemin.lstrip("/"))
    racine, _, extension = complet.rpartition(".")

    est_image = alias in PUBLIC_BUCKETS and extension.lower() in IMAGE_EXTENSIONS
    if est_image and racine:
        return racine, extension, "image"
    return complet, "", "raw"


def _type_livraison(alias: str) -> str:
    """`upload` (public, servi par le CDN) ou `private` (jamais servi en direct)."""
    return "upload" if alias in PUBLIC_BUCKETS else "private"


# ------------------------------------------------------------------ stockages


class ObjectStorage(Storage):
    """Base des stockages du projet — un dossier, une visibilité.

    Les sous-classes ne déclarent que ces deux choses ; tout le reste (compte,
    identifiants, TLS) vient des réglages, communs à tous.
    """

    #: Alias fonctionnel, résolu en nom de dossier à l'usage.
    bucket_alias: ClassVar[str] = "products"

    #: Public = servi sans signature. Privé = URL signée, expirante, émise
    #: après contrôle d'accès.
    is_public: ClassVar[bool] = False

    # -- écriture -------------------------------------------------------

    def _save(self, name: str, content: IO[bytes]) -> str:
        """Dépose le fichier et rend le **chemin relatif**, pas l'identifiant.

        C'est ce chemin que Django écrit dans la colonne. Le garder relatif est
        ce qui rend le fournisseur remplaçable : une colonne qui contiendrait
        `https://res.cloudinary.com/...` figerait Cloudinary dans les données.
        """
        public_id, extension, type_ressource = _identifiants(self.bucket_alias, name)
        options: dict[str, Any] = {
            "public_id": public_id,
            "resource_type": type_ressource,
            "type": _type_livraison(self.bucket_alias),
            # Deux fichiers de même nom ne s'écrasent pas. Django a déjà écarté
            # les collisions via `get_available_name`, qui s'appuie sur
            # `exists()` ; ce garde-fou couvre la course entre les deux.
            "overwrite": False,
            # Purge le CDN : sans cela, un fichier remplacé continue d'être
            # servi depuis les caches de bordure pendant des heures.
            "invalidate": True,
        }
        if extension:
            options["format"] = extension

        if hasattr(content, "seek"):
            content.seek(0)

        _configure()
        cloudinary.uploader.upload(content, **options)
        return name

    # -- lecture --------------------------------------------------------

    def _open(self, name: str, mode: str = "rb") -> File:
        """Relit un fichier déposé.

        Passe par l'URL du stockage, donc signée pour un dossier privé : la
        lecture d'une pièce d'identité emprunte le même chemin contrôlé que
        celle d'un client, au lieu d'une porte dérobée réservée au serveur.
        """
        # `follow_redirects` : contrairement à `requests`, httpx ne suit rien par
        # défaut, et le point de téléchargement des objets privés renvoie une
        # redirection vers l'objet lui-même. Sans ce drapeau, la lecture d'un
        # document rendrait un corps vide avec un code 302 — pas une erreur.
        reponse = httpx.get(self.url(name), timeout=30, follow_redirects=True)
        reponse.raise_for_status()
        return File(io.BytesIO(reponse.content), name=name)

    def url(self, name: str, parameters: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """Adresse de lecture.

        Publique et stable pour un dossier public, signée et expirante pour un
        dossier privé — c'est `is_public` qui tranche, et il est décidé par la
        classe, pas par l'appelant. Aucun site d'appel ne peut donc rendre
        publique une pièce d'identité en oubliant un argument.
        """
        if not self.is_public:
            return StorageService.presigned_url(self.bucket_alias, name)

        public_id, extension, type_ressource = _identifiants(self.bucket_alias, name)
        _configure()
        adresse, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=type_ressource,
            type="upload",
            format=extension or None,
            secure=True,
        )
        return str(adresse)

    # -- interrogation --------------------------------------------------

    def exists(self, name: str) -> bool:
        """Le fichier est-il déjà là ?

        Django s'en sert avant chaque dépôt pour ne pas écraser : c'est ce qui
        garantit que deux clients envoyant chacun un `photo.jpg` comme avatar
        ne partagent pas le même objet.
        """
        public_id, _, type_ressource = _identifiants(self.bucket_alias, name)
        _configure()
        try:
            cloudinary.api.resource(
                public_id,
                resource_type=type_ressource,
                type=_type_livraison(self.bucket_alias),
            )
        except cloudinary.api.NotFound:
            return False
        return True

    def size(self, name: str) -> int:
        public_id, _, type_ressource = _identifiants(self.bucket_alias, name)
        _configure()
        details = cloudinary.api.resource(
            public_id,
            resource_type=type_ressource,
            type=_type_livraison(self.bucket_alias),
        )
        return int(details.get("bytes", 0))

    def listdir(self, path: str) -> tuple[list[str], list[str]]:
        """Non implémenté, et volontairement.

        Rien dans le projet ne parcourt un dossier, et un stockage qui sait
        s'inventorier invite à le faire — or lister le dossier privé reviendrait
        à énumérer les pièces d'identité des livreurs.
        """
        raise NotImplementedError(
            "Le stockage objet ne s'inventorie pas : interroger les modèles, "
            "qui savent quels fichiers existent."
        )

    # -- suppression ----------------------------------------------------

    def delete(self, name: str) -> None:
        """Efface. Silencieux si le fichier n'existe plus : effacer deux fois
        n'est pas une erreur."""
        if not name:
            return
        public_id, _, type_ressource = _identifiants(self.bucket_alias, name)
        _configure()
        cloudinary.uploader.destroy(
            public_id,
            resource_type=type_ressource,
            type=_type_livraison(self.bucket_alias),
            invalidate=True,
        )


class ProductImageStorage(ObjectStorage):
    """Images du catalogue — articles, catégories."""

    bucket_alias = "products"
    is_public = True


class BannerStorage(ObjectStorage):
    """Bannières et visuels de campagne."""

    bucket_alias = "banners"
    is_public = True


class UserMediaStorage(ObjectStorage):
    """Avatars et couvertures d'établissement.

    Public : un avatar s'affiche dans une liste de commandes, aux côtés de
    dizaines d'autres. Le signer coûterait une signature par vignette et
    interdirait toute mise en cache.
    """

    bucket_alias = "users"
    is_public = True


class CourierDocumentStorage(ObjectStorage):
    """Pièces d'identité, permis, cartes grises, preuves de livraison.

    Privé, et c'est la seule chose à retenir : Cloudinary ne sert aucun objet
    `type=private` en accès anonyme, et chaque lecture exige une URL signée
    émise par le serveur pour une durée bornée.
    """

    bucket_alias = "documents"
    is_public = False


# ------------------------------------------------- fabriques pour les modèles
#
# Les champs `FileField` reçoivent une **fonction**, pas une instance. Django
# sérialise alors son chemin d'import dans la migration, et non l'état d'un
# objet configuré : les identifiants et le compte du jour ne se figent pas dans
# un fichier de migration versionné.
#
# Le passage par le registre `STORAGES` permet en outre aux tests de substituer
# un stockage en mémoire sans toucher aux modèles — voir `config/settings/test.py`.


def product_images() -> Storage:
    return storages["products"]


def banners() -> Storage:
    return storages["banners"]


def user_media() -> Storage:
    return storages["users"]


def courier_documents() -> Storage:
    return storages["documents"]


# -------------------------------------------------------------------- service


class StorageService:
    """La porte d'entrée du stockage objet.

    Tout ce que le projet fait d'un fichier passe par ici : déposer, effacer,
    donner une adresse de lecture, préparer un envoi direct. Le reste du code
    n'a ni à savoir quel fournisseur sert les octets, ni comment une URL est
    signée.
    """

    @staticmethod
    def storage_for(alias: str) -> Storage:
        """Stockage d'un alias fonctionnel (`products`, `documents`, …)."""
        return storages[alias]

    # -- écriture -------------------------------------------------------

    @staticmethod
    def save(alias: str, path: str, content: IO[bytes]) -> str:
        """Dépose un fichier et rend le chemin **réellement** écrit.

        Le chemin rendu peut différer de celui demandé : deux fichiers de même
        nom ne s'écrasent pas, le stockage suffixe le second. C'est ce chemin-là
        qu'il faut conserver, jamais celui qu'on a proposé.
        """
        return str(storages[alias].save(path, content))

    @staticmethod
    def delete(alias: str, path: str) -> None:
        """Efface un fichier. Silencieux s'il n'existe plus : effacer deux fois
        n'est pas une erreur, et une suppression rejouée ne doit pas faire
        échouer la transaction qui l'entoure."""
        if not path:
            return
        storages[alias].delete(path)

    @staticmethod
    def exists(alias: str, path: str) -> bool:
        return bool(storages[alias].exists(path))

    # -- lecture --------------------------------------------------------

    @staticmethod
    def url(alias: str, path: str) -> str:
        """Adresse de lecture, selon la visibilité du dossier.

        Publique et durable pour une image de catalogue, signée et expirante
        pour un document. L'appelant n'a pas à choisir — c'est précisément ce
        qui évite qu'un oubli rende un justificatif public.
        """
        return str(storages[alias].url(path))

    @staticmethod
    def presigned_url(alias: str, path: str, *, expire: int | None = None) -> str:
        """URL de lecture signée, valable [expire] secondes.

        À n'appeler qu'après avoir vérifié que le demandeur a le droit de lire
        ce document : la signature ne prouve rien sur lui, elle prouve
        seulement que le serveur a accepté.

        Passe par le point de téléchargement de Cloudinary et non par une URL de
        livraison signée : cette dernière porte bien une signature, mais **elle
        n'expire pas**. Un lien transmis une fois resterait valable pour
        toujours, ce qui est exactement ce qu'on refuse à une pièce d'identité.
        """
        delai = expire if expire is not None else settings.STORAGE_SIGNED_URL_EXPIRE
        public_id, extension, type_ressource = _identifiants(alias, path)
        _configure()
        return str(
            cloudinary.utils.private_download_url(
                public_id,
                extension,
                resource_type=type_ressource,
                type=_type_livraison(alias),
                expires_at=int(time.time()) + delai,
            )
        )

    @staticmethod
    def presigned_upload_url(
        alias: str,
        path: str,
        *,
        content_type: str = "application/octet-stream",
        expire: int | None = None,
    ) -> str:
        """URL d'envoi signée — le client dépose son fichier sans passer par l'API.

        Utile pour les gros fichiers, qu'il serait absurde de faire transiter
        par le serveur applicatif. La signature couvre **l'identifiant visé** :
        elle ne donne pas le droit d'écrire ailleurs dans le dossier.

        Contrairement à S3, où l'on signait un `PUT`, Cloudinary attend un
        `POST` multipart sur son point d'envoi, les paramètres signés étant
        portés par la chaîne de requête. `content_type` n'y a pas d'équivalent :
        le type est déduit du fichier reçu, et la nature de la ressource est
        déjà fixée par `resource_type`.
        """
        del content_type  # sans objet chez ce fournisseur — voir la docstring
        delai = expire if expire is not None else settings.STORAGE_SIGNED_URL_EXPIRE
        public_id, extension, type_ressource = _identifiants(alias, path)
        _configure()

        horodatage = int(time.time())
        parametres: dict[str, Any] = {
            "public_id": public_id,
            "type": _type_livraison(alias),
            "timestamp": horodatage,
            "expires_at": horodatage + delai,
        }
        if extension:
            parametres["format"] = extension

        parametres["signature"] = cloudinary.utils.api_sign_request(
            parametres, str(cloudinary.config().api_secret)
        )
        parametres["api_key"] = cloudinary.config().api_key

        base = cloudinary.utils.cloudinary_api_url("upload", resource_type=type_ressource)
        return f"{base}?{urlencode(parametres)}"

    # -- provisionnement ------------------------------------------------

    @staticmethod
    def ensure_buckets() -> list[str]:
        """Sans objet chez Cloudinary — rend une liste vide, et c'est un résultat.

        Un dossier Cloudinary n'existe pas en tant qu'objet : c'est un préfixe
        dans l'identifiant d'une ressource, créé implicitement au premier dépôt.
        Il n'y a donc rien à provisionner, et rien ne peut manquer.

        La visibilité, elle, ne dépend pas d'une politique posée sur un
        contenant comme chez S3 : elle est portée **par chaque objet**, via son
        `type` de livraison, décidé à l'envoi par la classe de stockage. Un
        fichier du dossier privé est déposé en `type=private` quoi qu'il arrive,
        sans qu'aucune politique n'ait à être posée au préalable — une étape de
        moins, et une étape de moins qu'on peut oublier.

        La méthode et sa commande sont conservées plutôt que supprimées : elles
        sont appelées au démarrage dans `docker-compose.yml` et
        `docker-compose.prod.yml`, et les retirer ferait échouer deux séquences
        de démarrage pour un gain nul.
        """
        return []
