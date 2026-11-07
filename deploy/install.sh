#!/usr/bin/env bash
# Установка BIS на виртуальную машину (Ubuntu 22.04/24.04, Yandex Cloud или любой VPS).
#
#   curl -fsSL https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
# Что делает: ставит Docker, скачивает BIS, спрашивает ключ и каталог Yandex AI Studio, создаёт настройки
# с надёжными паролями, поднимает базу, API, веб-интерфейс и фоновые задачи, создаёт организацию и администратора.
# Повторный запуск обновляет BIS до последней версии и сохраняет настройки и данные.
set -euo pipefail

REPO="${BIS_REPO:-https://github.com/baboevazamatkz-create/bis.git}"
BRANCH="${BIS_BRANCH:-claude/construction-ai-automation-system-lya9lz}"
DIR="${BIS_DIR:-/opt/bis}"
ENV_FILE="$DIR/deploy/.env"

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }
ask() { local v; read -r -p "$1" v </dev/tty; printf '%s' "$v"; }
ask_secret() { local v; read -r -s -p "$1" v </dev/tty; echo >/dev/tty; printf '%s' "$v"; }

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash install.sh"; exit 1; }

say "1/6 Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl git openssl
  curl -fsSL https://get.docker.com | sh
fi
apt-get install -y -q git openssl >/dev/null 2>&1 || true
docker compose version >/dev/null

say "2/6 Код BIS ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" --depth 1 "$REPO" "$DIR"
fi

PUBLIC_IP="$(curl -fsS -m 5 https://ipinfo.io/ip 2>/dev/null || hostname -I | awk '{print $1}')"

if [ ! -f "$ENV_FILE" ]; then
  say "3/6 Настройки"
  echo "Ключ и каталог Yandex AI Studio. Сервисному аккаунту ключа нужны роли:"
  echo "  ai.languageModels.user, ai.speechkit-stt.user, ai.vision.user"
  YA_KEY="$(ask_secret 'Секретный API-ключ (AQVN…, ввод скрыт): ')"
  YA_FOLDER="$(ask 'Идентификатор каталога (b1g…): ')"
  TG_TOKEN="$(ask 'Токен Telegram-бота от @BotFather (Enter — пропустить): ')"
  TG_NAME=""
  [ -n "$TG_TOKEN" ] && TG_NAME="$(ask 'Имя бота без @ (например my_bis_bot): ')"
  umask 077
  cat >"$ENV_FILE" <<EOF
BIS_ENV=prod
BIS_SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
BIS_HTTP_PORT=80
BIS_CORS_ORIGINS=["http://$PUBLIC_IP"]
BIS_PUBLIC_URL=http://$PUBLIC_IP
BIS_EMBEDDING_PROVIDER=yandex
BIS_VECTOR_ENABLED=false

# Yandex AI Studio: модели, голос, сканы
BIS_LLM_CONFIG_FILE=/config/llm.yandex.json
YANDEX_API_KEY=$YA_KEY
YANDEX_FOLDER_ID=$YA_FOLDER
BIS_STT_PROVIDER=yandex
BIS_OCR_PROVIDER=yandex

# Telegram-бот (режим опроса — без домена и HTTPS)
BIS_TELEGRAM_BOT_TOKEN=$TG_TOKEN
BIS_TELEGRAM_BOT_USERNAME=$TG_NAME
EOF
  chmod 600 "$ENV_FILE"
else
  say "3/6 Настройки: $ENV_FILE уже есть — оставляю как есть"
fi

say "4/6 Сборка и запуск (первый раз — 5–10 минут)"
cd "$DIR/deploy"
PROFILES=()
grep -q '^BIS_TELEGRAM_BOT_TOKEN=.\+' .env && PROFILES+=(--profile telegram)
docker compose "${PROFILES[@]}" up -d --build

say "5/6 Проверка"
for _ in $(seq 1 60); do
  curl -fsS -m 3 http://localhost/api/health >/dev/null 2>&1 && break
  sleep 3
done
curl -fsS http://localhost/api/health >/dev/null || { echo "API не ответил — смотрите: docker compose -f $DIR/deploy/docker-compose.yml logs api"; exit 1; }
docker compose exec -T api bis llm-check || echo "Модели отвечают не все — проверьте ключ, каталог и роли (выше подсказка)."

if [ ! -f "$DIR/deploy/.installed" ]; then
  say "6/6 Организация и администратор"
  if [ "$(ask 'Загрузить демо-данные для пробы (объект, смета, договор)? [y/N]: ')" = "y" ]; then
    docker compose exec -T api bis seed-demo
    echo "Демо-вход: admin@demo.bis / demo12345 — смените пароль в «Настройки → Пользователи»."
  else
    ORG="$(ask 'Название организации (например ООО «СтройМонтаж»): ')"
    INN="$(ask 'ИНН организации: ')"
    EMAIL="$(ask 'Email администратора: ')"
    NAME="$(ask 'ФИО администратора: ')"
    PASS="$(ask_secret 'Пароль администратора (не короче 8 символов): ')"
    docker compose exec -T api bis create-org --name "$ORG" --inn "$INN" --admin-email "$EMAIL" \
      --admin-password "$PASS" --admin-name "$NAME"
  fi
  touch "$DIR/deploy/.installed"
fi

cat <<EOF

✅ BIS работает: http://$PUBLIC_IP

  Обновить:        sudo bash $DIR/deploy/install.sh
  Журналы:         cd $DIR/deploy && docker compose logs -f api
  Проверить ИИ:    cd $DIR/deploy && docker compose exec api bis llm-check
  Настройки:       $ENV_FILE (после правки: cd $DIR/deploy && docker compose up -d)
EOF
