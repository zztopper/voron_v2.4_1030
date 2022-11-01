#/usr/bin/env bash
cd /home/pi/klipper_config
TODAY=$(date)
git add .
git commit -m "$TODAY"
#git push origin main
git push git@github.com:zztopper/voron_v2.4_1030 main
