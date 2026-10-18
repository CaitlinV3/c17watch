#!/usr/bin/env sh
# Regenerate requirements.txt from requirements.in: exact versions + SHA-256 hashes.
#
#   scripts/lock.sh             re-lock, keeping current versions where possible
#   scripts/lock.sh --upgrade   move everything to the newest versions allowed
#   scripts/lock.sh --upgrade-package urllib3   bump just one package
#
# Hashes cover every published file for each version (Linux, macOS, sdist), so a
# lock made on your Mac is valid inside the Linux container.
set -eu
cd "$(dirname "$0")/.."
if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "Activate your venv first:  source .venv/bin/activate" >&2
  exit 1
fi
python -m pip install --quiet --upgrade pip-tools
python -m piptools compile --quiet --generate-hashes --strip-extras \
  --output-file requirements.txt "$@" requirements.in
python -m pip install --quiet --require-hashes --no-deps -r requirements.txt
python -m unittest discover -s tests -t . >/dev/null 2>&1 \
  && echo "requirements.txt updated; tests pass. Review 'git diff requirements.txt', then commit." \
  || { echo "requirements.txt updated but TESTS FAIL; run: python -m unittest discover -s tests -t . -v" >&2; exit 1; }
