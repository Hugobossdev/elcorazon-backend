"""Routes d'administration des comptes — montées sous `/api/v1/administration/`.

Séparées de `apps.accounts.urls`, qui porte l'authentification, parce que les
deux n'ont ni le même public ni le même contrat : `/auth/` est ce qu'appelle
n'importe qui pour obtenir un jeton, `/administration/` ce qu'appelle le
back-office une fois qu'il en a un, muni des bonnes permissions.

Le préfixe est le même pour les deux ressources parce qu'elles sont le même
écran du produit — « Rôles & permissions », « Clients » — et qu'un client Dart
généré depuis le schéma y trouve un module cohérent.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.accounts import backoffice

app_name = "administration"

router = DefaultRouter()
router.register("customers", backoffice.CustomerViewSet, basename="customer")
router.register("roles", backoffice.RoleViewSet, basename="role")

urlpatterns = router.urls
