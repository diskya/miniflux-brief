#!/bin/sh
# Fail-closed host rules for the two fixed reader addresses in docker-compose.yml.
# Default is a read-only check. Changes require an explicit --apply/--remove.
set -eu

COMMENT="miniflux-brief-reader-isolation"
READER_API_IP="172.30.40.2/32"
READER_EGRESS_IP="172.30.40.19/32"
PROXY_IP="172.30.40.18/32"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Run as root (for example: sudo $0 ${1:---check})." >&2
    exit 1
fi

has_rule() {
    iptables -C "$@" >/dev/null 2>&1
}

add_rule() {
    chain=$1
    shift
    has_rule "$chain" "$@" || iptables -I "$chain" 1 "$@"
}

remove_rule() {
    chain=$1
    shift
    if has_rule "$chain" "$@"; then
        iptables -D "$chain" "$@"
    fi
}

input_api="-s $READER_API_IP -m comment --comment $COMMENT -j REJECT"
input_egress="-s $READER_EGRESS_IP -m comment --comment $COMMENT -j REJECT"
forward_proxy="-s $READER_EGRESS_IP -d $PROXY_IP -m comment --comment $COMMENT -j ACCEPT"
forward_api="-s $READER_API_IP -m comment --comment $COMMENT -j REJECT"
forward_egress="-s $READER_EGRESS_IP -m comment --comment $COMMENT -j REJECT"

# Intentional word splitting turns these fixed strings into iptables arguments.
# shellcheck disable=SC2086
case "${1:---check}" in
    --apply)
        add_rule INPUT $input_api
        add_rule INPUT $input_egress
        add_rule DOCKER-USER $forward_egress
        add_rule DOCKER-USER $forward_api
        add_rule DOCKER-USER $forward_proxy
        echo "Reader firewall rules installed."
        ;;
    --remove)
        remove_rule INPUT $input_api
        remove_rule INPUT $input_egress
        remove_rule DOCKER-USER $forward_proxy
        remove_rule DOCKER-USER $forward_api
        remove_rule DOCKER-USER $forward_egress
        echo "Reader firewall rules removed."
        ;;
    --check)
        missing=0
        for specification in \
            "INPUT|$input_api" \
            "INPUT|$input_egress" \
            "DOCKER-USER|$forward_proxy" \
            "DOCKER-USER|$forward_api" \
            "DOCKER-USER|$forward_egress"
        do
            chain=${specification%%|*}
            rule=${specification#*|}
            # shellcheck disable=SC2086
            if ! has_rule "$chain" $rule; then
                echo "missing: $chain $rule"
                missing=1
            fi
        done
        exit "$missing"
        ;;
    --dry-run)
        echo "Would reject reader traffic to the host and all forwarding except 172.30.40.19 -> 172.30.40.18."
        ;;
    *)
        echo "Usage: $0 [--check|--dry-run|--apply|--remove]" >&2
        exit 2
        ;;
esac
