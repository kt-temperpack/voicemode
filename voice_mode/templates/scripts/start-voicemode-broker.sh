#!/bin/bash
set -eu

VOICEMODE_DIR="${VOICEMODE_BASE_DIR:-$HOME/.voicemode}"

voicemode_load_env_file() {
    local file="$1" line key val
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [ -z "$line" ] && continue
        case "$line" in '#'*) continue ;; esac
        case "$line" in [A-Za-z_]*=*) : ;; *) continue ;; esac
        key="${line%%=*}"
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
        val="${line#*=}"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        export "$key=$val"
    done < "$file"
}

voicemode_load_env_file "$VOICEMODE_DIR/voicemode.env"
BROKER_REPO="${VOICEMODE_BROKER_REPO:-$HOME}"

exec voicemode broker run --repo "$BROKER_REPO" --no-terminal-keys
