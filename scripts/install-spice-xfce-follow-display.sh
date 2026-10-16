#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir="$HOME/.local/bin"
autostart_dir="$HOME/.config/autostart"
target="$target_dir/spice-xfce-follow-display"

mkdir -p "$target_dir" "$autostart_dir"
install -m 0755 "$source_dir/spice-xfce-follow-display.sh" "$target"
cat > "$autostart_dir/accessforge-spice-resize.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SPICE display follow
Comment=Apply client-requested display sizes in Xfce
Exec=$target
OnlyShowIn=XFCE;
Terminal=false
EOF

printf 'Installed %s and Xfce autostart entry. Log out and back in to enable it.\n' "$target"
