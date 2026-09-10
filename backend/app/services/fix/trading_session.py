"""Persistent Centroid/GFC FIX 4.4 trading session for demo conformance only."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.services.fix.codec import field_map, split_messages
from app.services.fix.messages import build_heartbeat, build_logon, build_logout
from app.services.fix.trading_messages import build_new_order_single, build_order_cancel_request, build_order_status_request
from app.services.secrets import scrub

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)
_HEARTBEAT_INTERVAL = 30
_RECV_BUFFER = 65536


class CentroidTradingSession:
    """Dedicated trading FIX session, isolated from the market-data session."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._out_seq = 1
        self._in_seq = 1
        self._buffer = ""
        self._state: dict[str, Any] = {
            "status": "disconnected",
            "tcp_connected": False,
            "fix_logged_on": False,
            "last_logon_at": None,
            "last_heartbeat_at": None,
            "last_error": None,
            "last_execution_report": None,
            "last_cancel_reject": None,
            "last_business_reject": None,
            "last_session_reject": None,
            "last_order_request": None,
        }

    def _scrub(self, text: str | None) -> str | None:
        if text is None:
            return None
        return scrub(text, getattr(self.settings, "centroid_trading_password", None), getattr(self.settings, "centroid_trading_username", None))

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.centroid_trading_host
            and self.settings.centroid_trading_port
            and self.settings.centroid_trading_sender_comp_id
            and self.settings.centroid_trading_target_comp_id
            and self.settings.centroid_trading_account
        )

    def diagnostics(self) -> dict[str, Any]:
        out = dict(self._state)
        out.update(
            {
                "configured": self.configured,
                "trading_enabled": bool(self.settings.centroid_trading_enabled),
                "conformance_mode": bool(self.settings.centroid_trading_conformance_mode),
                "outbound_seq": self._out_seq,
                "inbound_seq": self._in_seq,
                "host": self.settings.centroid_trading_host,
                "port": self.settings.centroid_trading_port,
                "sender_comp_id": self.settings.centroid_trading_sender_comp_id,
                "target_comp_id": self.settings.centroid_trading_target_comp_id,
                "account_set": bool(self.settings.centroid_trading_account),
                "credentials": {
                    "username_set": bool(self.settings.centroid_trading_username),
                    "password_set": bool(self.settings.centroid_trading_password),
                },
            }
        )
        return out

    def start_background(self) -> None:
        if not self.settings.centroid_trading_enabled or not self.configured:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="centroid-fix-trading", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_logout()
        except Exception:
            pass
        self._close_socket()

    def send_conformance_order(self, *, symbol: str, side: str, quantity: float, ord_type: str = "1", time_in_force: str = "3", price: float | None = None, ttl_ms: int | None = None) -> dict[str, Any]:
        if not self.settings.centroid_trading_conformance_mode:
            raise PermissionError("Conformance mode is disabled")
        if not self.settings.centroid_trading_enabled:
            raise PermissionError("Trading session is disabled")
        if not self._sock or not self._state.get("fix_logged_on"):
            raise ConnectionError("Trading FIX session is not logged on")
        msg = build_new_order_single(
            seq_num=self._next_out_seq(),
            sender_comp_id=self.settings.centroid_trading_sender_comp_id or "",
            target_comp_id=self.settings.centroid_trading_target_comp_id or "",
            account=self.settings.centroid_trading_account or "",
            symbol=symbol,
            side=side,
            quantity=quantity,
            ord_type=ord_type,
            time_in_force=time_in_force,
            price=price,
            ttl_ms=ttl_ms,
        )
        self._state["last_order_request"] = {"symbol": symbol, "side": str(side), "quantity": quantity, "ord_type": str(ord_type), "time_in_force": str(time_in_force), "price": price, "ttl_ms": ttl_ms}
        self._send(msg)
        return dict(self._state["last_order_request"])

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_run()
            except Exception as exc:
                self._state.update(status="error", tcp_connected=False, fix_logged_on=False, last_error=self._scrub(str(exc)))
                logger.warning("Centroid trading FIX session error: %s", self._state["last_error"])
            self._close_socket()
            if not self._stop.is_set():
                time.sleep(5)

    def _connect_and_run(self) -> None:
        host = self.settings.centroid_trading_host or ""
        port = int(self.settings.centroid_trading_port or 0)
        self._state.update(status="connecting", tcp_connected=False, fix_logged_on=False, last_error=None)
        raw_sock = socket.create_connection((host, port), timeout=15)
        raw_sock.settimeout(1.0)
        if self.settings.centroid_trading_ssl:
            ctx = ssl.create_default_context()
            # Centroid's demo endpoint currently presents a legacy certificate
            # that OpenSSL 3 rejects at the default security level. Lower the
            # cipher security level only for this dedicated trading socket while
            # keeping CA validation and hostname verification enabled.
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            self._sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self._sock = raw_sock
        self._state.update(tcp_connected=True)
        # Per Centroid v0.16.9, trading sessions MUST NOT reset sequence numbers on Logon.
        self._send_logon()
        self._state["status"] = "connected"
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
                raise ConnectionError("Trading FIX socket closed by remote host")
            self._buffer += chunk.decode("ascii", errors="replace")
            self._process_buffer()

    def _process_buffer(self) -> None:
        messages, self._buffer = split_messages(self._buffer)
        for raw in messages:
            fmap = field_map(raw)
            msg_type = fmap.get("35", "")
            try:
                seq = int(fmap.get("34", "0"))
                if seq >= self._in_seq:
                    self._in_seq = seq + 1
            except ValueError:
                pass
            if msg_type == "0":
                self._state["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            elif msg_type == "1":
                self._send_heartbeat(test_req_id=fmap.get("112") or None)
            elif msg_type == "A":
                self._state.update(status="connected", fix_logged_on=True, last_logon_at=datetime.now(timezone.utc).isoformat())
            elif msg_type == "8":
                self._state["last_execution_report"] = self._execution_report(fmap)
            elif msg_type == "9":
                self._state["last_cancel_reject"] = self._safe_map(fmap, ["11", "37", "39", "41", "102", "434", "58"])
            elif msg_type == "j":
                self._state["last_business_reject"] = self._safe_map(fmap, ["45", "372", "380", "58"])
            elif msg_type == "3":
                self._state["last_session_reject"] = self._safe_map(fmap, ["45", "371", "372", "373", "58"])
            elif msg_type == "5":
                raise ConnectionError(self._scrub(fmap.get("58") or "logout") or "logout")

    def _execution_report(self, fmap: dict[str, str]) -> dict[str, Any]:
        return self._safe_map(fmap, ["11", "17", "150", "55", "54", "38", "40", "32", "59", "37", "39", "41", "31", "151", "14", "6", "44", "58", "60"])

    def _safe_map(self, fmap: dict[str, str], tags: list[str]) -> dict[str, Any]:
        return {tag: self._scrub(fmap[tag]) for tag in tags if tag in fmap}

    def _next_out_seq(self) -> int:
        seq = self._out_seq
        self._out_seq += 1
        return seq

    def _send(self, message: str) -> None:
        if not self._sock:
            raise ConnectionError("Trading FIX socket not connected")
        with self._send_lock:
            self._sock.sendall(message.encode("ascii"))

    def _send_logon(self) -> None:
        self._send(
            build_logon(
                seq_num=self._next_out_seq(),
                sender_comp_id=self.settings.centroid_trading_sender_comp_id or "",
                target_comp_id=self.settings.centroid_trading_target_comp_id or "",
                username=self.settings.centroid_trading_username,
                password=self.settings.centroid_trading_password,
                heart_bt_int=_HEARTBEAT_INTERVAL,
                reset_seq_num=False,
            )
        )

    def _send_heartbeat(self, test_req_id: str | None = None) -> None:
        self._send(build_heartbeat(seq_num=self._next_out_seq(), sender_comp_id=self.settings.centroid_trading_sender_comp_id or "", target_comp_id=self.settings.centroid_trading_target_comp_id or "", test_req_id=test_req_id))
        self._state["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()

    def _send_logout(self) -> None:
        if self._sock:
            self._send(build_logout(seq_num=self._next_out_seq(), sender_comp_id=self.settings.centroid_trading_sender_comp_id or "", target_comp_id=self.settings.centroid_trading_target_comp_id or "", text="Client shutdown"))

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._state.update(tcp_connected=False, fix_logged_on=False)


_session: CentroidTradingSession | None = None
_session_lock = threading.Lock()


def get_centroid_trading_session(settings: "Settings") -> CentroidTradingSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = CentroidTradingSession(settings)
        return _session


def start_centroid_trading_background(settings: "Settings") -> None:
    get_centroid_trading_session(settings).start_background()


def stop_centroid_trading_background(settings: "Settings") -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None
