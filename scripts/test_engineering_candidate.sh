#!/bin/sh
# Run a complete, frozen source snapshot without mounting production data.
# Usage: sh scripts/test_engineering_candidate.sh /absolute/source image [pytest args]
set -eu
source_dir=${1:?absolute source snapshot required}
qa_image=${2:?explicit test image required}
shift 2
case "$source_dir" in /*) ;; *) echo "Source snapshot must be absolute" >&2; exit 2;; esac
test -f "$source_dir/pyproject.toml"
test -f "$source_dir/.gitignore"
test -f "$source_dir/tests/conftest.py"
test -f "$source_dir/services/hwfit/data/hf_models.json"
test ! -e "$source_dir/.env"
test ! -d "$source_dir/data"
docker run --rm --network none --cpus 4 --memory 8g \
  -e DATABASE_URL=sqlite:///:memory: \
  -v "$source_dir:/candidate:ro" --entrypoint sh "$qa_image" -c '
    set -eu
    cp -a /candidate /tmp/odysseus-qa
    cd /tmp/odysseus-qa
    exec python -m pytest "$@"
  ' sh "$@"
