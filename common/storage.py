"""Stockage objet — S3, servi par MinIO en développement et en production.

**MinIO n'est pas une dépendance du code, c'est un serveur S3.** Rien ici ne
connaît MinIO : le protocole est celui d'Amazon S3, parlé par `boto3` à travers
`django-storages`. Passer sur AWS S3 le jour venu ne demande que de changer
`S3_ENDPOINT_URL` et les identifiants — aucune ligne de Python.

## Deux visibilités, et c'est la seule chose qui compte

Un catalogue se regarde ; une pièce d'identité ne se regarde pas. La séparation
n'est donc pas un rangement, c'est une frontière :

* **compartiments publics** (`products`, `banners`, `users`) — images de
  produits, bannières, avatars. Servis directement, sans signature, avec une
  mise en cache longue. Ce sont des fichiers destinés à être vus ;
* **compartiment privé** (`documents`) — pièces d'identité, permis, cartes
  grises, preuves de livraison. **Jamais servis en direct.** Chaque lecture
  passe par une URL signée que le serveur émet, qui expire, et qui n'est
  produite qu'après avoir vérifié qui demande.

L'implémentation précédente rangeait tout dans un compartiment unique. Les
documents des livreurs y étaient signés — ce qui était juste — mais l'image
d'un burger l'était aussi : chaque URL expirait au bout de quinze minutes, donc
aucun cache ni CDN ne pouvait s'y accrocher, et une page de catalogue mise en
favori affichait des cadres vides le lendemain.

## Une seule porte

Le reste du projet n'importe ni `boto3` ni `storages` : il passe par
[`StorageService`] ou par les fabriques de stockage rattachées aux champs des
modèles. Un test d'architecture le vérifie. La raison est concrète : le jour où
l'on ajoute le chiffrement au repos, une politique de rétention ou un second
fournisseur, il n'y a qu'un fichier à ouvrir.
"""

from __future__ import annotations

import json
from typing import IO, Any, ClassVar
from urllib.parse import quote

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.files.storage import Storage, storages
from storages.backends.s3 import S3Storage

#: Client `boto3` bas niveau. `boto3` ne publie pas d'annotations et les stubs
#: (`boto3-stubs`) pèsent plus lourd que le service qu'ils typeraient : l'alias
#: nomme l'intention là où le vérificateur ne peut rien garantir.
type S3Client = Any

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

#: Compartiments dont le contenu est destiné à être vu de tous. Servis sans
#: signature : une URL de photo de burger n'a aucune raison d'expirer.
PUBLIC_BUCKETS = ("products", "banners", "users")

#: Compartiments dont chaque lecture est autorisée une par une. Jamais servis
#: en direct, jamais mis en cache par un intermédiaire.
PRIVATE_BUCKETS = ("documents",)


class UnknownBucket(RuntimeError):
    """Alias de compartiment absent de `STORAGE_BUCKETS`."""

    def __init__(self, alias: str) -> None:
        super().__init__(
            f"Aucun compartiment configuré pour l'alias {alias!r}. "
            f"Complétez STORAGE_BUCKETS dans les réglages."
        )


def bucket_name(alias: str) -> str:
    """Nom réel du compartiment derrière un alias fonctionnel.

    Le code désigne « les images de produits » ; le déploiement décide que cela
    s'appelle `elcorazon-products`, `elcorazon-prod-products` ou autre chose.
    Aucun nom de compartiment n'est écrit dans le code.
    """
    try:
        return str(settings.STORAGE_BUCKETS[alias])
    except KeyError as exc:
        raise UnknownBucket(alias) from exc


# ------------------------------------------------------------------ stockages


class ObjectStorage(S3Storage):
    """Base des stockages du projet — un compartiment, une visibilité.

    Les sous-classes ne déclarent que ces deux choses ; tout le reste (point
    d'accès, identifiants, région, TLS) vient des réglages, communs à tous.
    """

    #: Alias fonctionnel, résolu en nom de compartiment à l'instanciation.
    bucket_alias: ClassVar[str] = "products"

    #: Public = servi sans signature. Privé = URL signée, expirante, émise
    #: après contrôle d'accès.
    is_public: ClassVar[bool] = False

    def __init__(self, **overrides: Any) -> None:
        options: dict[str, Any] = {
            "bucket_name": bucket_name(self.bucket_alias),
            "endpoint_url": settings.STORAGE_ENDPOINT_URL or None,
            "region_name": settings.STORAGE_REGION,
            "use_ssl": settings.STORAGE_USE_SSL,
            "access_key": settings.STORAGE_ACCESS_KEY or None,
            "secret_key": settings.STORAGE_SECRET_KEY or None,
            # MinIO ne sait pas résoudre un compartiment en sous-domaine
            # (`bucket.host`) : il faut le chemin (`host/bucket`). AWS accepte
            # les deux, donc ce réglage reste juste des deux côtés — et
            # paramétrable pour le jour où un CDN exige l'autre forme.
            "addressing_style": settings.STORAGE_ADDRESSING_STYLE,
            "querystring_auth": not self.is_public,
            "querystring_expire": settings.STORAGE_SIGNED_URL_EXPIRE,
            # Deux fichiers de même nom ne s'écrasent pas. Avec l'écrasement
            # (le défaut de django-storages), deux clients envoyant chacun un
            # `photo.jpg` comme avatar auraient partagé le même objet : le
            # second aurait remplacé le premier, qui aurait vu apparaître la
            # photo d'un inconnu sur son profil.
            "file_overwrite": False,
            # Aucune ACL par objet : la visibilité est portée par la politique
            # du compartiment, posée une fois (voir `StorageService.ensure_buckets`).
            # Les ACL par objet sont refusées par défaut sur les compartiments
            # AWS récents, et les poser ici ferait échouer chaque envoi.
            "default_acl": None,
            "signature_version": "s3v4",
        }
        options.update(overrides)
        super().__init__(**options)

    # -- URL ------------------------------------------------------------

    def url(self, name: str, parameters: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """URL de lecture.

        Publique et stable pour un compartiment public, signée et expirante
        pour un compartiment privé — c'est `querystring_auth` qui tranche, et
        il est décidé par la classe, pas par l'appelant. Aucun site d'appel ne
        peut donc rendre publique une pièce d'identité en oubliant un argument.
        """
        if self.is_public and settings.STORAGE_PUBLIC_BASE_URL:
            base = settings.STORAGE_PUBLIC_BASE_URL.rstrip("/")
            return f"{base}/{self.bucket_name}/{quote(name)}"
        return str(super().url(name, parameters=parameters, **kwargs))


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
    """Pièces justificatives et preuves de livraison — **privé**.

    Une pièce d'identité n'est lisible que par le personnel habilité, et une
    fois : l'URL est signée, elle expire, et elle n'est émise qu'après
    vérification du droit d'en connaître. Ces documents ont vécu dans un
    compartiment public dans l'implémentation précédente — une pièce d'identité
    y était lisible indéfiniment par qui connaissait l'adresse.
    """

    bucket_alias = "documents"
    is_public = False


# ------------------------------------------------- fabriques pour les modèles
#
# Les champs `FileField` reçoivent une **fonction**, pas une instance. Django
# sérialise alors son chemin d'import dans la migration, et non l'état d'un
# objet configuré : les identifiants et le point d'accès du jour ne se figent
# pas dans un fichier de migration versionné.
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
        """Adresse de lecture, selon la visibilité du compartiment.

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
        """
        delai = expire if expire is not None else settings.STORAGE_SIGNED_URL_EXPIRE
        return str(
            StorageService._client().generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name(alias), "Key": path},
                ExpiresIn=delai,
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
        par le serveur applicatif. L'URL vise **un chemin précis** : elle ne
        donne pas le droit d'écrire ailleurs dans le compartiment.
        """
        delai = expire if expire is not None else settings.STORAGE_SIGNED_URL_EXPIRE
        return str(
            StorageService._client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": bucket_name(alias),
                    "Key": path,
                    "ContentType": content_type,
                },
                ExpiresIn=delai,
            )
        )

    # -- provisionnement ------------------------------------------------

    @staticmethod
    def ensure_buckets() -> list[str]:
        """Crée les compartiments manquants et pose leur politique de lecture.

        Rend la liste de ceux qui ont été créés. Idempotent : appelée au
        démarrage d'un environnement de développement, elle ne fait rien la
        deuxième fois.

        Les compartiments publics reçoivent une politique de **lecture seule
        anonyme** — lire, jamais lister ni écrire. Le compartiment privé n'en
        reçoit aucune : sans politique, S3 refuse tout ce qui n'est pas signé,
        ce qui est exactement le comportement voulu.
        """
        client = StorageService._client()
        crees: list[str] = []

        for alias in (*PUBLIC_BUCKETS, *PRIVATE_BUCKETS):
            nom = bucket_name(alias)
            if StorageService._create_if_absent(client, nom):
                crees.append(nom)
            if alias in PUBLIC_BUCKETS:
                StorageService._apply_public_read_policy(client, nom)

        return crees

    @staticmethod
    def _create_if_absent(client: S3Client, nom: str) -> bool:
        try:
            client.head_bucket(Bucket=nom)
            return False
        except ClientError as exc:
            statut = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if statut not in (403, 404):
                raise

        # `403` signifie « il existe et il n'est pas à vous » : le créer
        # échouerait, et le signaler tout de suite vaut mieux qu'un envoi
        # refusé plus tard, à l'exécution.
        if statut == 403:
            raise PermissionError(
                f"Le compartiment {nom!r} existe mais n'est pas accessible avec ces identifiants."
            )

        client.create_bucket(Bucket=nom)
        return True

    @staticmethod
    def _apply_public_read_policy(client: S3Client, nom: str) -> None:
        politique = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "LectureAnonyme",
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    # Lecture d'un objet **désigné** : ni `ListBucket`, ni
                    # `PutObject`. Sans cette restriction, un compartiment
                    # « public » laisserait inventorier son contenu, ce qui
                    # revient à publier la liste des fichiers.
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{nom}/*"],
                }
            ],
        }
        client.put_bucket_policy(Bucket=nom, Policy=json.dumps(politique))

    @staticmethod
    def _client() -> S3Client:
        """Client S3 bas niveau — le **seul** du projet.

        `boto3` n'est importé nulle part ailleurs : un test d'architecture le
        vérifie. Ce qui compte n'est pas l'esthétique mais le point de reprise :
        chiffrement au repos, rétention, second fournisseur se règlent ici.
        """
        return boto3.client(
            "s3",
            endpoint_url=settings.STORAGE_ENDPOINT_URL or None,
            region_name=settings.STORAGE_REGION,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY or None,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY or None,
            use_ssl=settings.STORAGE_USE_SSL,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": settings.STORAGE_ADDRESSING_STYLE},
            ),
        )
