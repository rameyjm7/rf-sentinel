# WiFi / IEEE 802.11 SDR Plugin

RF Sentinel plugin for passive WiFi SDR activity and frame helpers.

The first integration layer detects 802.11 OFDM short-training-field-like
activity from a shared wideband IQ tap. It does not retune or own an SDR; it is
intended to run alongside Bluetooth, Zigbee, and other 2.4 GHz demodulators.

```bash
wifi_80211 activity --input capture.cs8 --sample-rate 60000000 --center-freq 2432000000
```

Events are JSONL and use protocol `wifi` with kind `wifi_activity`.

The package also includes a small 802.11 MAC parser and radiotap PCAP writer for
decoded MAC bytes from gr-ieee802-11.
