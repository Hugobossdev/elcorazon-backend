#!/usr/bin/env bash
#
# Déploiement de production.
#
#   ./deploy/deploy.sh              # déploie la révision courante
#   ./deploy/deploy.sh --cert-init  # premier certificat TLS, puis déploie
#
# Le script est **idempotent** et s'arrête à la première erreur : mieux vaut un
# déploiement interrompu à mi-course qu'un service qui tourne à moitié migré.

set -Eeuo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RACINE"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
INIT_CERT=false
[[ "${1:-}" == "--cert-init" ]] && INIT_CERT=true

info()   { printf '\033[1;34m▸\033[0m %s\n' "$1"; }
succes() { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
echouer() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ─────────────────────────────────────────────────────────── vérifications
info 'Vérification de la configuration'

[[ -f .env.prod ]] || echouer '.env.prod absent — copier .env.prod.example et le renseigner.'
[[ -f secrets/jwt.pem && -f secrets/jwt.pub ]] || echouer \
  'Clés JWT absentes de secrets/. Générer avec :
     openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out secrets/jwt.pem
     openssl rsa -pubout -in secrets/jwt.pem -out secrets/jwt.pub'

set -a; source .env.prod; set +a

# Ces quatre variables n'ont pas de valeur par défaut acceptable. `prod.py` le
# vérifie aussi au démarrage ; l'attraper ici évite un conteneur qui redémarre
# en boucle sans que la cause soit lisible.
for var in DJANGO_SECRET_KEY POSTGRES_PASSWORD REDIS_PASSWORD DOMAIN; do
  [[ -n "${!var:-}" ]] || echouer "$var est vide dans .env.prod"
done

[[ "${DJANGO_DEBUG:-False}" == "False" ]] || echouer \
  'DJANGO_DEBUG doit être False en production.'

succes 'Configuration complète'

# ───────────────────────────────────────────────────────── configuration Nginx
info 'Génération de la configuration Nginx'
export DOMAIN
envsubst '${DOMAIN}' < deploy/nginx.prod.conf > deploy/nginx.generated.conf
succes "Nginx configuré pour $DOMAIN"

# ─────────────────────────────────────────────────────── premier certificat
if $INIT_CERT; then
  info 'Obtention du premier certificat TLS'

  mkdir -p deploy/certbot/www deploy/certbot/conf

  # Nginx doit répondre en HTTP pour que le défi ACME aboutisse, mais il ne peut
  # pas démarrer si le certificat référencé n'existe pas encore. On sert donc le
  # défi avec une configuration minimale, le temps de l'obtenir.
  cat > deploy/nginx.generated.conf <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'en cours de déploiement'; add_header Content-Type text/plain; }
}
NGINX

  $COMPOSE up -d nginx
  sleep 5

  $COMPOSE run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" \
    --email "${ACME_EMAIL:?ACME_EMAIL requis pour le premier certificat}" \
    --agree-tos --no-eff-email --non-interactive \
    || echouer "Certbot a échoué. Vérifier que $DOMAIN pointe bien vers ce serveur."

  # Retour à la configuration complète, maintenant que le certificat existe.
  envsubst '${DOMAIN}' < deploy/nginx.prod.conf > deploy/nginx.generated.conf
  succes 'Certificat obtenu'
fi

# ───────────────────────────────────────────────────────── sauvegarde préalable
if $COMPOSE ps db --status running --quiet 2>/dev/null | grep -q .; then
  info 'Sauvegarde de la base avant migration'
  ./deploy/backup.sh --silencieux
  succes 'Sauvegarde effectuée'
fi

# ──────────────────────────────────────────────────────────────── construction
info 'Construction de l’image'
$COMPOSE build api
succes 'Image construite'

# ───────────────────────────────────────────────────────────────── démarrage
info 'Démarrage des services'
# `--wait` fait attendre les healthchecks : le script ne rend la main qu'une
# fois l'API réellement joignable, pas seulement le conteneur lancé.
$COMPOSE up -d --wait db redis minio
$COMPOSE up -d --wait api
$COMPOSE up -d worker beat nginx certbot
succes 'Services démarrés'

# ───────────────────────────────────────────────────────────── vérifications
info 'Vérification du déploiement'

$COMPOSE exec -T api python manage.py check --deploy --fail-level WARNING \
  || echouer 'check --deploy signale un problème de configuration.'

$COMPOSE exec -T api python manage.py migrate --check \
  || echouer 'Des migrations restent à appliquer.'

if ! curl -fsS "https://${DOMAIN}/health/" > /dev/null; then
  echouer "L'API ne répond pas sur https://${DOMAIN}/health/"
fi

succes "Déploiement terminé — https://${DOMAIN}"

printf '\n'
info 'État des services'
$COMPOSE ps
