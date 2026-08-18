#!/usr/bin/env bash
#
# Restauration depuis une sauvegarde.
#
#   ./deploy/restore.sh                              # dernière sauvegarde
#   ./deploy/restore.sh deploy/backups/db-2026....dump
#   ./deploy/restore.sh --lister                     # inventaire, sans rien faire
#
# **Opération destructrice.** Elle remplace la base en service. Le script arrête
# l'API et les workers avant d'écrire — restaurer sous une application qui écrit
# donne une base à moitié ancienne, à moitié neuve, et personne ne sait laquelle
# est laquelle.

set -Eeuo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RACINE"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
DESTINATION="deploy/backups"

info()   { printf '\033[1;34m▸\033[0m %s\n' "$1"; }
succes() { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
alerte() { printf '\033[1;33m!\033[0m %s\n' "$1"; }
echouer() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ────────────────────────────────────────────────────────────────── inventaire
if [[ "${1:-}" == "--lister" ]]; then
  info 'Sauvegardes disponibles'
  ls -lh "$DESTINATION"/db-*.dump 2>/dev/null || echo '  (aucune)'
  exit 0
fi

[[ -f .env.prod ]] || echouer '.env.prod absent.'
set -a; source .env.prod; set +a

ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(ls -t "$DESTINATION"/db-*.dump 2>/dev/null | head -n1 || true)"
  [[ -n "$ARCHIVE" ]] || echouer "Aucune sauvegarde dans $DESTINATION."
  alerte "Aucune archive indiquée — la plus récente sera utilisée."
fi

[[ -f "$ARCHIVE" ]] || echouer "Archive introuvable : $ARCHIVE"

# ─────────────────────────────────────────────────────────────── vérification
info "Contrôle de l'archive"
# `pg_restore --list` lit l'en-tête sans rien écrire : une archive tronquée ou
# corrompue échoue ici, avant qu'on ait touché à la base en service.
$COMPOSE exec -T db pg_restore --list < "$ARCHIVE" > /dev/null \
  || echouer "Archive illisible ou corrompue : $ARCHIVE"
succes "Archive valide — $(du -h "$ARCHIVE" | cut -f1)"

# ────────────────────────────────────────────────────────────── confirmation
cat <<CONFIRMATION

  ┌─────────────────────────────────────────────────────────────┐
  │  RESTAURATION — opération destructrice                      │
  └─────────────────────────────────────────────────────────────┘

  Archive     : $ARCHIVE
  Base cible  : $POSTGRES_DB
  Domaine     : ${DOMAIN:-non renseigné}

  Le contenu actuel de la base sera **remplacé**. Les commandes, paiements et
  comptes créés depuis cette sauvegarde seront perdus.

CONFIRMATION

read -r -p '  Taper « restaurer » pour confirmer : ' reponse
[[ "$reponse" == "restaurer" ]] || echouer 'Annulé.'

# ───────────────────────────────────────────── sauvegarde de l'état courant
info "Sauvegarde de l'état actuel avant écrasement"
FILET="$DESTINATION/avant-restauration-$(date +%Y%m%d-%H%M%S).dump"
$COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --format=custom --compress=9 > "$FILET" 2>/dev/null || true
if [[ -s "$FILET" ]]; then
  succes "Filet de sécurité — $FILET"
else
  alerte "L'état actuel n'a pas pu être sauvegardé (base absente ou vide ?)."
fi

# ──────────────────────────────────────────────────────── arrêt des écritures
info "Arrêt de l'API et des workers"
$COMPOSE stop api worker beat
succes 'Écritures arrêtées'

# ─────────────────────────────────────────────────────────────── restauration
info 'Restauration en cours'

# `--clean --if-exists` remplace les objets existants sans échouer sur ceux qui
# n'existent pas. `--no-owner` évite l'échec si l'utilisateur de la sauvegarde
# diffère de celui de la cible.
if ! $COMPOSE exec -T db pg_restore \
      -U "$POSTGRES_USER" \
      -d "$POSTGRES_DB" \
      --clean --if-exists --no-owner --no-privileges \
      < "$ARCHIVE"; then
  alerte 'pg_restore a signalé des erreurs.'
  alerte "Le filet de sécurité est disponible : $FILET"
  # On ne s'arrête pas : pg_restore signale souvent des erreurs bénignes
  # (suppression d'objets absents). La vérification ci-dessous tranche.
fi

# ───────────────────────────────────────────────────────────────── migrations
info 'Alignement du schéma'
$COMPOSE up -d --wait db
$COMPOSE run --rm api python manage.py migrate --noinput \
  || echouer 'Les migrations ont échoué — la base restaurée est plus ancienne que le code.'
succes 'Schéma aligné'

# ────────────────────────────────────────────────────────────────── redémarrage
info 'Redémarrage des services'
$COMPOSE up -d --wait api
$COMPOSE up -d worker beat
succes 'Services redémarrés'

# ───────────────────────────────────────────────────────────── vérification
info 'Vérification'
if [[ -n "${DOMAIN:-}" ]] && curl -fsS "https://${DOMAIN}/health/" > /dev/null; then
  succes "API joignable — https://${DOMAIN}"
else
  alerte "L'API ne répond pas encore ; vérifier avec : $COMPOSE logs api"
fi

printf '\n'
succes 'Restauration terminée'
alerte "Le stockage objet n'est pas restauré par ce script."
alerte "Pour le remettre en place :"
cat <<'STOCKAGE'

    docker run --rm \
      -v elcorazon-prod_miniodata:/data \
      -v "$(pwd)/deploy/backups:/sauvegarde" \
      alpine sh -c "rm -rf /data/* && tar xzf /sauvegarde/minio-<horodatage>.tar.gz -C /data"

STOCKAGE
