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

# Données de démonstration. Conditionnées à `SEED_DEMO_DATA`, et non posées à
# tous les démarrages : cette même image sert l'API, les workers et beat (ADR-001)
# et a vocation à porter une exploitation réelle. Un seed inconditionnel y
# injecterait une carte fictive dans la base d'un vrai restaurant au premier
# déploiement — le genre de dégât qu'on ne remarque qu'une fois les commandes
# passées dessus.
#
# L'offre gratuite de Render n'ouvre ni shell ni SSH : c'est ici ou nulle part.
# Sans ces deux lignes, un déploiement de démonstration migre correctement, sert
# `/api/v1/catalog/items/` en 200 — et rend une liste vide, ce qui ressemble
# beaucoup à une panne côté application alors que le serveur va bien.
#
# L'ordre n'est pas indifférent : `seed_full_catalog` cherche son restaurant par
# slug et s'arrête sur une `CommandError` s'il ne le trouve pas. C'est
# `seed_reference_data` qui pose le pays, la ville, la zone et l'établissement.
#
# Les deux sont idempotentes (`get_or_create`), donc rejouées sans dommage à
# chaque redémarrage — et un redémarrage est fréquent ici, l'instance gratuite
# s'endormant après quinze minutes.
#
# Sans `--with-images` : les photos se téléchargent depuis des hébergeurs tiers
# et se déposent dans le compartiment `products`. Au démarrage, cela allongerait
# le boot de plusieurs minutes et ferait dépendre la mise en service de la
# disponibilité d'un CDN — pour un déploiement qui n'a pas nécessairement de
# stockage objet configuré (`S3_*` est en `sync: false`). La carte s'affiche donc
# sans photos ; les ajouter est un `--with-images` le jour où R2 est branché.
#
# Pas de `|| true`, contrairement au compte ci-dessus : `set -e` s'applique. Un
# échec ici n'est pas un état attendu comme l'est un compte déjà créé, c'est un
# seed cassé — et le blueprint énonce déjà la règle, mieux vaut un déploiement
# refusé avec l'erreur en clair qu'un service en ligne qui répond faux.
if [ "${SEED_DEMO_DATA:-}" = "true" ]; then
    python manage.py seed_reference_data
    python manage.py seed_full_catalog
fi

# `exec` : uvicorn remplace le shell et devient le PID 1. Sans lui, les signaux
# d'arrêt de l'orchestrateur s'adressent au shell, qui ne les relaie pas — le
# service serait tué au bout du délai de grâce au lieu de fermer proprement ses
# connexions.
#
# `${PORT:-8000}` : Render impose le port par cette variable. La valeur de repli
# couvre un `docker run` local.
exec uvicorn config.asgi:application --host 0.0.0.0 --port "${PORT:-8000}"
