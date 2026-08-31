#!/usr/bin/env bash
# Build the offline Windows deploy package for the SECS/GEM EAP middleware.
#
# Output: deploy_out/astar-middleware-deploy-YYYY-MM-DD-HHMMSS.zip
#
# Run from the repo root. Needs:
#   - python3 with pip (any 3.10+)
#   - zip and unzip (macOS provides both)
#
# The wheels are built for whatever Python is installed on the Windows server.
# Default: auto-detect from the python3 running this script.
# Override: PY_VERSION=3.13 ./scripts/build_deploy_package.sh
#
# The bundled PYTHON_VERSION.txt is read by install.ps1 so the version check
# is always in sync with what was actually downloaded - no hardcoded versions.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_STAMP=$(date +%Y-%m-%d-%H%M%S)
PKG_NAME="astar-middleware-deploy-${BUILD_STAMP}"
OUT_ROOT="${REPO_DIR}/deploy_out"
STAGE_DIR="${OUT_ROOT}/${PKG_NAME}"

# The public production template is a reviewed release artifact. Never package
# a working-tree variant that could contain an operator's live configuration.
if ! git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "    [ERROR] Build must run from a Git checkout so release inputs can be verified."
    exit 1
fi
if ! git -C "${REPO_DIR}" diff --quiet HEAD -- config/production.yaml; then
    echo "    [ERROR] config/production.yaml differs from the reviewed HEAD revision."
    echo "            Commit the sanitized template or restore it before packaging."
    exit 1
fi

# Auto-detect from running python3 unless explicitly overridden.
PY_VERSION_OVERRIDDEN=0
if [[ -n "${PY_VERSION+x}" ]]; then
    TARGET_PY_VERSION="${PY_VERSION}"
    PY_VERSION_OVERRIDDEN=1
else
    TARGET_PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
fi
TARGET_PLATFORM="win_amd64"

step() { echo -e "\033[1;36m==>\033[0m $*"; }
ok()   { echo -e "    \033[1;32m[OK]\033[0m $*"; }

step "Wiping previous stage: ${STAGE_DIR}"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/source" "${STAGE_DIR}/wheels"

step "Copying middleware source"
cp -R "${REPO_DIR}/eap_middleware"      "${STAGE_DIR}/source/"
cp -R "${REPO_DIR}/gateway"             "${STAGE_DIR}/source/"
# The GUI runs on the interpreter install.ps1 installs; no separate frozen exe.
cp -R "${REPO_DIR}/gui"                 "${STAGE_DIR}/source/"
# The simulator and its panel travel in the ZIP but are NOT installed by
# default: install.ps1 copies them only under -Role Simulator or -Role Both.
# The separation that matters is what lands on an EAP host, and that is
# enforced at install time; carrying one ZIP to both machines of an
# installation is what makes setup a single drag-and-drop.
cp -R "${REPO_DIR}/simulator"           "${STAGE_DIR}/source/"
cp -R "${REPO_DIR}/simulator_gui"       "${STAGE_DIR}/source/"
cp -R "${REPO_DIR}/scripts"             "${STAGE_DIR}/source/"
cp -R "${REPO_DIR}/config"              "${STAGE_DIR}/source/"
# Never package an operator's local production configuration.
find "${STAGE_DIR}/source/config" -name '*.local.yaml' -type f -delete
# Only this generated machine-profile data is required at runtime. Document
# renders and visual-QA screenshots under output/ do not belong in deployment.
mkdir -p "${STAGE_DIR}/source/output"
for profile in davinci200_mc4_hc1 nexgen_mg_series spts_fxp_omega; do
    [[ -d "${REPO_DIR}/output/${profile}" ]] && cp -R "${REPO_DIR}/output/${profile}" "${STAGE_DIR}/source/output/"
done
cp -R "${REPO_DIR}/docs"                "${STAGE_DIR}/source/"
cp "${REPO_DIR}/pyproject.toml"         "${STAGE_DIR}/source/"
cp "${REPO_DIR}/requirements.txt"       "${STAGE_DIR}/source/"
cp "${REPO_DIR}/requirements-release.lock" "${STAGE_DIR}/source/"
cp "${REPO_DIR}/README.md"              "${STAGE_DIR}/source/"
# strip __pycache__ and macOS cruft
find "${STAGE_DIR}/source" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${STAGE_DIR}/source" -name '.DS_Store' -type f -delete
ok "Source staged"

step "Copying deploy assets (SETUP.bat, Setup.ps1, install.ps1, docs)"
# SETUP.bat is the only file anyone should have to double-click. install.ps1
# stays a first-class entry point for unattended and scripted installs.
cp "${REPO_DIR}/deploy/SETUP.bat"          "${STAGE_DIR}/SETUP.bat"
cp "${REPO_DIR}/deploy/Setup.ps1"          "${STAGE_DIR}/Setup.ps1"
cp "${REPO_DIR}/deploy/install.ps1"        "${STAGE_DIR}/install.ps1"
cp "${REPO_DIR}/deploy/upgrade.ps1"        "${STAGE_DIR}/upgrade.ps1"
cp "${REPO_DIR}/deploy/README_DEPLOY.txt"  "${STAGE_DIR}/README_DEPLOY.txt"
cp "${REPO_DIR}/deploy/SETUP_CHECKLIST.txt" "${STAGE_DIR}/SETUP_CHECKLIST.txt"
# Ship the Windows 11 quickstart at the package root so the operator sees it
# immediately on extraction (also kept under source/docs for reference).
cp "${REPO_DIR}/docs/QUICKSTART_WIN11.md"  "${STAGE_DIR}/QUICKSTART.md"
ok "Deploy assets staged (SETUP.bat + install.ps1 + QUICKSTART.md)"

# Wheels + bundled Python. By default REUSE the pre-vetted, version-matched
# wheels and offline Python installer already in deploy/ (truly offline, no
# network, guaranteed to match the bundled interpreter). Set REBUILD_WHEELS=1
# to instead download fresh win_amd64 wheels for the running python3.
BUNDLED_WHEELS="${REPO_DIR}/deploy/wheels"
BUNDLED_PY_DIR="${REPO_DIR}/deploy/python"
BUNDLED_PY_VER_FILE="${REPO_DIR}/deploy/PYTHON_VERSION.txt"
REUSE_BUNDLED_ARTIFACTS=0

validate_target_python_version() {
    if [[ ! "${TARGET_PY_VERSION}" =~ ^[0-9]+\.[0-9]+$ ]]; then
        echo "    [ERROR] Python target version must be major.minor; got '${TARGET_PY_VERSION}'."
        exit 1
    fi
}

validate_target_python_version

if [[ "${REBUILD_WHEELS:-0}" != "1" && -d "${BUNDLED_WHEELS}" ]]; then
    REUSE_BUNDLED_ARTIFACTS=1
    # Reuse is safe only when the vetted artifacts declare their interpreter.
    if [[ ! -f "${BUNDLED_PY_VER_FILE}" ]]; then
        echo "    [ERROR] Reusing offline wheels requires ${BUNDLED_PY_VER_FILE}."
        exit 1
    fi
    BUNDLED_TARGET_PY_VERSION="$(tr -d '[:space:]' < "${BUNDLED_PY_VER_FILE}")"
    if [[ ! "${BUNDLED_TARGET_PY_VERSION}" =~ ^[0-9]+\.[0-9]+$ ]]; then
        echo "    [ERROR] Bundled Python version must be major.minor; got '${BUNDLED_TARGET_PY_VERSION}'."
        exit 1
    fi
    if (( PY_VERSION_OVERRIDDEN == 1 )) && [[ "${TARGET_PY_VERSION}" != "${BUNDLED_TARGET_PY_VERSION}" ]]; then
        echo "    [ERROR] PY_VERSION=${TARGET_PY_VERSION} does not match bundled Python ${BUNDLED_TARGET_PY_VERSION}."
        echo "            Set REBUILD_WHEELS=1 to build artifacts for the requested version."
        exit 1
    fi
    TARGET_PY_VERSION="${BUNDLED_TARGET_PY_VERSION}"
    step "Reusing pre-vetted offline wheels (Python ${TARGET_PY_VERSION}, no download)"
    cp "${BUNDLED_WHEELS}"/*.whl "${STAGE_DIR}/wheels/"
    ok "$(ls "${STAGE_DIR}/wheels" | wc -l | tr -d ' ') wheels copied"
else
    step "Downloading Windows ${TARGET_PLATFORM} wheels for Python ${TARGET_PY_VERSION}"
    # --only-binary=:all: refuses sdists so the install on Windows won't try to
    # compile anything. If pip can't find a wheel for a dep, the build fails
    # loudly here and we know to upgrade or substitute.
    python3 -m pip download \
        --requirement "${REPO_DIR}/requirements-release.lock" \
        --require-hashes \
        --dest "${STAGE_DIR}/wheels" \
        --platform "${TARGET_PLATFORM}" \
        --python-version "${TARGET_PY_VERSION}" \
        --implementation cp \
        --only-binary=:all: \
        --no-cache-dir
    ok "$(ls "${STAGE_DIR}/wheels" | wc -l | tr -d ' ') wheels downloaded"
fi

step "Validating the complete offline wheel set"
python3 -m pip download \
    --requirement "${REPO_DIR}/requirements-release.lock" \
    --require-hashes \
    --dest "${STAGE_DIR}/wheels" \
    --no-index \
    --find-links "${STAGE_DIR}/wheels" \
    --platform "${TARGET_PLATFORM}" \
    --python-version "${TARGET_PY_VERSION}" \
    --implementation cp \
    --only-binary=:all: \
    --no-cache-dir
ok "Offline wheels satisfy the hashed Python ${TARGET_PY_VERSION} release lock"

# Version stamp read by install.ps1 - keeps installer and wheels in sync.
echo "${TARGET_PY_VERSION}" > "${STAGE_DIR}/PYTHON_VERSION.txt"

# Bundle only an offline Python installer matching the wheel target. A rebuild
# for another Python version may intentionally produce a package without one.
shopt -s nullglob
MATCHING_PY_INSTALLERS=("${BUNDLED_PY_DIR}"/python-"${TARGET_PY_VERSION}".*-amd64.exe)
shopt -u nullglob
if (( ${#MATCHING_PY_INSTALLERS[@]} > 1 )); then
    echo "    [ERROR] Multiple Python ${TARGET_PY_VERSION} installers found in ${BUNDLED_PY_DIR}."
    exit 1
elif (( ${#MATCHING_PY_INSTALLERS[@]} == 1 )); then
    mkdir -p "${STAGE_DIR}/python"
    cp "${MATCHING_PY_INSTALLERS[0]}" "${STAGE_DIR}/python/"
    ok "Bundled offline Python installer: $(ls "${STAGE_DIR}/python")"
elif (( REUSE_BUNDLED_ARTIFACTS == 1 )); then
    echo "    [ERROR] Reused wheels target Python ${TARGET_PY_VERSION}, but no matching"
    echo "            python-${TARGET_PY_VERSION}.x-amd64.exe exists in ${BUNDLED_PY_DIR}."
    exit 1
else
    echo "    [WARN] No Python ${TARGET_PY_VERSION} offline installer in ${BUNDLED_PY_DIR} - the"
    echo "           target machine must already have Python ${TARGET_PY_VERSION}."
fi

step "Writing release hash manifest"
(
    cd "${STAGE_DIR}"
    # Everything here is executed on the target machine, so everything here
    # is hashed. SETUP.bat and Setup.ps1 run before install.ps1 verifies this
    # manifest, so their entries back the ZIP-level hash rather than gating
    # their own execution - the release record is what an operator checks.
    #
    # wheels/ and source/ are included in full: install.ps1 pip-installs
    # straight from wheels/ with no per-package check of its own, and copies
    # source/ verbatim into the running app, so a corrupted or substituted
    # file in an offline USB/network-share copy must be caught here, before
    # anything installs, not just at the three-file bootstrap layer.
    MANIFEST_FILES=("SETUP.bat" "Setup.ps1" "install.ps1" "upgrade.ps1" "PYTHON_VERSION.txt")
    if [[ -d "python" ]]; then
        shopt -s nullglob
        PYTHON_MANIFEST_FILES=(python/python-*.exe)
        shopt -u nullglob
        MANIFEST_FILES+=("${PYTHON_MANIFEST_FILES[@]}")
    fi
    if [[ -d "wheels" ]]; then
        while IFS= read -r -d '' f; do
            MANIFEST_FILES+=("${f#./}")
        done < <(find wheels -type f -print0)
    fi
    if [[ -d "source" ]]; then
        while IFS= read -r -d '' f; do
            MANIFEST_FILES+=("${f#./}")
        done < <(find source -type f -print0)
    fi
    shasum -a 256 "${MANIFEST_FILES[@]}" > RELEASE_MANIFEST.sha256
)
ok "Release manifest covers installer inputs, wheels and source"

step "Producing ZIP"
cd "${OUT_ROOT}"
ZIP_PATH="${OUT_ROOT}/${PKG_NAME}.zip"
rm -f "${ZIP_PATH}"
zip -qr "${ZIP_PATH}" "${PKG_NAME}"
(
    cd "${OUT_ROOT}"
    shasum -a 256 "${PKG_NAME}.zip" > "${PKG_NAME}.zip.sha256"
)
ok "Wrote ${ZIP_PATH}"

step "Package summary"
echo "    Path:  ${ZIP_PATH}"
echo "    Hash:  ${ZIP_PATH}.sha256"
echo "    Size:  $(du -h "${ZIP_PATH}" | cut -f1)"
echo "    Contents:"
unzip -l "${ZIP_PATH}" | tail -20

echo
echo -e "\033[1;32mDone.\033[0m ${ZIP_PATH}"
echo "Follow README_DEPLOY.txt inside the extracted package on the Windows server."
