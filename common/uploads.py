"""Ce qu'une image publique doit être pour entrer au stockage.

## Pourquoi ici, et pourquoi le serveur

Le back-office refusait déjà une photo de plus de 5 Mo — mais **dans
l'écran** : un appel direct à l'API, ou une autre application demain, déposait
n'importe quel poids dans le compartiment public `products`, servi tel quel à
chaque client sur un réseau mobile. Une règle qui ne vit que dans une
interface n'est pas une règle (contrat commun : le serveur est la source de
vérité).

`ImageField` vérifie déjà, par Pillow, que les octets sont une image. Il ne
dit rien de leur poids, ni du format : un TIFF de 40 Mo ou un BMP passent. Ce
validateur ajoute les deux bornes, pour les photos de plats comme pour celles
des catégories — une seule règle, citée par les deux sérialiseurs.
"""

from __future__ import annotations

from typing import Any

from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

__all__ = ["FORMATS_ACCEPTES", "POIDS_MAX_OCTETS", "valider_image_publique"]

#: Le plafond qu'affichait déjà le back-office.
POIDS_MAX_OCTETS = 5 * 1024 * 1024

#: Formats que tous les téléphones affichent. Le nom est celui de Pillow.
FORMATS_ACCEPTES = frozenset({"JPEG", "PNG", "WEBP"})


def valider_image_publique(fichier: Any) -> Any:
    """Refuse une image trop lourde ou dans un format que les clients
    n'afficheraient pas partout. `None` (retrait de l'image) passe."""
    if fichier is None:
        return fichier

    taille = getattr(fichier, "size", None)
    if taille is not None and taille > POIDS_MAX_OCTETS:
        raise serializers.ValidationError(
            f"L'image pèse {taille / 1024 / 1024:.1f} Mo ; le maximum est "
            f"{POIDS_MAX_OCTETS // 1024 // 1024} Mo."
        )

    if isinstance(fichier, UploadedFile):
        # `ImageField` a déjà ouvert le fichier ; Pillow y a laissé le format.
        # On le relit si besoin, en revenant au début pour l'écriture qui suit.
        format_ = getattr(getattr(fichier, "image", None), "format", None)
        if format_ is None:
            try:
                fichier.seek(0)
                with Image.open(fichier) as image:
                    format_ = image.format
            except (UnidentifiedImageError, OSError) as exc:
                illisible = "Ce fichier n'est pas une image lisible."
                raise serializers.ValidationError(illisible) from exc
            finally:
                fichier.seek(0)
        if format_ not in FORMATS_ACCEPTES:
            raise serializers.ValidationError(
                f"Format {format_ or 'inconnu'} refusé : envoyez une image JPEG, PNG ou WebP."
            )
    return fichier
