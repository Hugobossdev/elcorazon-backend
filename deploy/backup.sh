#!/usr/bin/env bash
#
# Sauvegarde de la base et du stockage objet.
#
#   ./deploy/backup.sh                 # sauvegarde complète, horodatée
#   ./deploy/backup.sh --silencieux    # sans sortie, pour appel depuis deploy.sh
#   ./deploy/backup.sh --base-seule    # PostgreSQL uniquement
#
# Une sauvegarde qu'on ne restaure jamais n'est pas une sauvegarde. `restore.sh`
# est le pendant obligatoire de ce script, et il doit être exécuté au moins une
# fois sur un environnement de recette avant de compter dessus.

set -Eeuo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RACINE"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
DESTINATION="deploy/backups"
HORODATAGE="$(date +%Y%m%d-%H%M%S)"
RETENTION_JOURS="${BACKUP_RETENTION_DAYS:-30}"

SILENCIEUX=false
BASE_SEULE=false
for arg in "$@"; do
  case "$arg" in
    --silencieux) SILENCIEUX=true ;;
    --base-seule) BASE_SEULE=true ;;
  esac
done

dire() { $SILENCIEUX || printf '\033[1;34m▸\033[0m %s\n' "$1"; }
succes() { $SILENCIEUX || printf '\033[1;32m✓\033[0m %s\n' "$1"; }
echouer() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[[ -f .env.prod ]] || echouer '.env.prod absent.'
set -a; source .env.prod; set +a

mkdir -p "$DESTINATION"

# ──────────────────────────────────────────────────────────────── PostgreSQL
dire 'Sauvegarde de PostgreSQL'

ARCHIVE_BASE="$DESTINATION/db-$HORODATAGE.dump"

# Format personnalisé (`-Fc`) et non SQL brut : il se restaure sélectivement,
# se compresse seul, et `pg_restore` peut en lister le contenu sans le charger.
$COMPOSE exec -T db pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  --format=custom \
  --compress=9 \
  > "$ARCHIVE_BASE" || echouer 'pg_dump a échoué.'

# Une archive vide est un échec silencieux : pg_dump peut rendre 0 en écrivant
# un fichier tronqué si le conteneur meurt en cours de route.
TAILLE=$(wc -c < "$ARCHIVE_BASE")
[[ "$TAILLE" -gt 1024 ]] || echouer "Archive suspecte : $TAILLE octets."

succes "Base sauvegardée — $(du -h "$ARCHIVE_BASE" | cut -f1)"

# ────────────────────────────────────────────────────────────── stockage objet
if ! $BASE_SEULE; then
  dire 'Sauvegarde du stockage objet'

  ARCHIVE_MEDIA="$DESTINATION/minio-$HORODATAGE.tar.gz"

  # Le stockage porte les pièces justificatives des livreurs et les images du
  # catalogue. Les perdre ne casse pas le service mais oblige chaque livreur à
  # redéposer son dossier.
  docker run --rm \
    -v elcorazon-prod_miniodata:/data:ro \
    -v "$(pwd)/$DESTINATION:/sauvegarde" \
    alpine tar czf "/sauvegarde/$(basename "$ARCHIVE_MEDIA")" -C /data . \
    || echouer 'Sauvegarde du stockage échouée.'

  succes "Stockage sauvegardé — $(du -h "$ARCHIVE_MEDIA" | cut -f1)"
fi

# ─────────────────────────────────────────────────────── secrets : rappel seul
if ! $SILENCIEUX; then
  cat <<'RAPPEL'

  Ce script ne sauvegarde pas les secrets, et c'est volontaire :
    • secrets/jwt.pem et jwt.pub — les perdre invalide tous les jetons en
      circulation, tout le monde se reconnecte ;
    • .env.prod — mots de passe de base, de Redis, du stockage, clés PayDunya.

  Ils doivent être conservés **ailleurs** : un coffre, un gestionnaire de
  secrets. Les mettre dans la même archive que la base annulerait l'intérêt de
  chiffrer l'une ou l'autre.
RAPPEL
fi

# ───────────────────────────────────────────────────────────────── rétention
dire "Purge des sauvegardes de plus de $RETENTION_JOURS jours"
find "$DESTINATION" -name 'db-*.dump'      -mtime "+$RETENTION_JOURS" -delete
find "$DESTINATION" -name 'minio-*.tar.gz' -mtime "+$RETENTION_JOURS" -delete

succes "Sauvegarde terminée — $DESTINATION/*-$HORODATAGE.*"
