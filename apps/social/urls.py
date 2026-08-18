"""Routes du social — montées sous `/api/v1/social/`."""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.social import views

app_name = "social"

router = DefaultRouter()
router.register("groups", views.SocialGroupViewSet, basename="group")
router.register("posts", views.PostViewSet, basename="post")

urlpatterns = router.urls
