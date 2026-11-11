#!/usr/bin/env bash
# EasyBuilder Pro under Wine on Linux — to check that a project built by ebpro-cmtp really opens.
#
# What it needs (not in the repository):
#   EBPRO_DIR   the installed EasyBuilder Pro folder (the one with "EasyBuilder Pro.exe"); from the Weintek setup.exe
#               (Inno Setup): innoextract -d ebpro setup.exe  →  ebpro/app
#   an X display (DISPLAY, e.g. Xvfb :1), wine (32-bit programs: wineWowPackages on NixOS), xdotool, xwininfo, import
#   32-bit Mesa for the Qt part of EasyBuilder: MESA32 = the lib folder with libGLX_mesa.so and dri/, GLVND32 = the
#               lib folder with 32-bit libGL.so.1 (NixOS: nix-build '<nixpkgs>' -A pkgsi686Linux.mesa / .libglvnd)
#
# Commands:
#   setup            make the Wine prefix: no Mono / Gecko prompts, the VC++ 2015 runtime from the EasyBuilder folder
#   start [seconds]  start EasyBuilder and wait; closes «Project Recovery» and the start dialog
#   open FILE [sec]  open a project (Ctrl+O), wait up to sec (150): prints OPENED <window title> or FAILED <why>
#   shot FILE.png    a screenshot of the display
#   stop             close Wine
#
# Fonts: EasyBuilder asks to replace every font the PC lacks (Arial, GOST Type AU…). Put the TTF files into
# $WINEPREFIX/drive_c/windows/Fonts; without them `open` answers the dialog with «ignore».
# Proved with EasyBuilder Pro 6.10.02.421 and wine 10.0 on 11 Nov 2026.
set -u
: "${EBPRO_DIR:?set EBPRO_DIR to the folder with EasyBuilder Pro.exe}"
export DISPLAY="${DISPLAY:-:1}" WINEPREFIX="${WINEPREFIX:-$HOME/.wine-ebpro}" WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}" WINEDLLOVERRIDES="mscoree=;mshtml="
LOG="${EBPRO_LOG:-/tmp/ebpro_wine.log}"

windows() { xwininfo -root -tree | grep -i 'easybuilder pro.exe' | grep -v -E ' 1x1|has no name|111x1|115x1|830x489'; }
main_window() { xdotool search --name 'EasyBuilder Pro V6' | head -1; }
# click near the bottom right corner of the dialog whose title matches $1 (its Cancel button): dx, dy from that corner
corner_click() {
    geo="$(windows | grep -i "$1" | head -1 | sed -E 's/.* ([0-9]+)x([0-9]+)\+[-0-9]+\+[-0-9]+ +\+([-0-9]+)\+([-0-9]+).*/\1 \2 \3 \4/')"
    [ -n "$geo" ] || return 1
    set -- $geo "$2" "$3"
    xdotool mousemove $(($3 + $1 - $5)) $(($4 + $2 - $6)) click 1
}
recovery() { windows | grep -q 'Project Recovery' && { corner_click 'Project Recovery' 48 22; sleep 4; }; }

case "${1:-}" in
setup)
    mkdir -p "$WINEPREFIX"
    wineboot -i
    (cd "$EBPRO_DIR" && wine vc2015_redist_x86.exe /quiet /norestart)
    mkdir -p "$WINEPREFIX/drive_c/p"
    ;;
start)
    wineserver -k 2>/dev/null; sleep 2
    (
        [ -n "${GLVND32:-}" ] && export LD_LIBRARY_PATH="$GLVND32:${MESA32:-}:${LD_LIBRARY_PATH:-}"
        [ -n "${MESA32:-}" ] && export LIBGL_DRIVERS_PATH="$MESA32/dri" LIBGL_ALWAYS_SOFTWARE=1 __GLX_VENDOR_LIBRARY_NAME=mesa
        cd "$EBPRO_DIR" && nohup wine 'EasyBuilder Pro.exe' >"$LOG" 2>&1 </dev/null &
    )
    sleep "${2:-75}"
    recovery                             # «closed abnormally last time, recover?» — Cancel
    xdotool key Escape; sleep 2          # the start dialog
    windows
    ;;
open)
    f="${2:?project file}"; name="$(basename "$f")"
    mkdir -p "$WINEPREFIX/drive_c/p"; cp "$f" "$WINEPREFIX/drive_c/p/$name"
    recovery
    xdotool windowactivate "$(main_window)" 2>/dev/null; sleep 1
    xdotool key Escape; sleep 1; xdotool key Escape; sleep 1      # whatever the keys of the last run left open
    xdotool key ctrl+o; sleep 4
    xdotool type --delay 30 "C:\\p\\$name"; sleep 1; xdotool key Return
    deadline=$(( $(date +%s) + ${3:-150} ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 3
        recovery && continue
        list="$(windows)"
        if echo "$list" | grep -q -i 'Yaz. Tipini\|Font'; then      # «these fonts are not installed»: ignore them all
            # Cancel (the right-hand button at the bottom), then «ignore the missing font?» — Yes for each
            echo "$list" | grep -q -E '"EasyBuilder Pro": .* [0-9]+x[0-9]+\+1\+28' && { xdotool key Return; sleep 2; }
            corner_click 'Yaz. Tipini\|Font' 42 17; sleep 2
            for _k in 1 2 3 4 5 6 7 8 9 10 11 12; do
                windows | grep -q -E '"EasyBuilder Pro": .* [0-9]+x[0-9]+\+1\+28' || break
                xdotool key Return; sleep 2
            done
            continue
        fi
        if echo "$list" | grep -q -E '"EasyBuilder Pro": .* [0-9]+x[0-9]+\+1\+28'; then     # a message box
            [ -n "${EBPRO_SHOT:-}" ] && import -window root "$EBPRO_SHOT"
            xdotool key Return; sleep 3; xdotool key Return; sleep 2
            echo "FAILED $name: EasyBuilder showed a message instead of the project (corrupted file?)"; exit 1
        fi
        title="$(echo "$list" | grep -o '"EasyBuilder Pro V6[^"]*"' | head -1)"
        case "$title" in *": $name "*) sleep 3; windows | grep -q -i 'Yaz. Tipini\|Font' && continue
                                       echo "OPENED $title"; exit 0;; esac
    done
    echo "FAILED $name: not opened in time; on the screen: $(windows | grep -o '"[^"]*"' | head -3 | tr '\n' ' ')"; exit 1
    ;;
shot) import -window root "${2:?png}";;
stop) wineserver -k;;
*) sed -n '2,22p' "$0"; exit 2;;
esac
