"""Centroid/GFC FIX 4.4 market-data session (read-only, Phase 1)."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.services.fix.codec import field_map, split_messages
from app.services.fix.messages import (
    build_heartbeat,
    build_logon,
    build_logout,
    build_market_data_request,
)
from app.services.fix.parser import (
    parse_market_data_incremental,
    parse_market_data_snapshot,
)
from app.services.fix.quote_store import FIX_MSG_TYPE_LABELS, FixQuoteStore
from app.services.secrets import scrub

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30
_RECV_BUFFER = 65536
_REJECT_MSG_TYPES = frozenset({"3", "Y", "j"})


class CentroidMarketDataSession:
    """FIX 4.4 market-data session — subscribe only, no order routing."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.store = FixQuoteStore.get()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._out_seq = 1
        self._in_seq = 1
        self._buffer = ""
        self._md_req_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.centroid_md_host
            and self.settings.centroid_md_port
            and self.settings.centroid_md_sender_comp_id
            and self.settings.centroid_md_target_comp_id
        )

    def _scrub(self, text: str) -> str:
        return scrub(
            text,
            getattr(self.settings, "centroid_md_password", None),
            getattr(self.settings, "centroid_md_username", None),
        )

    def _safe_raw_summary(self, fmap: dict[str, str], msg_type: str) -> str:
        """Compact, redacted FIX summary for diagnostics (no passwords)."""
        parts = [f"35={msg_type}"]
        for tag in ("34", "49", "56", "262", "55", "58", "372", "373", "380", "381"):
            if tag in fmap:
                parts.append(f"{tag}={self._scrub(fmap[tag])}")
        return " ".join(parts)

    def start_background(self) -> None:
        if not self.configured:
            logger.info("Centroid FIX MD not configured — skipping session start.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="centroid-fix-md",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_logout()
        except Exception:  # noqa: BLE001
            pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
        self.store.set_health(
            status="disconnected",
            tcp_connected=False,
            fix_logged_on=False,
            md_subscription_status="none",
        )

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_run()
            except Exception as exc:  # noqa: BLE001
                msg = self._scrub(str(exc))
                logger.warning("Centroid FIX MD session error: %s", msg)
                self.store.set_health(status="error", last_error=msg, tcp_connected=False)
            self._close_socket()
            if not self._stop.is_set():
                time.sleep(5)

    def _connect_and_run(self) -> None:
        host = self.settings.centroid_md_host or ""
        port = int(self.settings.centroid_md_port or 0)
        self.store.set_health(
            status="connecting",
            host=host,
            port=port,
            sender_comp_id=self.settings.centroid_md_sender_comp_id,
            target_comp_id=self.settings.centroid_md_target_comp_id,
            ssl_enabled=bool(self.settings.centroid_md_ssl),
            tcp_connected=False,
            fix_logged_on=False,
            md_subscription_status="none",
        )
        raw_sock = socket.create_connection((host, port), timeout=15)
        raw_sock.settimeout(1.0)
        if self.settings.centroid_md_ssl:
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self._sock = raw_sock

        self.store.set_health(tcp_connected=True)

        if self.settings.centroid_md_reset_on_logon:
            self._out_seq = 1
            self._in_seq = 1

        self._send_logon()
        symbol = self.settings.centroid_md_symbol_usdmxn or "USD/MXN"
        self._md_req_id = f"MD-{int(time.time())}"
        sub_type = str(self.settings.centroid_md_subscription_request_type)
        depth = str(self.settings.centroid_md_market_depth)
        include_265 = bool(self.settings.centroid_md_include_md_update_type)
        md_msg = build_market_data_request(
            seq_num=self._next_out_seq(),
            sender_comp_id=self.settings.centroid_md_sender_comp_id or "",
            target_comp_id=self.settings.centroid_md_target_comp_id or "",
            symbol=symbol,
            md_req_id=self._md_req_id,
            subscription_type=sub_type,
            market_depth=depth,
            include_md_update_type=include_265,
        )
        self.store.record_md_request(
            md_req_id=self._md_req_id,
            symbol=symbol,
            subscription_request_type=sub_type,
            market_depth=depth,
            md_update_type="1" if include_265 else None,
            entry_types=["0", "1"],
        )
        self._send(md_msg)
        self.store.set_health(
            status="connected",
            outbound_seq=self._out_seq,
            inbound_seq=self._in_seq,
        )

        last_hb = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_hb >= _HEARTBEAT_INTERVAL:
                self._send_heartbeat()
                last_hb = time.monotonic()
            try:
                chunk = self._sock.recv(_RECV_BUFFER)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("FIX socket closed by remote host")
            self._buffer += chunk.decode("ascii", errors="replace")
            self._process_buffer()

    def _process_buffer(self) -> None:
        messages, self._buffer = split_messages(self._buffer)
        for raw in messages:
            self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        fmap = field_map(raw)
        msg_type = fmap.get("35", "")
        try:
            seq = int(fmap.get("34", "0"))
            if seq >= self._in_seq:
                self._in_seq = seq + 1
        except ValueError:
            pass

        if msg_type in _REJECT_MSG_TYPES or msg_type in FIX_MSG_TYPE_LABELS:
            self.store.record_inbound(
                msg_type=msg_type,
                fmap=fmap,
                raw_summary=self._safe_raw_summary(fmap, msg_type),
            )

        if msg_type == "0":
            self.store.set_health(
                last_heartbeat_at=datetime.now(timezone.utc),
                inbound_seq=self._in_seq,
            )
        elif msg_type == "1":
            test_id = fmap.get("112", "")
            self._send_heartbeat(test_req_id=test_id or None)
        elif msg_type == "A":
            self.store.set_health(
                status="connected",
                fix_logged_on=True,
                last_logon_at=datetime.now(timezone.utc),
            )
        elif msg_type == "W":
            parsed = parse_market_data_snapshot(raw)
            sym = parsed.get("symbol") or self.settings.centroid_md_symbol_usdmxn
            if sym:
                self.store.update_quote(sym, bid=parsed.get("bid"), ask=parsed.get("ask"))
        elif msg_type == "X":
            parsed = parse_market_data_incremental(raw)
            sym = parsed.get("symbol") or self.settings.centroid_md_symbol_usdmxn
            if sym:
                self.store.update_quote(sym, bid=parsed.get("bid"), ask=parsed.get("ask"))
        elif msg_type == "Y":
            text = fmap.get("58") or fmap.get("372") or "market data request rejected"
            cleaned = self._scrub(text)
            self.store.set_health(
                md_subscription_status="rejected",
                last_error=cleaned,
                warnings=[cleaned],
            )
        elif msg_type == "j":
            text = fmap.get("58") or "business message reject"
            cleaned = self._scrub(text)
            self.store.set_health(
                md_subscription_status="rejected",
                last_error=cleaned,
                warnings=[cleaned],
            )
        elif msg_type == "3":
            text = fmap.get("58") or fmap.get("373") or "session reject"
            cleaned = self._scrub(text)
            self.store.set_health(last_error=cleaned, warnings=[cleaned])
        elif msg_type == "5":
            text = fmap.get("58", "logout")
            raise ConnectionError(self._scrub(text))

    def _next_out_seq(self) -> int:
        seq = self._out_seq
        self._out_seq += 1
        self.store.set_health(outbound_seq=self._out_seq)
        return seq

    def _send(self, message: str) -> None:
        if not self._sock:
            raise ConnectionError("FIX socket not connected")
        payload = message.encode("ascii")
        with self._send_lock:
            self._sock.sendall(payload)

    def _send_logon(self) -> None:
        msg = build_logon(
            seq_num=self._next_out_seq(),
            sender_comp_id=self.settings.centroid_md_sender_comp_id or "",
            target_comp_id=self.settings.centroid_md_target_comp_id or "",
            username=self.settings.centroid_md_username,
            password=self.settings.centroid_md_password,
            heart_bt_int=_HEARTBEAT_INTERVAL,
            reset_seq_num=bool(self.settings.centroid_md_reset_on_logon),
        )
        self._send(msg)

    def _send_heartbeat(self, test_req_id: str | None = None) -> None:
        msg = build_heartbeat(
            seq_num=self._next_out_seq(),
            sender_comp_id=self.settings.centroid_md_sender_comp_id or "",
            target_comp_id=self.settings.centroid_md_target_comp_id or "",
            test_req_id=test_req_id,
        )
        self._send(msg)
        self.store.set_health(
            last_heartbeat_at=datetime.now(timezone.utc),
            outbound_seq=self._out_seq,
        )

    def _send_logout(self) -> None:
        if not self._sock:
            return
        msg = build_logout(
            seq_num=self._next_out_seq(),
            sender_comp_id=self.settings.centroid_md_sender_comp_id or "",
            target_comp_id=self.settings.centroid_md_target_comp_id or "",
            text="Client shutdown",
        )
        self._send(msg)

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
        self._sock = None
        self.store.set_health(tcp_connected=False)


# Module-level session manager
_session: CentroidMarketDataSession | None = None
_session_lock = threading.Lock()


def get_centroid_md_session(settings: "Settings") -> CentroidMarketDataSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = CentroidMarketDataSession(settings)
        return _session


def start_centroid_md_background(settings: "Settings") -> None:
    if not settings.centroid_md_enabled:
        return
    get_centroid_md_session(settings).start_background()


def stop_centroid_md_background(settings: "Settings") -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None
