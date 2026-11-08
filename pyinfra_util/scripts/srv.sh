#!/bin/sh
# Wrapper around this account's systemd --user service(s).
# Usage: ./srv.sh status|start|restart|logs [unit] [journalctl args...]
# With one *.service unit installed, [unit] can be omitted; with more than
# one, pass its name as the second argument.
set -e

action="$1"
shift || true

unit_dir="$HOME/.config/systemd/user"
units=$(find "$unit_dir" -maxdepth 1 -name '*.service' 2>/dev/null)
count=$(printf '%s\n' "$units" | grep -c .)

case "$1" in
    ""|-*) ;;
    *)
        unit="$1"
        shift
        ;;
esac

if [ -z "$unit" ]; then
    if [ "$count" -ne 1 ]; then
        echo "more than one *.service unit in $unit_dir, pass one as the second argument:" >&2
        printf '%s\n' "$units" >&2
        exit 1
    fi
    unit=$(basename "$units")
fi

case "$action" in
    status|start|restart|stop|reload)
        exec systemctl --user "$action" "$unit"
        ;;
    logs)
        exec journalctl --user -u "$unit" "$@"
        ;;
    *)
        echo "usage: $0 status|start|restart|logs [unit] [journalctl args...]" >&2
        exit 1
        ;;
esac
