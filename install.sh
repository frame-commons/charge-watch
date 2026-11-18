#!/bin/sh
# SPDX-License-Identifier: MIT
# Install charge-watch for the current user: the script to ~/.local/bin, a systemd user unit, started now.
# Everything lives in your home directory, so it survives SteamOS updates. Uninstall: ./install.sh --remove
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin/charge-watch"
unit="$HOME/.config/systemd/user/charge-watch.service"
if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now charge-watch.service 2>/dev/null || true
    rm -f "$bin" "$unit"
    systemctl --user daemon-reload
    echo "charge-watch removed (its settings stay in ~/.config/charge-watch/, its log in ~/.local/state/charge-watch/)"
    exit 0
fi
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
install -m 755 "$here/charge_watch.py" "$bin"
install -m 644 "$here/systemd/charge-watch.service" "$unit"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/charge-watch/config.json"
if [ ! -e "$conf" ]; then
    mkdir -p "$(dirname "$conf")"
    install -m 600 "$here/config.example.json" "$conf"
    echo "settings: $conf (auto-shutdown and phone push are off; see the README to turn them on)"
fi
systemctl --user daemon-reload
systemctl --user enable --now charge-watch.service
systemctl --user restart charge-watch.service   # pick up a new script or settings on a re-install
echo "charge-watch installed and running. Check it:  $bin status   |   hear it:  $bin test-alert"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "(~/.local/bin is not on your PATH; use the full path above, or add it)";; esac
