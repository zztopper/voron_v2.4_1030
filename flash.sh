#!/bin/bash
MCU1=/dev/serial/by-id/usb-Klipper_lpc1768_17700010C71039AF04BCF35BC42000F5-if00
MCU2=/dev/serial/by-id/usb-Klipper_lpc1768_19C0FF19E9A028AF536BC25AC42000F5-if00
VENDEV1="1d50:6015"
VENDEV2="1d50:6015"
cp ~/klipper_autoflash/linux_mcu.config ~/klipper/.config
cd ~/klipper
make clean
make flash > /dev/null
cd
cp ~/klipper_autoflash/lpc1768.config ~/klipper/.config
cd ~/klipper
make clean
if [ -e $MCU1 ]; then
	echo "Flashing 1st MCU by its path"
	make flash FLASH_DEVICE=$MCU1 > /dev/null
else
	echo "Flashing 1st MCU by its vendor and device - 1st pass"
	make flash FLASH_DEVICE=$VENDEV1
fi
if [ $? -ne 0 ]; then
	echo "Flashing 1st MCU by its vendor and device - 2nd pass"
	make flash FLASH_DEVICE=$VENDEV1
fi
if [ -e $MCU2 ]; then
	echo "Flashing 2nd MCU by its path"
	make flash FLASH_DEVICE=$MCU2 > /dev/null
else
	echo "Flashing 2nd MCU by its vendor and device - 1st pass"
	make flash FLASH_DEVICE=$VENDEV2
fi
if [ $? -ne 0 ]; then
	echo "Flashing 2nd MCU by its vendor and device - 2nd pass"
	make flash FLASH_DEVICE=$VENDEV2
fi
cd ~
sudo systemctl restart klipper_mcu
sudo systemctl restart klipper
