#!/bin/sh
# Prepares the data directory before the containers of the stack start.
#
# Docker creates missing bind-mount sources as root, and the DocuShelf
# container (UID 1000) could not write into them. This one-shot job runs as
# root in a throwaway busybox container. It
#   * creates the top-level directories and the drop zones that the Samba and
#     FTP containers mount,
#   * hands those directories over to the DocuShelf user - but only when they
#     belong to root, i.e. were just created by Docker,
#   * gives the SMB drop zone its mode (default 1733: the scanner account may
#     create files there, but cannot list or read the directory; DocuShelf owns
#     it and has full access).
# It never touches files and never changes anything inside the archive.
#
# Environment: DOCUSHELF_UID, DOCUSHELF_GID (default 1000), SCAN_DIR_MODE
# (default 1733), DATA_ROOT (default /data).
set -eu

uid="${DOCUSHELF_UID:-1000}"
gid="${DOCUSHELF_GID:-1000}"
scan_mode="${SCAN_DIR_MODE:-1733}"
root="${DATA_ROOT:-/data}"

case "$uid" in '' | *[!0-9]*) echo "init-data: DOCUSHELF_UID muss eine Zahl sein (ist: '$uid')" >&2; exit 1 ;; esac
case "$gid" in '' | *[!0-9]*) echo "init-data: DOCUSHELF_GID muss eine Zahl sein (ist: '$gid')" >&2; exit 1 ;; esac
case "$scan_mode" in '' | *[!0-7]*) echo "init-data: SCAN_DIR_MODE muss ein Oktalwert sein, z. B. 1733 (ist: '$scan_mode')" >&2; exit 1 ;; esac

if [ ! -d "$root" ]; then
    echo "init-data: $root ist kein Verzeichnis - ist das Datenvolume eingebunden?" >&2
    exit 1
fi

for dir in "$root" "$root/archiv" "$root/eingang" "$root/eingang/quellen" \
    "$root/eingang/quellen/smb" "$root/eingang/quellen/ftp"; do
    if [ -L "$dir" ]; then
        echo "init-data: $dir ist ein symbolischer Link - wird nicht verändert"
        continue
    fi
    mkdir -p "$dir"
    owner="$(stat -c %u "$dir")"
    if [ "$owner" = "0" ] && [ "$uid" != "0" ]; then
        chown "$uid:$gid" "$dir"
        echo "init-data: $dir gehört jetzt $uid:$gid"
    fi
done

if [ ! -L "$root/eingang/quellen/smb" ]; then
    chmod "$scan_mode" "$root/eingang/quellen/smb"
fi
echo "init-data: Datenverzeichnis bereit (SMB-Eingang mit Modus $scan_mode)"
