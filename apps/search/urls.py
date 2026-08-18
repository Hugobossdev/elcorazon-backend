"""Route de la recherche transverse — montée sous `/api/v1/search/`.

Une seule vue, sans routeur : ce n'est pas une collection qu'on liste ni un
objet qu'on adresse, c'est une question qu'on pose.
"""

from __future__ import annotations

from django.urls import path

from apps.search import views

app_name = "search"

urlpatterns = [
    path("", views.SearchView.as_view(), name="search"),
]
