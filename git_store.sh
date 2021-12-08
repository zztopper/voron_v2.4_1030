#/usr/bin/env bash
cd /home/pi/klipper_config
TODAY=$(date)
git add .
git commit --signoff -m "$TODAY"
git push origin main
