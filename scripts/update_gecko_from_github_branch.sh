#!/usr/bin/env sh
# Update the Gecko custom integration from a GitHub branch (no HACS release needed).
#
# Intended for Home Assistant OS: run inside "Advanced SSH & Web Terminal" or
# "Terminal & SSH" where /config is writable (usually mounted at /config).
#
# Usage:
#   chmod +x update_gecko_from_github_branch.sh
#   ./update_gecko_from_github_branch.sh
#
# Optional environment overrides:
#   CONFIG_DIR=/config              HA configuration directory
#   REPO=markus-lassfolk/hass_gecko_in_touch3_spa
#   BRANCH=feature/app-client-auth-flow
#   DRY_RUN=1                     only show what would be done

set -eu

CONFIG_DIR="${CONFIG_DIR:-/config}"
REPO="${REPO:-markus-lassfolk/hass_gecko_in_touch3_spa}"
BRANCH="${BRANCH:-feature/app-client-auth-flow}"
DEST="${CONFIG_DIR}/custom_components/gecko"
ARCHIVE_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"

if [ ! -d "${CONFIG_DIR}" ]; then
  echo "ERROR: CONFIG_DIR does not exist: ${CONFIG_DIR}" >&2
  echo "Set CONFIG_DIR to your HA config path (e.g. /config)." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  echo "ERROR: need curl or wget to download the archive." >&2
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  echo "ERROR: need tar to extract the archive." >&2
  exit 1
fi

TMPDIR="${TMPDIR:-/tmp}"
WORKDIR="$(mktemp -d "${TMPDIR}/gecko-update.XXXXXX")"
cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT INT TERM

echo "Downloading ${ARCHIVE_URL}"
cd "${WORKDIR}"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "${ARCHIVE_URL}" -o repo.tar.gz
else
  wget -qO repo.tar.gz "${ARCHIVE_URL}"
fi

echo "Extracting..."
tar xzf repo.tar.gz

# GitHub names the top folder: {repo}-{branch-with-slashes-as-hyphens}
SRC="$(find . -maxdepth 1 -mindepth 1 -type d ! -name '.*' | head -n 1)"
if [ -z "${SRC}" ] || [ ! -d "${SRC}/custom_components/gecko" ]; then
  echo "ERROR: archive layout unexpected; missing custom_components/gecko under ${SRC:-?}" >&2
  ls -la >&2
  exit 1
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN: would replace ${DEST} with ${SRC}/custom_components/gecko"
  exit 0
fi

echo "Installing to ${DEST}"
mkdir -p "${CONFIG_DIR}/custom_components"
rm -rf "${DEST}"
cp -a "${SRC}/custom_components/gecko" "${DEST}"

echo "Done."
echo "In Home Assistant: Developer tools → YAML → Restart (quick) is optional;"
echo "usually **Settings → Devices & services → Gecko → ⋮ → Reload** is enough."
