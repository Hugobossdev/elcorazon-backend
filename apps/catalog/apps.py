from __future__ import annotations

from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "apps.catalog"
    label = "catalog"
    verbose_name = "Catalogue"

    def ready(self) -> None:
        """Abonne le catalogue à la vérification de complétude d'un établissement.

        Et à la duplication d'un établissement, par le même chemin.

        Même mécanisme que les signaux de `loyalty` : l'abonné connaît
        l'émetteur, et `restaurants` n'apprend rien de `catalog`. Sans le
        premier abonnement, un établissement s'ouvrirait avec une carte vide ;
        sans le second, « dupliquer » ne copierait que la fiche.
        """
        from apps.catalog.duplication import SECTION_CATALOG, copier_le_catalogue
        from apps.catalog.readiness import catalogue_gaps
        from apps.restaurants.duplication import register_section
        from apps.restaurants.readiness import register_readiness_check

        register_readiness_check(catalogue_gaps)

        # Même mécanisme, même sens d'arête : `restaurants` sait qu'une section
        # nommée « catalog » existe et sait l'appeler, sans rien connaître des
        # catégories ni des articles.
        register_section(SECTION_CATALOG, copier_le_catalogue)
