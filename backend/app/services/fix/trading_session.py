"""Persistent Centroid/GFC FIX 4.4 trading session for demo conformance only."""

from __future__ import annotations

import logging
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from app.database import engine
from app.services.fix.codec import field_map, split_messages
from app.services.fix.messages import build_heartbeat, build_logon, build_logout
from app.services.fix.trading_messages import build_new_order_single, build_order_cancel_request, build_order_status_request
from app.services.secrets import scrub

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)
_HEARTBEAT_INTERVAL = 30
_RECV_BUFFER = 65536
_SEQUENCE_KEY = "centroid_td"
_LOW_SEQ_RE = re.compile(r"MsgSeqNum too low, expecting\s+(\d+)\s+but received\s+(\d+)", re.IGNORECASE)


class CentroidTradingSession:
    """Dedicated trading FIX session, isolated from the market-data session."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._seq_lock = threading.Lock()
        self._out_seq = 1
        self._in_seq = 1
        self._sequence_persistent = False
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
        self._load_sequence_state()

    def _scrub(self, text_value: str | None) -> str | None:
        if text_value is None:
            return None
        return scrub(text_value, getattr(self.settings, "centroid_trading_password", None), getattr(self.settings, "centroid_trading_username", None))

    def _ensure_sequence_table(self) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS fix_session_state (
                        session_key VARCHAR(64) PRIMARY KEY,
                        next_out_seq INTEGER NOT NULL,
                        next_in_seq INTEGER NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    def _load_sequence_state(self) -> None:
        try:
            self._ensure_sequence_table()
            with engine.begin() as conn:
                row = conn.execute(
                    text(
                        "SELECT next_out_seq, next_in_seq FROM fix_session_state "
                        "WHERE session_key = :session_key"
                    ),
                    {"session_key": _SEQUENCE_KEY},
                ).mappings().first()
                if row:
                    self._out_seq = max(1, int(row["next_out_seq"]))
                    self._in_seq = max(1, int(row["next_in_seq"]))
                else:
                    conn.execute(
                        text(
                            "INSERT INTO fix_session_state "
                            "(session_key, next_out_seq, next_in_seq, updated_at) "
                            "VALUES (:session_key, :next_out_seq, :next_in_seq, CURRENT_TIMESTAMP)"
                        ),
                        {
                            "session_key": _SEQUENCE_KEY,
                            "next_out_seq": self._out_seq,
                            "next_in_seq": self._in_seq,
                        },
                    )
            self._sequence_persistent = True
            logger.info(
                "Loaded Centroid trading FIX sequence state: outbound=%s inbound=%s",
                self._out_seq,
                self._in_seq,
            )
        except Exception as exc:  # noqa: BLE001
            self._sequence_persistent = False
            logger.warning("Centroid trading FIX sequence persistence unavailable: %s", self._scrub(str(exc)))

    def _persist_sequence_state(self) -> None:
        try:
            self._ensure_sequence_table()
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO fix_session_state
                            (session_key, next_out_seq, next_in_seq, updated_at)
                        VALUES
                            (:session_key, :next_out_seq, :next_in_seq, CURRENT_TIMESTAMP)
                        ON CONFLICT(session_key) DO UPDATE SET
                            next_out_seq = excluded.next_out_seq,
                            next_in_seq = excluded.next_in_seq,
                            updated_at = CURRENT_TIMESTAMP
                        """
                    ),
                    {
                        "session_key": _SEQUENCE_KEY,
                        "next_out_seq": self._out_seq,
                        "next_in_seq": self._in_seq,
                    },
                )
            self._sequence_persistent = True
        except Exception as exc:  # noqa: BLE001
            self._sequence_persistent = False
            logger.warning("Could not persist Centroid trading FIX sequence state: %s", self._scrub(str(exc)))

    def _reconcile_expected_out_seq(self, reason: str) -> bool:
        match = _LOW_SEQ_RE.search(reason)
        if not match:
            return False
        expected = int(match.group(1))
        received = int(match.group(2))
        if expected < 1:
            return False
        with self._seq_lock:
            old = self._out_seq
            self._out_seq = expected
            self._persist_sequence_state()
        logger.warning(
            "Reconciled Centroid trading FIX outbound sequence from %s to %s after peer rejected %s",
            old,
            expected,
            received,
        )
        return True

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
                "sequence_persistent": self._sequence_persistent,
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
                error_text = self._scrub(str(exc)) or "unknown error"
                self._reconcile_expected_out_seq(error_text)
                self._state.update(status="error", tcp_connected=False, fix_logged_on=False, last_error=error_text)
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
            # The GFC/Centroid demo trading endpoint currently uses a weak,
            # self-signed certificate. Keep TLS encryption and SNI for the
            # configured demo hostname, but disable certificate-chain and
            # hostname verification only on this dedicated demo trading socket.
            # No global SSL defaults or market-data TLS settings are changed.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
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
                    with self._seq_lock:
                        self._in_seq = seq + 1
                        self._persist_sequence_state()
            except ValueError:
                pass
            if msg_type == "0":
                self._state["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            elif msg_type == "1":
                self._send_heartbeat(test_req_id=fmap.get("112") or None)
            elif msg_type == "A":
                self._state.update(status="connected", fix_logged_on=True, last_logon_at=datetime.now(timezone.utc).isoformat())
                logger.info("Centroid trading FIX logon accepted")
            elif msg_type == "8":
                self._state["last_execution_report"] = self._execution_report(fmap)
            elif msg_type == "9":
                self._state["last_cancel_reject"] = self._safe_map(fmap, ["11", "37", "39", "41", "102", "434", "58"])
            elif msg_type == "j":
                self._state["last_business_reject"] = self._safe_map(fmap, ["45", "372", "380", "58"])
            elif msg_type == "3":
                self._state["last_session_reject"] = self._safe_map(fmap, ["45", "371", "372", "373", "58"])
            elif msg_type == "5":
                reason = self._scrub(fmap.get("58") or "logout") or "logout"
                self._reconcile_expected_out_seq(reason)
                raise ConnectionError(reason)

    def _execution_report(self, fmap: dict[str, str]) -> dict[str, Any]:
        return self._safe_map(fmap, ["11", "17", "150", "55", "54", "38", "40", "32", "59", "37", "39", "41", "31", "151", "14", "6", "44", "58", "60"])

    def _safe_map(self, fmap: dict[str, str], tags: list[str]) -> dict[str, Any]:
        return {tag: self._scrub(fmap[tag]) for tag in tags if tag in fmap}

    def _next_out_seq(self) -> int:
        with self._seq_lock:
            seq = self._out_seq
            self._out_seq += 1
            self._persist_sequence_state()
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
