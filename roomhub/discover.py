#!/usr/bin/env python3
"""
LAN discovery probe for the IRA room hub (Mac).

Scans for the Brahma Connect mDNS service (_BRAHMA._tcp.local) on the LAN.
Run on this Windows PC. If your Mac is already running the gateway, this
prints its IP:port so you don't have to look it up manually.
"""
from __future__ import annotations

import sys
import time


def main() -> int:
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    except ImportError:
        print("[discover] zeroconf not installed. Run: pip install zeroconf")
        return 2

    found: list[tuple[str, int, dict]] = []

    def on_change(browser, state, info):
        if state is ServiceStateChange.Added and info:
            addrs = info.parsed_addresses() or (info.address and [str(info.address)]) or []
            props = {k.decode(): v.decode() for k, v in (info.properties or {}).items()
                     if isinstance(k, bytes) and isinstance(v, bytes)}
            for a in addrs:
                found.append((a, info.port, props))

    zc = Zeroconf()
    try:
        browser = ServiceBrowser(zc, "_BRAHMA._tcp.local.", handlers=[on_change])
        print("[discover] Scanning LAN for _BRAHMA (Brahma Connect) for 10s...")
        time.sleep(10)
    finally:
        zc.close()

    if not found:
        print("[discover] No Brahma Connect gateway found on the LAN.")
        print("          -> Start it on the Mac: python roomhub/mac_roomhub_launcher.py")
        return 1
    for a, p, props in found:
        print(f"[discover] FOUND hub at {a}:{p}  props={props}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
