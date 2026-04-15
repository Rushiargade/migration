#!/usr/bin/env bash
# preflight_check.sh - Pre-migration environment check script
#
# Verifies that all required tools, Python version, packages, and connectivity
# are in place before running vmigrate.
#
# Usage:
#   bash preflight_check.sh [CONVERSION_HOST]
#
# Arguments:
#   CONVERSION_HOST  Optional. Hostname/IP of the Linux conversion host.
#                    SSH connectivity to this host is checked if provided.
#                    Defaults to the CONVERSION_HOST environment variable.
#
# Exit codes:
#   0 - All checks passed
#   1 - One or more checks failed

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

PASS="${GREEN}PASS${RESET}"
FAIL="${RED}FAIL${RESET}"
WARN="${YELLOW}WARN${RESET}"

FAILED=0

pass() { echo -e "  [${PASS}] $*"; }
fail() { echo -e "  [${FAIL}] $*"; FAILED=1; }
warn() { echo -e "  [${WARN}] $*"; }
header() { echo -e "\n${BOLD}== $* ==${RESET}"; }

# ---------------------------------------------------------------------------
# Arguments / environment
# ---------------------------------------------------------------------------
CONVERSION_HOST="${1:-${CONVERSION_HOST:-}}"

# ---------------------------------------------------------------------------
# Check: qemu-img
# ---------------------------------------------------------------------------
header "Disk Conversion Tools"

if command -v qemu-img &>/dev/null; then
    QEMU_VERSION=$(qemu-img --version | head -1)
    pass "qemu-img found: ${QEMU_VERSION}"
else
    fail "qemu-img not found. Install with:
         Debian/Ubuntu: apt install qemu-utils
         RHEL/Fedora:   dnf install qemu-img"
fi

# ---------------------------------------------------------------------------
# Check: virt-v2v (optional but required for Windows VMs)
# ---------------------------------------------------------------------------
if command -v virt-v2v &>/dev/null; then
    V2V_VERSION=$(virt-v2v --version 2>&1 | head -1)
    pass "virt-v2v found: ${V2V_VERSION}"
else
    warn "virt-v2v not found (required for Windows VM migrations). Install with:
         RHEL/Fedora:   dnf install virt-v2v
         Debian/Ubuntu: apt install virt-v2v"
fi

# ---------------------------------------------------------------------------
# Check: Python 3.10+
# ---------------------------------------------------------------------------
header "Python Environment"

PYTHON_BIN=""
for py in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$py" &>/dev/null; then
        PYTHON_BIN="$py"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    fail "Python 3 not found. Install Python 3.10 or later."
else
    PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
    PYTHON_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)")
    PYTHON_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)")

    if [[ "$PYTHON_MAJOR" -ge 3 && "$PYTHON_MINOR" -ge 10 ]]; then
        pass "Python found: ${PYTHON_VERSION} (${PYTHON_BIN})"
    else
        fail "Python 3.10+ required, found: ${PYTHON_VERSION}. Upgrade Python."
    fi
fi

# ---------------------------------------------------------------------------
# Check: Required Python packages
# ---------------------------------------------------------------------------
if [[ -n "$PYTHON_BIN" ]]; then
    header "Python Packages"

    REQUIRED_PACKAGES=(
        "pyVmomi"
        "proxmoxer"
        "requests"
        "paramiko"
        "yaml"
        "click"
        "rich"
        "winrm"
    )

    for pkg in "${REQUIRED_PACKAGES[@]}"; do
        # Use the import name (yaml is imported as 'yaml', not 'pyyaml')
        if "$PYTHON_BIN" -c "import ${pkg}" &>/dev/null 2>&1; then
            pass "Python package '${pkg}' is installed"
        else
            fail "Python package '${pkg}' is not installed. Run: pip install vmigrate"
        fi
    done

    # Check vmigrate itself
    if "$PYTHON_BIN" -c "import vmigrate" &>/dev/null 2>&1; then
        VMIGRATE_VER=$("$PYTHON_BIN" -c "import vmigrate; print(vmigrate.__version__)")
        pass "vmigrate package installed (version ${VMIGRATE_VER})"
    else
        fail "vmigrate package not installed. Run: pip install -e ."
    fi
fi

# ---------------------------------------------------------------------------
# Check: SSH client
# ---------------------------------------------------------------------------
header "SSH Tools"

if command -v ssh &>/dev/null; then
    SSH_VERSION=$(ssh -V 2>&1 | head -1)
    pass "SSH client found: ${SSH_VERSION}"
else
    fail "SSH client (ssh) not found. Install openssh-client."
fi

# ---------------------------------------------------------------------------
# Check: Conversion host connectivity
# ---------------------------------------------------------------------------
if [[ -n "$CONVERSION_HOST" ]]; then
    header "Conversion Host Connectivity (${CONVERSION_HOST})"

    # TCP ping on port 22
    if timeout 5 bash -c "cat < /dev/null > /dev/tcp/${CONVERSION_HOST}/22" &>/dev/null 2>&1; then
        pass "TCP port 22 reachable on ${CONVERSION_HOST}"
    else
        fail "Cannot reach ${CONVERSION_HOST}:22. Check firewall rules and hostname."
    fi

    # SSH connectivity (key-based, no password prompt)
    if ssh -o StrictHostKeyChecking=no \
            -o BatchMode=yes \
            -o ConnectTimeout=5 \
            "${CONVERSION_HOST}" \
            "echo ok" &>/dev/null 2>&1; then
        pass "SSH key-based auth working to ${CONVERSION_HOST}"

        # Check qemu-img on conversion host
        if ssh -o StrictHostKeyChecking=no \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                "${CONVERSION_HOST}" \
                "command -v qemu-img" &>/dev/null 2>&1; then
            REMOTE_QEMU=$(ssh -o StrictHostKeyChecking=no \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                "${CONVERSION_HOST}" \
                "qemu-img --version | head -1" 2>/dev/null)
            pass "qemu-img on ${CONVERSION_HOST}: ${REMOTE_QEMU}"
        else
            fail "qemu-img not found on ${CONVERSION_HOST}. Install qemu-utils."
        fi

        # Check virt-v2v on conversion host
        if ssh -o StrictHostKeyChecking=no \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                "${CONVERSION_HOST}" \
                "command -v virt-v2v" &>/dev/null 2>&1; then
            REMOTE_V2V=$(ssh -o StrictHostKeyChecking=no \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                "${CONVERSION_HOST}" \
                "virt-v2v --version 2>&1 | head -1" 2>/dev/null)
            pass "virt-v2v on ${CONVERSION_HOST}: ${REMOTE_V2V}"
        else
            warn "virt-v2v not found on ${CONVERSION_HOST} (required for Windows VMs)."
        fi

        # Check available disk space on conversion host
        REMOTE_FREE=$(ssh -o StrictHostKeyChecking=no \
            -o BatchMode=yes \
            -o ConnectTimeout=5 \
            "${CONVERSION_HOST}" \
            "df -BG /tmp | tail -1 | awk '{print \$4}'" 2>/dev/null | tr -d 'G')
        if [[ -n "$REMOTE_FREE" ]]; then
            if [[ "$REMOTE_FREE" -ge 50 ]]; then
                pass "Free disk space on ${CONVERSION_HOST} /tmp: ${REMOTE_FREE}GB"
            else
                warn "Low disk space on ${CONVERSION_HOST} /tmp: ${REMOTE_FREE}GB " \
                     "(recommend >= 50GB for disk images)"
            fi
        fi
    else
        fail "SSH key-based auth failed to ${CONVERSION_HOST}. " \
             "Ensure your public key is in ~/.ssh/authorized_keys on the remote host."
    fi
else
    echo -e "\n  ${YELLOW}Skipping conversion host check (no CONVERSION_HOST specified).${RESET}"
    echo    "  Usage: $0 <conversion_host>  or  CONVERSION_HOST=host $0"
fi

# ---------------------------------------------------------------------------
# Check: Local disk space
# ---------------------------------------------------------------------------
header "Local Resources"

LOCAL_FREE=$(df -BG /tmp 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
if [[ -n "$LOCAL_FREE" ]]; then
    if [[ "$LOCAL_FREE" -ge 50 ]]; then
        pass "Free disk space on /tmp: ${LOCAL_FREE}GB"
    else
        warn "Low disk space on /tmp: ${LOCAL_FREE}GB (recommend >= 50GB for disk images)"
    fi
fi

# Check available memory
TOTAL_MEM_GB=$(awk '/MemTotal/ { printf "%.0f\n", $2/1024/1024 }' /proc/meminfo 2>/dev/null || echo 0)
if [[ "$TOTAL_MEM_GB" -ge 4 ]]; then
    pass "System memory: ${TOTAL_MEM_GB}GB"
else
    warn "System memory: ${TOTAL_MEM_GB}GB (recommend >= 4GB for conversion)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}================================${RESET}"
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}All preflight checks PASSED.${RESET}"
    echo "You can now run: vmigrate migrate --all"
    exit 0
else
    echo -e "${RED}${BOLD}One or more preflight checks FAILED.${RESET}"
    echo "Fix the issues listed above before running vmigrate."
    exit 1
fi
