from __future__ import annotations

import os
from typing import Any

import requests


def gateway_base(base_url: str | None = None) -> str:
    return (base_url or os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080")).rstrip("/")


def gateway_token(token: str | None = None) -> str:
    explicit = (token or "").strip()
    if explicit:
        return explicit
    return (os.getenv("SDR_GATEWAY_API_TOKEN", "") or "").strip()


def gateway_headers(token: str | None = None) -> dict[str, str]:
    resolved = gateway_token(token)
    return {"Authorization": f"Bearer {resolved}"} if resolved else {}


def ws_url_for_stream(base_url: str, stream_id: str, token: str | None = None) -> str:
    base = gateway_base(base_url)
    ws_base = "wss://" + base[len("https://") :] if base.startswith("https://") else "ws://" + base[len("http://") :]
    suffix = f"?keep=1"
    resolved_token = gateway_token(token)
    if resolved_token:
        suffix += f"&token={resolved_token}"
    return f"{ws_base}/ws/iq/{stream_id}{suffix}"


def list_devices(base_url: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    resp = requests.get(f"{gateway_base(base_url)}/devices", headers=gateway_headers(token), timeout=5)
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def list_streams(base_url: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    resp = requests.get(f"{gateway_base(base_url)}/streams", headers=gateway_headers(token), timeout=5)
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def list_sweeps(base_url: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    resp = requests.get(f"{gateway_base(base_url)}/sweeps", headers=gateway_headers(token), timeout=5)
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def start_stream(
    *,
    base_url: str | None,
    token: str | None,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
    baseband_filter_hz: int,
) -> dict[str, Any]:
    resp = requests.post(
        f"{gateway_base(base_url)}/streams/start",
        headers=gateway_headers(token),
        json={
            "device_id": device_id,
            "center_freq_hz": int(center_freq_hz),
            "sample_rate_sps": int(sample_rate_sps),
            "lna_gain_db": int(lna_gain_db),
            "vga_gain_db": int(vga_gain_db),
            "amp_enable": bool(amp_enable),
            "baseband_filter_hz": int(baseband_filter_hz),
            "duration_seconds": None,
            "num_samples": None,
        },
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            detail = str(payload.get("detail") or payload.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        if detail:
            raise RuntimeError(f"sdr-gateway stream start failed: HTTP {resp.status_code}: {detail}") from exc
        raise RuntimeError(f"sdr-gateway stream start failed: HTTP {resp.status_code}") from exc
    return dict(resp.json())


def retune_stream(
    *,
    base_url: str | None,
    token: str | None,
    stream_id: str,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
    baseband_filter_hz: int,
) -> dict[str, Any]:
    resp = requests.post(
        f"{gateway_base(base_url)}/streams/{stream_id}/retune",
        headers=gateway_headers(token),
        json={
            "device_id": device_id,
            "center_freq_hz": int(center_freq_hz),
            "sample_rate_sps": int(sample_rate_sps),
            "lna_gain_db": int(lna_gain_db),
            "vga_gain_db": int(vga_gain_db),
            "amp_enable": bool(amp_enable),
            "baseband_filter_hz": int(baseband_filter_hz),
        },
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            detail = str(payload.get("detail") or payload.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        if detail:
            raise RuntimeError(f"sdr-gateway stream retune failed: HTTP {resp.status_code}: {detail}") from exc
        raise RuntimeError(f"sdr-gateway stream retune failed: HTTP {resp.status_code}") from exc
    return dict(resp.json())


def stop_stream(base_url: str | None, token: str | None, stream_id: str) -> None:
    try:
        requests.post(f"{gateway_base(base_url)}/streams/{stream_id}/stop", headers=gateway_headers(token), timeout=5)
    except requests.RequestException:
        pass


def start_planned_sweep(
    *,
    base_url: str | None,
    token: str | None,
    device_id: str,
    frequencies_hz: list[int],
    margin_hz: int,
    bin_width_hz: int,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
    label: str,
) -> dict[str, Any]:
    resp = requests.post(
        f"{gateway_base(base_url)}/sweeps/plan/start",
        headers=gateway_headers(token),
        json={
            "device_id": device_id,
            "frequencies_hz": [int(freq) for freq in frequencies_hz],
            "margin_hz": int(margin_hz),
            "bin_width_hz": int(bin_width_hz),
            "lna_gain_db": int(lna_gain_db),
            "vga_gain_db": int(vga_gain_db),
            "amp_enable": bool(amp_enable),
            "label": label,
            "strategy": "auto",
        },
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            detail = str(payload.get("detail") or payload.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        if detail:
            raise RuntimeError(f"sdr-gateway sweep start failed: HTTP {resp.status_code}: {detail}") from exc
        raise RuntimeError(f"sdr-gateway sweep start failed: HTTP {resp.status_code}") from exc
    return dict(resp.json())


def sweep_samples(base_url: str | None, token: str | None, sweep_id: str) -> list[dict[str, Any]]:
    resp = requests.get(
        f"{gateway_base(base_url)}/sweeps/{sweep_id}/samples",
        headers=gateway_headers(token),
        timeout=5,
    )
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def stop_sweep(base_url: str | None, token: str | None, sweep_id: str) -> None:
    try:
        requests.post(f"{gateway_base(base_url)}/sweeps/{sweep_id}/stop", headers=gateway_headers(token), timeout=5)
    except requests.RequestException:
        pass


def start_iq_sweep(
    *,
    base_url: str | None,
    token: str | None,
    device_id: str,
    center_freqs_hz: list[int],
    start_freq_hz: int | None,
    stop_freq_hz: int | None,
    hop_hz: int | None,
    sample_rate_sps: int,
    dwell_s: float,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
    baseband_filter_hz: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "device_id": device_id,
        "center_freqs_hz": [int(freq) for freq in center_freqs_hz],
        "sample_rate_sps": int(sample_rate_sps),
        "dwell_s": float(dwell_s),
        "lna_gain_db": int(lna_gain_db),
        "vga_gain_db": int(vga_gain_db),
        "amp_enable": bool(amp_enable),
        "baseband_filter_hz": int(baseband_filter_hz),
    }
    if start_freq_hz is not None:
        payload["start_freq_hz"] = int(start_freq_hz)
    if stop_freq_hz is not None:
        payload["stop_freq_hz"] = int(stop_freq_hz)
    if hop_hz is not None:
        payload["hop_hz"] = int(hop_hz)

    resp = requests.post(
        f"{gateway_base(base_url)}/iq-sweeps/start",
        headers=gateway_headers(token),
        json=payload,
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        if detail:
            raise RuntimeError(f"sdr-gateway IQ sweep start failed: HTTP {resp.status_code}: {detail}") from exc
        raise RuntimeError(f"sdr-gateway IQ sweep start failed: HTTP {resp.status_code}") from exc
    return dict(resp.json())


def iq_sweep_chunk(base_url: str | None, token: str | None, iq_sweep_id: str, nbytes: int) -> dict[str, Any]:
    resp = requests.get(
        f"{gateway_base(base_url)}/iq-sweeps/{iq_sweep_id}/chunk",
        headers=gateway_headers(token),
        params={"nbytes": int(nbytes)},
        timeout=12,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("error") or "")
        except (ValueError, AttributeError):
            detail = resp.text.strip()
        if detail:
            raise RuntimeError(f"sdr-gateway IQ sweep chunk failed: HTTP {resp.status_code}: {detail}") from exc
        raise RuntimeError(f"sdr-gateway IQ sweep chunk failed: HTTP {resp.status_code}") from exc
    return dict(resp.json())


def stop_iq_sweep(base_url: str | None, token: str | None, iq_sweep_id: str) -> None:
    try:
        requests.post(f"{gateway_base(base_url)}/iq-sweeps/{iq_sweep_id}/stop", headers=gateway_headers(token), timeout=5)
    except requests.RequestException:
        pass
