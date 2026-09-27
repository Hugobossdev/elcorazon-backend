"""Géocodage inverse — les composants d'une position, servis par le serveur.

## Pourquoi le serveur, et pas l'application

Trois raisons, dans l'ordre où elles comptent.

**La clé.** Une clé Google embarquée dans un binaire est publique : on l'extrait
d'un APK en quelques minutes, et la restreindre par empreinte d'application ne
protège pas un build web, où elle est en clair dans le paquet JavaScript. Ici
elle reste dans l'environnement du serveur, restreinte par adresse IP.

**Le cache.** Le back-office reverse-géocode le même point chaque fois qu'un
administrateur déplace un marqueur d'un pixel puis revient. Côté serveur, une
réponse se garde ; côté application, chacune paie ses propres appels et personne
ne voit la facture monter.

**Le contrat.** L'implémentation Flutter existante ne rendait qu'une chaîne —
`formatted_address` — et l'écran devinait la ville en cherchant son nom dedans.
Ce qu'il faut, ce sont les composants **classés par Google** : pays, région,
ville, quartier, code postal. Les extraire une fois, ici, évite que trois
applications inventent trois extractions.

## Ce que ce module ne fait pas

Il n'écrit rien et ne décide rien. Une réponse de Google est une **proposition** :
c'est l'administrateur qui valide, et c'est le back-office qui enregistre. Rien
n'écrase une donnée saisie à la main — la chaîne est explicitement
« Google → proposition → validation → enregistrement ».

Il ne remplace pas non plus l'autocomplétion de lieux de l'application cliente,
qui a ses propres jetons de session et sa propre facturation groupée.

## Sans clé configurée

La route répond 503 avec une phrase qui dit quoi faire, plutôt que de tomber en
500 ou de rendre un objet vide qui ferait croire à une position sans pays.
C'est la même règle que pour les autres intégrations du projet : une
dépendance externe absente est une panne de configuration, pas une erreur du
client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache

__all__ = ["GeocodingUnavailable", "ReverseGeocodeResult", "reverse_geocode"]

#: Correspondance entre les types de composants Google et nos champs.
#:
#: L'ordre à l'intérieur de chaque liste est celui de préférence : Google
#: classe une agglomération tantôt en `locality`, tantôt en
#: `administrative_area_level_2` selon les pays, et les villes africaines
#: tombent souvent dans la seconde. Prendre la première correspondance trouvée
#: dans cet ordre donne la réponse la plus fine disponible.
COMPOSANTS: dict[str, tuple[str, ...]] = {
    "country": ("country",),
    "region": ("administrative_area_level_1",),
    "city": ("locality", "administrative_area_level_2", "administrative_area_level_1"),
    "district": ("sublocality_level_1", "sublocality", "neighborhood"),
    "postal_code": ("postal_code",),
    "street": ("route",),
    "street_number": ("street_number",),
}

#: Durée de conservation d'une réponse, en secondes.
#:
#: Une semaine : un carrefour ne change pas de quartier, et le back-office
#: repasse sur les mêmes points à chaque ajustement de marqueur. La clé de cache
#: arrondit à cinq décimales — environ un mètre — pour que deux relevés du même
#: endroit se partagent la même entrée.
CACHE_SECONDS = 7 * 24 * 3600


class GeocodingUnavailable(RuntimeError):
    """Le service de géocodage n'est pas joignable, ou pas configuré.

    Distincte d'une absence de résultat : « je ne sais pas répondre » et « il n'y
    a rien à cet endroit » appellent deux messages différents, et les confondre
    ferait chercher une panne là où il n'y a que l'océan.
    """


@dataclass(frozen=True, slots=True)
class ReverseGeocodeResult:
    """Ce que Google sait d'une position, rangé.

    Tous les membres sont facultatifs sauf les coordonnées : au large, Google ne
    rend ni pays ni ville, et inventer une valeur serait pire que de n'en rendre
    aucune.
    """

    latitude: float
    longitude: float
    formatted_address: str | None = None
    place_id: str | None = None
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    district: str | None = None
    postal_code: str | None = None
    street: str | None = None
    street_number: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def reverse_geocode(
    *, latitude: float, longitude: float, language: str = "fr"
) -> ReverseGeocodeResult:
    """Composants d'adresse d'une position, mis en cache.

    Rend un résultat aux membres nuls quand Google ne connaît rien à cet
    endroit — au large, en plein désert. Ce n'est **pas** une erreur : la
    position reste valide, et l'administrateur peut vouloir y poser un point de
    retrait qu'aucun service d'adressage ne nomme.

    Lève `GeocodingUnavailable` quand la clé manque ou que Google ne répond
    pas : là, il y a bien quelque chose à corriger.
    """
    cle_api = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    if not cle_api:
        raise GeocodingUnavailable(
            "Le géocodage n'est pas configuré sur ce serveur : renseignez GOOGLE_MAPS_API_KEY."
        )

    cle_cache = f"geocode:{language}:{latitude:.5f},{longitude:.5f}"
    if (memorise := cache.get(cle_cache)) is not None:
        return ReverseGeocodeResult(**memorise)

    charge = _appeler_google(latitude, longitude, language, cle_api)
    resultat = _extraire(latitude, longitude, charge)

    cache.set(cle_cache, resultat.as_dict(), CACHE_SECONDS)
    return resultat


def _appeler_google(latitude: float, longitude: float, language: str, cle: str) -> dict[str, Any]:
    parametres = urllib.parse.urlencode(
        {"latlng": f"{latitude},{longitude}", "language": language, "key": cle}
    )
    url = f"https://maps.googleapis.com/maps/api/geocode/json?{parametres}"

    try:
        # Borné : sans délai, un incident réseau chez le fournisseur bloquerait
        # un travailleur du back-office indéfiniment, sur un écran qui n'affiche
        # qu'un indicateur d'attente.
        with urllib.request.urlopen(url, timeout=8) as reponse:  # noqa: S310 - hôte constant
            charge: dict[str, Any] = json.loads(reponse.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as erreur:
        raise GeocodingUnavailable(
            "Le service de géocodage n'a pas répondu. Réessayez, ou saisissez "
            "les coordonnées à la main."
        ) from erreur

    statut = charge.get("status")
    if statut == "ZERO_RESULTS":
        return {"results": []}
    if statut != "OK":
        # `REQUEST_DENIED`, `OVER_QUERY_LIMIT` : ce sont des pannes de
        # configuration ou de quota, pas des réponses à la question posée. Le
        # message de Google est repris tel quel — il nomme la cause, et c'est
        # l'exploitant qui le lira.
        raise GeocodingUnavailable(
            f"Géocodage refusé par le fournisseur ({statut}). "
            f"{charge.get('error_message', '')}".strip()
        )

    return charge


def _extraire(latitude: float, longitude: float, charge: dict[str, Any]) -> ReverseGeocodeResult:
    """Range les composants classés par Google dans nos champs.

    On lit `address_components`, et **jamais** le texte de `formatted_address` :
    l'implémentation précédente y cherchait le nom de la ville par sous-chaîne,
    ce qui trouvait « Lomé » dans « Rue de Lomé, Cotonou ».
    """
    resultats = charge.get("results") or []
    if not resultats:
        return ReverseGeocodeResult(latitude=latitude, longitude=longitude)

    principal = resultats[0]
    composants = principal.get("address_components") or []

    valeurs: dict[str, str | None] = dict.fromkeys(COMPOSANTS)
    code_pays: str | None = None

    for champ, types_attendus in COMPOSANTS.items():
        for type_attendu in types_attendus:
            trouve = next((c for c in composants if type_attendu in (c.get("types") or [])), None)
            if trouve is not None:
                valeurs[champ] = trouve.get("long_name")
                if champ == "country":
                    code_pays = trouve.get("short_name")
                break

    return ReverseGeocodeResult(
        latitude=latitude,
        longitude=longitude,
        formatted_address=principal.get("formatted_address"),
        place_id=principal.get("place_id"),
        country_code=code_pays,
        **valeurs,
    )
