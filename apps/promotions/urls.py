"""Routes des promotions — montées sous `/api/v1/promotions/`.

Aucune route publique : un client saisit un code, il n'en liste pas. Ce qu'il
voit d'une promotion lui arrive par le devis de commande (`orders/preview/`) ou
par sa récompense de fidélité, jamais par une liste.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.promotions import backoffice

app_name = "promotions"

router = DefaultRouter()
router.register("", backoffice.ManagedPromotionViewSet, basename="promotion")

urlpatterns = router.urls
