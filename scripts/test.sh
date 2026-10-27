#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/agenthub-test.XXXXXXXXXX")
test_project=$(basename "$test_dir" | tr '[:upper:].' '[:lower:]-')
export AGENTHUB_TEST_REPORT_DIR="$repo_dir/backend/htmlcov/$test_project"

compose() {
    docker compose --project-directory "$repo_dir" \
        --env-file "$repo_dir/.env.example" -p "$test_project" \
        -f "$repo_dir/compose.test.yml" "$@"
}

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if ! compose down --volumes --remove-orphans; then
        printf 'Cleanup failed; retained test project: %s\n' "$test_project" >&2
        if [ "$result" -eq 0 ]; then result=1; fi
    fi
    rmdir "$test_dir" || true
    exit "$result"
}

# Never adopt resources belonging to an earlier run, even on a shared daemon.
for resource in container volume network; do
    case "$resource" in
        container) existing=$(docker ps -aq --filter "label=com.docker.compose.project=$test_project") ;;
        volume) existing=$(docker volume ls -q --filter "label=com.docker.compose.project=$test_project") ;;
        network) existing=$(docker network ls -q --filter "label=com.docker.compose.project=$test_project") ;;
    esac
    if [ -n "$existing" ]; then
        printf 'Refusing existing test project: %s\n' "$test_project" >&2
        rmdir "$test_dir"
        exit 1
    fi
done

compose config --quiet
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$AGENTHUB_TEST_REPORT_DIR"
printf 'Isolated test project: %s\nReports: %s\n' "$test_project" "$AGENTHUB_TEST_REPORT_DIR"
compose build backend minio
compose up -d --wait db redis minio mailpit
compose run --no-deps --rm -T backend bash -c \
    'bash scripts/prestart.sh && bash scripts/tests-start.sh "$@"' -- "$@"
