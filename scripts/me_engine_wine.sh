#!/usr/bin/env bash
# The CODESYS engine of EcoStruxure Machine Expert 2.2 (3.5.19.40) under Wine, without the window — to check that a
# .project written by plc-kit is read by the program's own code. docs/opo/MACHINE_EXPERT.md, section 7.
#
# What it needs (not in the repository):
#   ME_SRC      the program folder of Machine Expert 2.2 — the one with LogicBuilder.exe and LogicBuilder/ (copied from a
#               PC where the program is installed). A working copy is made in ME; the original is not changed.
#   NixOS with nix-shell (wine, winetricks, Xvfb are taken from nixpkgs); about 5 GB of disk; the first setup
#   downloads the .NET Framework 4.8 (about 5 minutes).
#
#   me_engine_wine.sh setup                       one time: the copy, the Wine prefix with .NET 4.8, the host program
#   me_engine_wine.sh open [--dump] [--save OUT.project] FILE.project [FILE2 ...]
#                                                 load with the real ObjectManager / BinaryArchive: prints OPENED … +
#                                                 the object tree (+ texts with --dump) + SUMMARY, or LOAD FAILED …;
#                                                 exit 0 — every object was read; --save writes it again by the engine
#   me_engine_wine.sh script SCRIPT.py [args]     an IronPython script in the engine (projects.open / create / save …);
#                                                 paths are Windows paths: C:\p\ = $WINEPREFIX/drive_c/p, Z:\ = /
#
# The host program (scripts/me_engine/lbh.cs) starts the component model of CODESYS from the folder: the plug-in
# cache, the object manager, the binary archive, the object plug-ins, the script engine. The shell of Schneider is
# not started (it asks for the licence), so its own plug-ins are not loaded but for the OPC UA object
# (me_engine/include.txt); there are no libraries and no device repository — nothing is compiled.
# «Opened here» means: the container, the tables and every object are read by the real code. It does not mean that
# the window of Machine Expert opens the file without a remark — that is checked on a PC with the program.
#
# Environment: ME_SRC, ME (the copy, default $HOME/.me-engine/program), WINEPREFIX ($HOME/.me-engine/prefix),
#   H_VERBOSE=1, H_LIST=1 (list plug-ins), H_INCLUDE / H_EXCLUDE (plug-in GUIDs), LBH_TIMEOUT (seconds, 900).
# Proved with Machine Expert 2.2 (CODESYS 3.5.19.40), wine 10.0, .NET Framework 4.8 on 12 Nov 2026.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${IN_LBH_NIX:-}" ]; then
    q=""; for a in "$@"; do q="$q $(printf '%q' "$a")"; done
    exec env IN_LBH_NIX=1 NIXPKGS_ALLOW_UNFREE=1 nix-shell -p wineWowPackages.stable winetricks xorg.xorgserver cabextract wget \
        --run "$(printf '%q' "$HERE/me_engine_wine.sh")$q"
fi
export ME="${ME:-$HOME/.me-engine/program}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.me-engine/prefix}" WINEARCH=win64 DISPLAY="${DISPLAY_LBH:-:7}"
export WINEDEBUG="${WINEDEBUG:--all}"
# winedbg off: a crash must not hang the run.  security=n,b: Common/Security.dll (a .NET assembly) must win over Wine's builtin security.dll.
export WINEDLLOVERRIDES="winedbg.exe=d;security=n,b"
unset shell   # zsh exports both `shell` and SHELL; .NET's ProcessStartInfo.EnvironmentVariables throws on the duplicate key
pgrep -f "Xvfb $DISPLAY" >/dev/null || { nohup Xvfb "$DISPLAY" -screen 0 1280x1024x24 >/dev/null 2>&1 & sleep 1; }
C="$WINEPREFIX/drive_c"; COMMON="$ME/LogicBuilder/Common"; T="${LBH_TIMEOUT:-900}"

build() {
    refs=(); for r in ComponentModel SystemInstances Core EngineWin ObjectsWin Engine Objects MessageService MessageServiceWin OptionStorage OptionStorageWin MessageStorage; do
        refs+=("/r:C:\\ME\\LogicBuilder\\Common\\$r.dll"); done
    rm -f "$COMMON/lbh.exe"
    (cd "$HERE/me_engine" && wine 'C:\windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe' /nologo /platform:x64 \
        '/out:C:\ME\LogicBuilder\Common\lbh.exe' "${refs[@]}" /r:Microsoft.CSharp.dll lbh.cs) && [ -f "$COMMON/lbh.exe" ]
}
lbh() { (cd "$COMMON" && timeout "$T" wine lbh.exe "$@" 2>/dev/null); rc=$?; [ $rc = 124 ] && echo "TIMEOUT after ${T}s"; return $rc; }
# copy a file into C:\p under an ASCII name and print its Windows path
stage() { n="in$$_$2.${1##*.}"; cp -f "$1" "$C/p/$n" && echo "C:\\p\\$n"; }

case "${1:-}" in
setup)
    [ -d "$ME" ] || { : "${ME_SRC:?set ME_SRC to the folder with LogicBuilder.exe}"; mkdir -p "$(dirname "$ME")"; cp -a "$ME_SRC" "$ME"; }   # the original stays as it is
    mkdir -p "$WINEPREFIX"
    if [ ! -f "$C/windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe" ]; then
        WINEDLLOVERRIDES="mscoree=;mshtml=" wineboot -i
        winetricks -q dotnet48 || exit 1                                   # ~5 min, downloads ~120 MB; needs GNU wget (busybox wget fails)
        winecfg -v win10; wineserver -w
    fi
    mkdir -p "$C/p"; ln -sfn "$ME" "$C/ME"; ln -sfn "$HERE/me_engine" "$C/t"   # C:\ME = program copy, C:\t = harness files, C:\p = projects
    sed -i 's/<level value="WARN"/<level value="ALL"/' "$ME/LogicBuilderShell.exe.config" "$ME/LogicBuilder.exe.config"
    build || { echo "harness build failed"; exit 1; }
    echo "harness built; first run builds the plug-in cache (1-2 min)"; lbh; echo "setup done rc=$?"
    ;;
build) build ;;
open)
    shift; args=(); i=0; save=""
    while [ $# -gt 0 ]; do case "$1" in
        --dump) args+=(--dump) ;;
        --save) save="$2"; args+=(--save "C:\\p\\out$$.project"); shift ;;
        *) [ -f "$1" ] || { echo "LOAD FAILED no such file: $1"; exit 2; }; i=$((i+1)); echo "# in$$_$i = $1"; args+=("$(stage "$1" $i)") ;;
    esac; shift; done
    lbh "${args[@]}"; rc=$?
    [ -n "$save" ] && [ -f "$C/p/out$$.project" ] && mv "$C/p/out$$.project" "$save"
    rm -f "$C/p/in$$_"*; exit $rc
    ;;
script)
    shift; s="$(stage "$1" s)"; shift
    lbh --script "$s" "$@"; rc=$?; rm -f "$C/p/in$$_"*; exit $rc
    ;;
wine) shift; "$@" ;;
*) sed -n '2,27p' "$0" ;;
esac
