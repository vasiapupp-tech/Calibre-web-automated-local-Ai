#!/bin/bash
# Создание нового контейнера CWA с библиотекой Calibre для другой тематики книг.
#
# Использование:
#   ./setup_new_container.sh <тематика> <порт>
#
# Пример:
#   ./setup_new_container.sh math 1232
#
# Создаст:
#   - каталог проекта:  ~/data/.app/calibre/books_<тематика>
#   - контейнер:        Books-<тематика>
#   - библиотеку:       ~/data/calibre/books_<тематика>
#   - папку импорта:    ~/data/calibre/books_<тематика>_dwl
#   - порт:             <порт>:8083
#
# После запуска применит патчи (из ./patches) и перезапустит контейнер.

set -euo pipefail

DOC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOPIC="${1:-}"
PORT="${2:-}"

if [ -z "$TOPIC" ] || [ -z "$PORT" ]; then
    echo "Использование: $0 <тематика> <порт>"
    echo "Пример:       $0 math 1232"
    exit 1
fi

# Санитизация имени тематики (слаг: латиница в нижнем регистре, пробелы -> '-')
SLUG=$(echo "$TOPIC" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | sed 's/^-//;s/-$//')
if [ -z "$SLUG" ]; then
    echo "ОШИБКА: не удалось получить валидное имя из «$TOPIC»" >&2
    exit 1
fi

CONTAINER_NAME="Books-${SLUG}"
PROJECT_DIR="$HOME/data/.app/calibre/books_${SLUG}"
LIBRARY_DIR="$HOME/data/calibre/books_${SLUG}"
INGEST_DIR="$HOME/data/calibre/books_${SLUG}_dwl"

echo "=== Создание контейнера CWA ==="
echo "  Тематика:    $SLUG"
echo "  Контейнер:   $CONTAINER_NAME"
echo "  Порт:        ${PORT}:8083"
echo "  Проект:      $PROJECT_DIR"
echo "  Библиотека:  $LIBRARY_DIR"
echo "  Импорт:      $INGEST_DIR"
echo

# Проверка, что контейнер с таким именем ещё не существует
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "ОШИБКА: контейнер «$CONTAINER_NAME» уже существует." >&2
    exit 1
fi

if [ -d "$PROJECT_DIR" ]; then
    echo "ОШИБКА: каталог проекта «$PROJECT_DIR» уже существует." >&2
    exit 1
fi

# 1. Каталог проекта
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# 2. Копируем шаблоны
cp "$DOC_DIR/docker-compose.yml" .
cp -r "$DOC_DIR/custom-cont-init.d" .

# 3. Подставляем значения в docker-compose.yml
sed -i \
    -e "s/Books-Topic/${CONTAINER_NAME}/g" \
    -e "s/calibre-books-topic/calibre-${SLUG}/g" \
    -e "s#books_topic#books_${SLUG}#g" \
    -e "s/1232:8083/${PORT}:8083/" \
    docker-compose.yml

echo "docker-compose.yml подготовлен:"
grep -E "container_name|books_|:8083" docker-compose.yml | sed 's/^/    /'
echo

# 4. Копируем код CWA из образа в ./app.
#    bind mount ./app:/app НЕ копирует код из образа автоматически: пустая
#    папка ./app «затмевает» код в образе. Поэтому извлекаем его из образа
#    через временный контейнер, как и в вашем варианте, но без comment/restart.
echo "=== Копирование кода CWA из образа в ./app... ==="
docker create --name "${CONTAINER_NAME}-tmp" ghcr.io/crocodilestick/calibre-web-automated:latest >/dev/null
mkdir -p "$PROJECT_DIR/app"
docker cp "${CONTAINER_NAME}-tmp:/app/." "$PROJECT_DIR/app/"
docker rm "${CONTAINER_NAME}-tmp" >/dev/null

APP_TARGET="$PROJECT_DIR/app/calibre-web-automated/cps/metadata_helper.py"
if [ ! -f "$APP_TARGET" ]; then
    echo "ОШИБКА: не удалось скопировать код CWA в ./app." >&2
    exit 1
fi
echo "Код CWA скопирован в ./app."

# 5. Первый запуск контейнера
echo "=== Запуск контейнера... ==="
docker compose up -d

# 6. Применяем патчи (изменённые файлы CWA)
echo "=== Применение патчей... ==="
cp -r "$DOC_DIR/patches/"* "$PROJECT_DIR/app/calibre-web-automated/"
echo "Патчи применены."

# 7. Перезапуск
echo "=== Перезапуск контейнера... ==="
docker compose restart

echo
echo "=== Готово! ==="
echo "  Контейнер:     $CONTAINER_NAME"
echo "  Порт:          $PORT"
echo "  Веб-интерфейс: http://<хост>:$PORT"
echo "  Библиотека:    $LIBRARY_DIR"
echo "  Импорт:        $INGEST_DIR"
echo
echo "Дальше (в веб-интерфейсе CWA):"
echo "  1) Settings → Enable Automatic Metadata Fetching"
echo "  2) Settings → Use Local AI for Metadata Extraction"
echo "  3) AI Server URL = http://172.17.0.1:8899"
echo "  4) Задать admin-логин/пароль при первом входе"
echo
echo "Проверка статуса:  docker ps --filter name=$CONTAINER_NAME"
echo "Логи:              docker logs -f $CONTAINER_NAME"
