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
# et se déposent dans le dossier `products`. Au démarrage, cela allongerait le
# boot de plusieurs minutes et ferait dépendre la mise en service de la
# disponibilité d'un CDN — pour un déploiement qui n'a pas nécessairement de
# stockage objet configuré (`CLOUDINARY_*` est en `sync: false`). Le
# téléchargement reste donc l'affaire d'un poste de développement, une fois pour
# toutes ; `attach_product_images`, plus bas, se charge d'en faire profiter cet
# environnement-ci sans rien retélécharger.
#
# Pas de `|| true`, contrairement au compte ci-dessus : `set -e` s'applique. Un
# échec ici n'est pas un état attendu comme l'est un compte déjà créé, c'est un
# seed cassé — et le blueprint énonce déjà la règle, mieux vaut un déploiement
# refusé avec l'erreur en clair qu'un service en ligne qui répond faux.
if [ "${SEED_DEMO_DATA:-}" = "true" ]; then
    python manage.py seed_reference_data
    python manage.py seed_full_catalog

    # Les photos ne sont pas téléchargées ici, mais elles sont déjà sur le
    # stockage objet : le compte Cloudinary est le même pour tous les
    # environnements. Cette commande ne fait que poser le lien manquant entre la
    # ligne et l'objet — elle n'envoie aucun octet, et ne renseigne une colonne
    # qu'après avoir vérifié auprès du fournisseur que le fichier existe.
    #
    # Sa place est dans ce bloc et non après le `fi` : ce sont les photos de la
    # carte de démonstration. Les slugs d'une exploitation réelle ne désignent
    # aucun de ces fichiers — la commande n'y écrirait rien, mais interrogerait
    # quand même le fournisseur article par article.
    #
    # Pas de `|| true` : elle ne lève pas sur un fournisseur injoignable, elle
    # le signale et laisse la carte sans photos, que le redémarrage suivant
    # rattrapera. Un échec qui remonte jusqu'ici est donc une vraie anomalie.
    python manage.py attach_product_images
fi

# `exec` : uvicorn remplace le shell et devient le PID 1. Sans lui, les signaux
# d'arrêt de l'orchestrateur s'adressent au shell, qui ne les relaie pas — le
# service serait tué au bout du délai de grâce au lieu de fermer proprement ses
# connexions.
#
# `${PORT:-8000}` : Render impose le port par cette variable. La valeur de repli
# couvre un `docker run` local.
exec uvicorn config.asgi:application --host 0.0.0.0 --port "${PORT:-8000}"
