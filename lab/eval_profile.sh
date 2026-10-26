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
set -eu

UNIT=meshforge-controller.service
DIR=/etc/systemd/system/${UNIT}.d
ENV_FILE=$DIR/eval-profile.env
CONF=$DIR/eval-profile.conf

restart() {
    systemctl daemon-reload
    systemctl restart "$UNIT"
    systemctl is-active --quiet "$UNIT"
}

if [ "${1:-}" = "--clear" ]; then
    rm -f "$ENV_FILE" "$CONF"
    restart
    echo "evaluation profile cleared"
    exit 0
fi

check() {
    if [ ! -f "$1" ]; then
        echo "no such file: $1" >&2
        exit 2
    fi
    # only MESHFORGE_CONTROLLER__PLANNER__* assignments (and comments) may go in
    if grep -v -E "^(#.*|MESHFORGE_CONTROLLER__PLANNER__[A-Z_]+='[^']*')?$" "$1" >/dev/null; then
        echo "refusing $1: not an evaluation profile file" >&2
        exit 2
    fi
}

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <profile> <profile.env> | --clear | --check <profile.env>" >&2
    exit 2
fi
if [ "$1" = "--check" ]; then
    check "$2"
    echo "ok: $2"
    exit 0
fi
profile=$1
source_env=$2
case "$profile" in
    *[!a-z0-9_]*|"") echo "bad profile name: $profile" >&2; exit 2 ;;
esac
check "$source_env"
install -d -m 0755 "$DIR"
install -m 0644 "$source_env" "$ENV_FILE"
printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" > "$CONF"
chmod 0644 "$CONF"
restart
echo "evaluation profile $profile active"
