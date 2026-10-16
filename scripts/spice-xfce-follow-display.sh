#!/bin/sh
# Follow SPICE/QXL's preferred mode in Xfce sessions that do not switch modes
# when the guest agent receives a monitor configuration request.

if [ -z "${DISPLAY:-}" ] || ! command -v xrandr >/dev/null 2>&1; then
    exit 0
fi

last_preferred=

while :; do
    # QXL publishes each client-requested size as the preferred (+) RandR
    # mode. Read the active size from the output header: some QXL versions
    # omit the usual (*) marker on the active mode line.
    modes=$(xrandr --query 2>/dev/null | awk '
        $1 ~ /^Virtual-[0-9]+$/ && $2 == "connected" && !output {
            output = $1
            for (i = 3; i <= NF; i++) {
                if ($i ~ /^[0-9]+x[0-9]+\+/) {
                    split($i, geometry, /\+/)
                    current = geometry[1]
                    break
                }
            }
            in_output = 1
            next
        }
        in_output && /^[^[:space:]]/ { exit }
        in_output && /\+/ { preferred = $1 }
        END {
            if (output != "" && preferred != "" && current != "")
                print output, preferred, current
        }
    ')

    if [ -n "$modes" ]; then
        set -- $modes
        if [ -n "$last_preferred" ] && [ "$2" != "$last_preferred" ] && [ "$2" != "$3" ]; then
            xrandr --output "$1" --auto >/dev/null 2>&1 || :
        fi
        last_preferred=$2
    fi

    sleep 1
done
