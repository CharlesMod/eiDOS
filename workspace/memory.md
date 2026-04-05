# Working Memory
## Progress
- DHT22 sensor confirmed working on GPIO4. Reads take ~2s.
- adafruit-circuitpython-dht installed in venv at /home/pi/kairos-env
- read_dht22.py written and tested -- outputs temp_c, humidity_pct to stdout

## Next Steps
- Need to add CSV writer to read_dht22.py
- Consider using pathlib for file paths
- Flask may be too heavy -- stdlib http.server might be better for Pi

## Notes
- CRC errors happen occasionally -- retry logic handles it (max 3 retries)
- Sensor needs 2s cooldown between reads
