#!/usr/bin/env bash
# Cleanup script — убирает em dash и AI-style формулировки из созданных файлов
# Прогоняется один раз после init.sh

set -e

EXPECTED_DIR="schurfer"
CURRENT_DIR=$(basename "$PWD")
if [[ "$CURRENT_DIR" != "$EXPECTED_DIR" ]]; then
    echo "Запусти из папки 'schurfer'. Сейчас в: $PWD"
    exit 1
fi

echo "Cleanup AI-маркеров..."
echo ""

# Файлы которые чистим
FILES=(
    "README.md"
    "ARCHITECTURE.md"
    "ROADMAP.md"
    "Makefile"
    "init.sh"
    "docs/adr/0001-monorepo-structure.md"
    "docs/adr/0002-private-product-only.md"
    "docs/adr/0003-go-workspaces.md"
    "docs/adr/0004-self-hosted-ci.md"
    "docs/adr/0005-frontend-stack.md"
    "docs/adr/0006-backend-languages.md"
    "docs/adr/0007-trade-journal-first.md"
    "docs/strategies/pump_short_v1.md"
    "docs/strategies/README.md"
    "docs/runbooks/README.md"
    "apps/collectors/README.md"
    "apps/execution/README.md"
    "apps/api-gateway/README.md"
    "apps/analytics/README.md"
    "apps/telegram-bot/README.md"
    "apps/web/README.md"
    "packages/core/README.md"
    "packages/exchanges/README.md"
    "packages/indicators/README.md"
    "packages/journal/README.md"
    "infra/docker/README.md"
    "infra/terraform/README.md"
    "infra/scripts/README.md"
)

# sed на macOS требует -i ''
SED_INPLACE="sed -i ''"

for file in "${FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        continue
    fi

    # 1. Em dash "—" заменить на обычный дефис с пробелами или просто убрать
    #    Контекст: "X — Y" -> "X - Y"
    eval "$SED_INPLACE 's/—/-/g' '$file'"

    # 2. En dash "–" то же самое
    eval "$SED_INPLACE 's/–/-/g' '$file'"

    # 3. Smart quotes
    eval "$SED_INPLACE 's/“/\"/g; s/”/\"/g' '$file'"
    eval "$SED_INPLACE \$'s/\xe2\x80\x98/\x27/g; s/\xe2\x80\x99/\x27/g' '$file'"

    # 4. Многоточие "…" -> "..."
    eval "$SED_INPLACE 's/…/.../g' '$file'"

    echo "  cleaned: $file"
done

echo ""
echo "Готово. Проверь diff:"
echo "  git diff --stat"
echo ""
echo "Если ок — коммитим:"
echo "  git add -A"
echo "  git commit -m 'chore: cleanup formatting'"
echo "  git push"
