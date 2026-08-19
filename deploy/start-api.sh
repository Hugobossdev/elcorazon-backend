#!/bin/sh
# Démarrage de l'API — préparation puis service.
#
# Cette séquence vit dans l'image, et non dans le `dockerCommand` du blueprint
# Render, parce qu'un `dockerCommand` n'est appliqué qu'aux services *créés par
# le blueprint*. Un service créé à la main depuis le tableau de bord exécute le
# `CMD` de l'image et ignore `render.yaml` : les migrations ne passaient pas, la
# base restait vide et toute route qui la touche répondait 500, tandis que
# `/health/` — volontairement sans accès base — répondait 200. Un service
# déclaré vivant sur une base sans tables : la panne la plus coûteuse à
# diagnostiquer. En la plaçant ici, la préparation suit l'image quel que soit
# l'hébergeur et la façon dont le service a été créé.
set -e

# Sans `set -e`, un échec de `migrate` laisserait uvicorn démarrer sur une base
# dans un état inconnu. Mieux vaut un déploiement refusé, avec l'erreur en clair
# dans les journaux, qu'un service en ligne qui répond faux.
python manage.py migrate --noinput

# `collectstatic` au démarrage et non à la construction de l'image : `base.py`
# lit `DJANGO_SECRET_KEY` à l'import sans valeur de repli, donc aucune commande
# Django ne s'exécute pendant le build. Sans cette ligne, WhiteNoise avertit
# « No directory at: /app/staticfiles/ » et `/admin/` s'affiche sans sa feuille
# de style.
python manage.py collectstatic --noinput

# Compte d'exploitation. L'offre gratuite de Render n'ouvre ni shell ni SSH : il
# n'existe aucun autre moment pour le créer, et sans lui `/admin/` est
# accessible mais définitivement inutilisable.
#
# `|| true` : la commande échoue quand le compte existe déjà, c'est-à-dire à
# tous les démarrages sauf le premier. Sans ce garde-fou, l'instance refuserait
# de redémarrer dès le deuxième déploiement — d'où la sortie de `set -e` le
# temps de cette ligne. Le test sur la variable évite d'exécuter une commande
# vouée à l'échec là où les trois variables ne sont pas renseignées.
if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ]; then
    python manage.py createsuperuser --noinput || true
fi

# `exec` : uvicorn remplace le shell et devient le PID 1. Sans lui, les signaux
# d'arrêt de l'orchestrateur s'adressent au shell, qui ne les relaie pas — le
# service serait tué au bout du délai de grâce au lieu de fermer proprement ses
# connexions.
#
# `${PORT:-8000}` : Render impose le port par cette variable. La valeur de repli
# couvre un `docker run` local.
exec uvicorn config.asgi:application --host 0.0.0.0 --port "${PORT:-8000}"
