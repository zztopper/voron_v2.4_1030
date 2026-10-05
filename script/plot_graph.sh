#!/usr/bin/env bash
set -euo pipefail
exec /home/pi/klippy-env/bin/python /home/pi/printer_data/config/script/plot_resonances.py "$@"
