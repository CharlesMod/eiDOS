#!/bin/bash
set +e

# eiDOS rescue: install first-boot provisioning service
cp /boot/firmware/eidos-first-boot.service /etc/systemd/system/eidos-first-boot.service
chmod 644 /etc/systemd/system/eidos-first-boot.service
systemctl enable eidos-first-boot.service

# Start it immediately (don't wait for reboot)
systemctl start eidos-first-boot.service &

# Self-destruct
rm -f /boot/firmware/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt
exit 0
