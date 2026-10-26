#!/bin/sh
# V1.0 G2 (D418): the evaluation campaign's --profile-hook on the installed
# Linux controller (meshforge-controller .deb). Run as root on the controller
# laptop; scripts/eval/run_campaign.py calls it between blocks:
#
#   run_campaign.py ... --profile-hook "sudo lab/eval_profile.sh {profile} {env}"
#   sudo lab/eval_profile.sh --clear        # after the campaign: back to normal
#   lab/eval_profile.sh --check <file.env>  # validate a profile file only
#
# It installs the profile's environment file (written by the runner to
# <raw>/profiles/<profile>.env) as a systemd drop-in of
# meshforge-controller.service and restarts the service. Environment
# variables override /etc/meshforge/controller.toml, which is never edited.
# Exit 0 only when the service is active again (the runner then waits for
# every node to be ONLINE).
#
# The source file is owned by the (unprivileged) campaign user, so it is
# copied first into a root-owned temporary file in the drop-in directory and
# the COPY is validated and moved into place: changing the source after the
# check cannot change what is installed.
set -eu

UNIT=meshforge-controller.service
DIR=/etc/systemd/system/${UNIT}.d
ENV_FILE=$DIR/eval-profile.env
CONF=$DIR/eval-profile.conf

# The only keys evaluation profiles set: scripts/eval/evalkit/arms.py
# (PROFILES -> profile_env); a unit test keeps the two in step.
KEYS='CALIBRATION|DISABLED_MODES|ML'

usage() {
    echo "usage: $0 <profile> <profile.env> | --clear | --check <profile.env>" >&2
    exit 2
}

restart() {
    systemctl daemon-reload
    systemctl restart "$UNIT"
    systemctl is-active --quiet "$UNIT"
}

# check <file> <name shown in messages>
check() {
    if [ ! -f "$1" ]; then
        echo "no such file: $2" >&2
        exit 2
    fi
    # only the profile's planner assignments, single-quoted, and comments; a
    # comment ending in a backslash would continue onto the next line in
    # systemd, so it is refused too. Only the line number is reported (the
    # file is read as root; its content is never echoed).
    bad=$(grep -n -v -E "^(#|#.*[^\\]|MESHFORGE_CONTROLLER__PLANNER__($KEYS)='[^']*')?\$" -- "$1" | head -n 1 | cut -d: -f1) || true
    if [ -n "$bad" ]; then
        echo "refusing $2: not an evaluation profile file (line $bad)" >&2
        exit 2
    fi
}

if [ "${1:-}" = "--clear" ]; then
    [ "$#" -eq 1 ] || usage
    rm -f -- "$ENV_FILE" "$CONF"
    restart
    echo "evaluation profile cleared"
    exit 0
fi

[ "$#" -eq 2 ] || usage
case "$2" in
    -*) echo "refusing file name starting with '-': $2" >&2; exit 2 ;;
esac
if [ "$1" = "--check" ]; then
    check "$2" "$2"
    echo "ok: $2"
    exit 0
fi
profile=$1
source_env=$2
case "$profile" in
    *[!a-z0-9_]*|"") echo "bad profile name: $profile" >&2; exit 2 ;;
esac
# early refusal before anything is touched (the authoritative check is on the copy)
check "$source_env" "$source_env"
install -d -m 0755 "$DIR"
tmp=$(mktemp "$DIR/.eval-profile.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
# size cap: a source swapped for /dev/zero or a huge file cannot fill the root fs
head -c 65536 -- "$source_env" > "$tmp"
check "$tmp" "$source_env"
chmod 0644 "$tmp"
mv -f -- "$tmp" "$ENV_FILE"
printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" > "$CONF"
chmod 0644 "$CONF"
restart
echo "evaluation profile $profile active"
