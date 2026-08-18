"""Règles de couches et surface publique — ADR-003, ADR-005, ADR-007.

Trois promesses du projet ne tenaient jusqu'ici qu'à la relecture :

* les couches ne remontent pas — un modèle n'importe pas une vue ;
* tout modèle métier porte une clé UUID (« un test d'architecture le
  signale », dit `common/models.py`) ;
* la liste des routes ouvertes sans jeton est **auditable**, ce qui suppose
  qu'elle soit fixe et déclarée quelque part.

Le dernier test est le plus utile des trois. Ouvrir une route par mégarde ne
casse rien et ne se voit pas : l'application fonctionne, simplement elle
répond à tout le monde. Ici, la liste est écrite, et toute route publique qui
n'y figure pas fait échouer la construction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver
from rest_framework.permissions import AllowAny

from common.models import UUIDModel
from tests.architecture.graph import iter_app_modules

pytestmark = pytest.mark.architecture

#: Routes joignables **sans jeton**, et pourquoi.
#:
#: Un visiteur doit pouvoir savoir si on le livre et voir un prix avant de
#: créer un compte ; un prestataire de paiement n'a pas de compte du tout. Tout
#: le reste exige une authentification.
#:
#: Cette liste est le point d'audit de l'ADR-005 : la lire suffit à connaître
#: la surface exposée. Une route publique de plus doit y être ajoutée à la
#: main, ce qui force la décision à être consciente.
ROUTES_PUBLIQUES: set[str] = {
    # Identité — on ne peut pas exiger un jeton pour en obtenir un.
    "v1:accounts:register",
    "v1:accounts:login",
    "v1:accounts:token-refresh",
    # Découverte — avant l'inscription.
    "v1:geography:country-list",
    "v1:geography:country-detail",
    "v1:geography:city-list",
    "v1:geography:city-detail",
    "v1:geography:zone-resolve",
    "v1:restaurants:restaurant-list",
    "v1:restaurants:restaurant-detail",
    "v1:catalog:category-list",
    "v1:catalog:category-detail",
    "v1:catalog:item-list",
    "v1:catalog:item-detail",
    # Le prestataire de paiement n'a pas de compte : il signe son corps.
    "v1:payments:webhook",
    # La part d'un paiement partagé, ouverte par le lien reçu sur une
    # messagerie. La moitié des convives d'un repas partagé n'ont pas de
    # compte, et exiger une inscription pour payer sa part ferait échouer la
    # fonctionnalité sur son cas le plus courant. Le justificatif est le jeton
    # aléatoire de l'URL, et il ne donne accès qu'à cette part — ni à la
    # commande, ni aux autres participants.
    "v1:payments:share",
    # Le schéma OpenAPI. Il décrit des routes que les applications appellent de
    # toute façon ; le fermer protégerait une liste d'URL, pas des données. Le
    # restreindre reste un réglage `SERVE_PERMISSIONS` d'une ligne.
    "schema",
}

#: Vues dont les permissions dépendent de la méthode HTTP.
#:
#: Elles échappent à l'audit statique ci-dessus : `get_permissions()` décide à
#: l'exécution, et lire `permission_classes` ne montre rien. Les déclarer ici
#: rend le trou visible — sans cette liste, une vue pourrait s'ouvrir
#: entièrement sans que le test d'audit ne bronche.
PERMISSIONS_DYNAMIQUES: set[str] = {
    # Les avis se lisent sans compte, s'écrivent en client authentifié. Le
    # comportement des deux méthodes est couvert par `tests/catalog`.
    "ReviewViewSet",
}


def _iter_patterns(
    patterns: list[URLPattern | URLResolver], prefix: str = ""
) -> list[tuple[str, object]]:
    """Aplatit l'arbre de routage en (nom complet, vue)."""
    found: list[tuple[str, object]] = []
    for entry in patterns:
        if isinstance(entry, URLResolver):
            namespace = f"{prefix}{entry.namespace}:" if entry.namespace else prefix
            found += _iter_patterns(entry.url_patterns, namespace)
        elif entry.name:
            found.append((f"{prefix}{entry.name}", entry.callback))
    return found


def _permission_classes(view: object) -> list[type]:
    """Permissions effectives d'une vue, quelle que soit sa forme.

    DRF expose la classe sous `cls` pour une `APIView` et sous `initkwargs`
    pour une action de ViewSet, qui peut redéfinir les permissions de sa
    route. Ne regarder que la première manquerait précisément les actions —
    c'est-à-dire les routes ajoutées après coup.
    """
    surchargees = getattr(view, "initkwargs", {}).get("permission_classes")
    if surchargees is not None:
        return list(surchargees)

    cls = getattr(view, "cls", None)
    return list(getattr(cls, "permission_classes", []))


class TestSurfacePublique:
    def test_la_liste_des_routes_ouvertes_est_exactement_celle_declaree(self) -> None:
        """ADR-005 — le refus est le défaut, et l'exception est écrite.

        L'échec dans un sens signale une route ouverte par mégarde ; dans
        l'autre, une entrée devenue obsolète. Les deux méritent d'être vues.
        """
        ouvertes = {
            nom
            for nom, vue in _iter_patterns(get_resolver().url_patterns)
            if AllowAny in _permission_classes(vue)
        }

        assert ouvertes == ROUTES_PUBLIQUES, (
            f"Ouvertes mais non déclarées : {sorted(ouvertes - ROUTES_PUBLIQUES)} ; "
            f"déclarées mais fermées : {sorted(ROUTES_PUBLIQUES - ouvertes)}"
        )

    def test_les_vues_a_permissions_dynamiques_sont_declarees(self) -> None:
        """Une vue qui décide ses permissions à l'exécution échappe à l'audit.

        C'est légitime — les avis se lisent sans compte et s'écrivent avec —
        mais cela doit rester rare et visible. Sans cette déclaration, une vue
        pourrait s'ouvrir entièrement sans que le test précédent ne bronche.
        """
        from rest_framework.views import APIView

        dynamiques = {
            vue.cls.__name__
            for _, vue in _iter_patterns(get_resolver().url_patterns)
            if (cls := getattr(vue, "cls", None)) is not None
            and issubclass(cls, APIView)
            and cls.get_permissions is not APIView.get_permissions
        }

        assert dynamiques == PERMISSIONS_DYNAMIQUES, (
            f"Non déclarées : {sorted(dynamiques - PERMISSIONS_DYNAMIQUES)} ; "
            f"déclarées à tort : {sorted(PERMISSIONS_DYNAMIQUES - dynamiques)}"
        )

    def test_aucune_route_n_est_sans_permission(self) -> None:
        """Une vue sans `permission_classes` hérite du défaut du projet, qui est
        `IsAuthenticated`. Le vérifier ici garantit que le réglage global n'a
        pas été vidé — auquel cas tout deviendrait public d'un coup, sans
        qu'aucun test métier ne le remarque.
        """
        from django.conf import settings

        assert settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == [
            "rest_framework.permissions.IsAuthenticated"
        ]


class TestCouches:
    def test_un_modele_n_importe_ni_vue_ni_serialiseur(self) -> None:
        """La dépendance va du transport vers le domaine, jamais l'inverse.

        Un modèle qui importe un sérialiseur rend le domaine inutilisable sans
        DRF — donc intestable sans lui, et inexportable vers une tâche, une
        commande ou un consommateur.
        """
        coupables: list[str] = []

        for module in iter_app_modules():
            if module.name not in {"models.py", "states.py", "signals.py"}:
                continue
            # Les imports sont lus dans l'arbre syntaxique, pas cherchés dans
            # le texte : « reviews » contient « views », et un test qui signale
            # cela n'est pas un test, c'est un obstacle qu'on finit par
            # désactiver.
            for importe in _imported_modules(module):
                if importe.endswith((".serializers", ".views")) or importe == "rest_framework":
                    coupables.append(f"{module.parent.name}/{module.name} → {importe}")

        assert not coupables, "Couches inversées :\n" + "\n".join(coupables)

    def test_les_services_ne_connaissent_pas_le_transport(self) -> None:
        """ADR-003 — un service ne sait rien de HTTP.

        Il reçoit des objets du domaine et lève des exceptions métier ; c'est
        la vue qui traduit en codes de statut. Un service qui renverrait une
        `Response` ne pourrait plus être appelé depuis une tâche Celery ni
        depuis un consommateur WebSocket.
        """
        coupables: list[str] = []

        for module in iter_app_modules():
            if module.name != "services.py":
                continue
            for importe in _imported_modules(module):
                # `rest_framework_simplejwt` est un **autre paquet** : frapper
                # et révoquer un jeton n'est pas du transport HTTP, et
                # `AuthService` a besoin de ses classes de jetons.
                if importe == "rest_framework" or importe.startswith("rest_framework."):
                    coupables.append(f"{module.parent.name}/services.py → {importe}")

        assert not coupables, "Services couplés au transport :\n" + "\n".join(coupables)


class TestModeles:
    def test_tout_modele_metier_porte_une_cle_uuid(self) -> None:
        """ADR-007, T5 — les identifiants exposés ne sont pas devinables.

        Une clé auto-incrémentée révèle le volume d'activité et rend
        l'énumération triviale : essayer `/orders/1/`, `/orders/2/` suffit à
        mesurer le chiffre d'affaires d'un concurrent.
        """
        exemptes = {
            # Table de jonction générée par Django pour `User.roles`.
            "User_roles",
        }

        coupables = [
            f"{model._meta.app_label}.{model.__name__}"
            for model in django_apps.get_models()
            if model._meta.app_label in {config.label for config in _business_apps()}
            and model.__name__ not in exemptes
            and not issubclass(model, UUIDModel)
        ]

        assert not coupables, f"Modèles sans clé UUID : {coupables}"


def _imported_modules(module: Path) -> set[str]:
    """Modules importés par ce fichier, lus dans l'arbre syntaxique."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}

    return found


def _business_apps() -> list[object]:
    return [config for config in django_apps.get_app_configs() if config.name.startswith("apps.")]
