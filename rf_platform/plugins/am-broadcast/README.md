# AM Broadcast Plugin

RF Sentinel plugin for scanning the medium-wave AM broadcast band with an SDRplay receiver.

## Usage

```bash
cd /home/jake/workspace/SDR/RF_Sentinel/rf_platform/plugins/am-broadcast
python -m pip install -e .
am-broadcast scan --serial 1710022B20
```

Default scan plan:

- US AM broadcast band: 530 kHz to 1700 kHz
- Channel spacing: 10 kHz
- SDRplay driver: `driver=sdrplay`
- Native sample format: CS16

Useful options:

```bash
am-broadcast scan --top 20
am-broadcast scan --sort freq
am-broadcast scan --json
am-broadcast scan --csv
am-broadcast scan --start-khz 540 --stop-khz 1200 --step-khz 10
am-broadcast scan --antenna A --sample-rate-sps 250000 --bandwidth-hz 80000 --dwell-s 0.2
```

If the SDRplay Python module is installed outside your virtualenv, run from the same shell where `SoapySDRUtil --find='driver=sdrplay'` works. You can also set:

```bash
export SDRPLAY_SERIAL=1710022B20
```
