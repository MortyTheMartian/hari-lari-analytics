# Activate the project venv and load environment variables from .env.
# Usage: source setup.sh

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Run this with: source setup.sh"
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
  echo "No .venv found. Create it with: uv sync"
  return 1
fi
source "$PROJECT_ROOT/.venv/bin/activate"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi
