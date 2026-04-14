#!/bin/bash
# Log chrony frequency and CPU temperature every 60s
# Usage: ./log_freq_temp.sh [output_file]

LOGFILE="${1:-/tmp/chrony_freq_temp.csv}"

echo "timestamp,frequency_ppm,temp_c,rms_ns,skew_ppm,governor" > "$LOGFILE"

while true; do
    ts=$(date -Iseconds)
    tracking=$(chronyc tracking)
    freq=$(echo "$tracking" | awk '/Frequency/{print $3}')
    rms=$(echo "$tracking" | awk '/RMS offset/{print $4}')
    skew=$(echo "$tracking" | awk '/Skew/{print $3}')
    temp=$(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)
    gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)

    # Convert RMS to nanoseconds
    rms_ns=$(echo "$rms" | awk '{printf "%.0f", $1 * 1e9}')

    echo "$ts,$freq,$temp,$rms_ns,$skew,$gov" >> "$LOGFILE"
    sleep 60
done
