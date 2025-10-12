#!/usr/bin/env bash
# switch_mic_aec.sh
# Auto-switch AEC (module-echo-cancel) to a new microphone, update ~/.config/pulse/default.pa,
# and restart PipeWire/Pulse user services.
#
# Usage examples:
#   ./switch_mic_aec.sh --pattern "G22"                 # pick first alsa_input matching pattern
#   ./switch_mic_aec.sh --device alsa_input.usb-XXX...  # specify full pactl source name
#   ./switch_mic_aec.sh --sink-device alsa_output.usb-YYY...  # (optional) also change sink
#   ./switch_mic_aec.sh --dry-run                       # show what would change
#
# Requirements: bash, pactl, systemctl (user), PipeWire with pipewire-pulse

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'

usage() {
  cat <<USAGE
switch_mic_aec.sh - Update WebRTC AEC to a new mic and restart services.

Options:
  -d, --device NAME        Full pactl source name to use (e.g., alsa_input.usb-XXX-00.analog-mono)
  -p, --pattern REGEX      Pick first alsa_input matching REGEX (ignored if --device given)
  -s, --sink-device NAME   Full pactl sink name to use (default: current sink_master from default.pa, or first alsa_output)
      --dry-run            Do not modify files or restart; just print planned actions
  -h, --help               Show this help

Examples:
  ./switch_mic_aec.sh --pattern "G22"
  ./switch_mic_aec.sh --device alsa_input.usb-G22_SF-560_20180508-00.analog-stereo
USAGE
}

MIC=""
SINK=""
PATTERN=""
DRYRUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--device) MIC="${2:-}"; shift 2;;
    -p|--pattern) PATTERN="${2:-}"; shift 2;;
    -s|--sink-device) SINK="${2:-}"; shift 2;;
    --dry-run) DRYRUN=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo -e "${RED}Unknown option:${NC} $1"; usage; exit 1;;
  esac
done

DEFAULT_PA="${HOME}/.config/pulse/default.pa"
mkdir -p "$(dirname "$DEFAULT_PA")"

timestamp() { date +%Y-%m-%dT%H:%M:%S%z; }

get_current_sink_from_default() {
  [[ -f "$DEFAULT_PA" ]] || return 0
  awk '
    $0 ~ /^load-module[ \t]+module-echo-cancel\b/ { inblk=1; next }
    inblk && $0 ~ /sink_master=/ {
      if (match($0, /sink_master=([^ \t\\]+)/, a)) { print a[1]; exit }
    }
    inblk && $0 !~ /^[ \t]/ { inblk=0 }
  ' "$DEFAULT_PA"
}

get_current_source_from_default() {
  [[ -f "$DEFAULT_PA" ]] || return 0
  awk '
    $0 ~ /^load-module[ \t]+module-echo-cancel\b/ { inblk=1; next }
    inblk && $0 ~ /source_master=/ {
      if (match($0, /source_master=([^ \t\\]+)/, a)) { print a[1]; exit }
    }
    inblk && $0 !~ /^[ \t]/ { inblk=0 }
  ' "$DEFAULT_PA"
}

pick_new_mic_auto() {
  # Prefer the highest index alsa_input that is different from current source_master
  local current
  current="$(get_current_source_from_default || true)"
  pactl list short sources | awk -v cur="$current" '
    $2 ~ /alsa_input/ && $2 != cur { print $1, $2 }
  ' | sort -nr | awk 'NR==1{print $2}'
}

if [[ -z "$MIC" ]]; then
  if [[ -n "$PATTERN" ]]; then
    MIC="$(pactl list short sources | awk -v p="$PATTERN" '$2 ~ /alsa_input/ && $2 ~ p {print $2; exit}')"
  fi
  if [[ -z "$MIC" ]]; then
    MIC="$(pick_new_mic_auto || true)"
  fi
fi

if [[ -z "$MIC" ]]; then
  echo -e "${RED}Could not determine new mic (alsa_input.*).${NC} Use --device or --pattern."
  exit 1
fi

if [[ -z "$SINK" ]]; then
  SINK="$(get_current_sink_from_default || true)"
  if [[ -z "$SINK" ]]; then
    SINK="$(pactl list short sinks | awk '$2 ~ /alsa_output/ {print $1, $2}' | sort -n | awk 'NR==1{print $2}')"
  fi
fi

if [[ -z "$SINK" ]]; then
  echo -e "${RED}No sink (alsa_output.*) found.${NC} Use --sink-device to specify."
  exit 1
fi

echo -e "${GRN}Selected mic (source_master):${NC} $MIC"
echo -e "${GRN}Selected sink (sink_master): ${NC} $SINK"

# Backup default.pa if exists
if [[ -f "$DEFAULT_PA" ]]; then
  cp -f "$DEFAULT_PA" "$DEFAULT_PA.bak.$(date +%Y%m%d-%H%M%S)"
  echo -e "${YLW}Backup saved:${NC} $DEFAULT_PA.bak.$(date +%Y%m%d-%H%M%S)"
fi

# Remove any existing module-echo-cancel block (line + indented continuations)
TMP="$(mktemp)"
if [[ -f "$DEFAULT_PA" ]]; then
  awk '
    BEGIN{skip=0}
    {
      if(skip==0){
        if($0 ~ /^load-module[ \t]+module-echo-cancel\b/){
          skip=1
          next
        } else {
          print
        }
      } else {
        if($0 ~ /^[ \t]/){
          next
        } else {
          skip=0
          print
        }
      }
    }
  ' "$DEFAULT_PA" > "$TMP"
else
  : > "$TMP"
fi

# Append new AEC block
cat >> "$TMP" <<EOF

# Added by switch_mic_aec.sh on $(timestamp)
load-module module-echo-cancel aec_method=webrtc \\
  source_master=${MIC} \\
  sink_master=${SINK} \\
  source_name=EC.source sink_name=EC.sink
EOF

if [[ $DRYRUN -eq 1 ]]; then
  echo -e "${YLW}[DRY-RUN] Would write to:${NC} $DEFAULT_PA"
  echo "----- begin preview -----"
  cat "$TMP"
  echo "----- end preview -----"
  rm -f "$TMP"
  exit 0
fi

mv "$TMP" "$DEFAULT_PA"

echo -e "${GRN}Updated:${NC} $DEFAULT_PA"

# Restart services
echo -e "${YLW}Restarting PipeWire & pipewire-pulse (user)...${NC}"
systemctl --user restart pipewire pipewire-pulse

# Give some time for nodes to appear
sleep 1

# Set defaults to EC.*
pactl set-default-source EC.source || true
pactl set-default-sink   EC.sink   || true

# Verify EC devices exist; if not, load on-the-fly
if ! pactl list short sources | awk '$2=="EC.source"{found=1} END{exit found?0:1}'; then
  echo -e "${YLW}EC.source not found after restart; loading module dynamically...${NC}"
  MID="$(pactl list modules short | awk '/module-echo-cancel/{print $1; exit}')"
  if [[ -n "$MID" ]]; then pactl unload-module "$MID" || true; fi
  pactl load-module module-echo-cancel aec_method=webrtc \
    source_master="$MIC" sink_master="$SINK" \
    source_name=EC.source sink_name=EC.sink >/dev/null
fi

echo
echo -e "${GRN}Done.${NC}"
pactl info | egrep 'Default (Source|Sink)' || true
echo
echo "Sources:"
pactl list short sources | grep -E 'EC\.source|alsa_input' || true
echo
echo "Sinks:"
pactl list short sinks   | grep -E 'EC\.sink|alsa_output' || true
