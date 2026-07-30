"""Reservas de mantenimiento compartidas entre WOL-Bot y otros procesos.

El archivo JSON es un contrato IPC local y deliberadamente pequeño. Se protege
con flock y se reemplaza de forma atómica para que dos procesos (el scheduler y
``wolctl.py``) nunca observen contenido parcialmente escrito.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass

import config


class LeaseConflict(RuntimeError):
    """Otro proceso mantiene una reserva vigente para el servidor."""


@dataclass(frozen=True)
class MaintenanceLease:
    server_key: str
    owner: str
    expires_at: float

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def _empty_state() -> dict:
    return {"version": 1, "leases": {}}


@contextmanager
def _state_lock():
    lock_path = f"{config.MAINTENANCE_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_state() -> dict:
    try:
        with open(config.MAINTENANCE_FILE, encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_state()
    if not isinstance(state, dict) or not isinstance(state.get("leases"), dict):
        return _empty_state()
    return state


def _write_state(state: dict):
    path = config.MAINTENANCE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _active_leases(state: dict, now: float) -> dict:
    active = {}
    for server_key, raw in state["leases"].items():
        if not isinstance(raw, dict):
            continue
        owner = raw.get("owner")
        expires_at = raw.get("expires_at")
        if isinstance(owner, str) and isinstance(expires_at, (int, float)) and expires_at > now:
            active[server_key] = {"owner": owner, "expires_at": float(expires_at)}
    return active


def acquire_lease(
    server_key: str,
    owner: str,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> MaintenanceLease:
    if server_key not in config.SERVERS:
        raise KeyError(f"Servidor desconocido: {server_key}")
    owner = owner.strip()
    if not owner:
        raise ValueError("El owner de la reserva no puede estar vacío")
    if ttl_seconds < 1 or ttl_seconds > config.MAINTENANCE_MAX_TTL_SECS:
        raise ValueError(
            f"TTL inválido: debe estar entre 1 y {config.MAINTENANCE_MAX_TTL_SECS} segundos"
        )

    now = time.time() if now is None else now
    with _state_lock():
        state = _read_state()
        state["leases"] = _active_leases(state, now)
        current = state["leases"].get(server_key)
        if current and current["owner"] != owner:
            raise LeaseConflict(
                f"{server_key} ya está reservado por {current['owner']}"
            )
        expires_at = now + ttl_seconds
        state["leases"][server_key] = {
            "owner": owner,
            "expires_at": expires_at,
        }
        _write_state(state)

    return MaintenanceLease(server_key, owner, expires_at)


def active_lease(
    server_key: str,
    *,
    now: float | None = None,
) -> MaintenanceLease | None:
    now = time.time() if now is None else now
    with _state_lock():
        state = _read_state()
        active = _active_leases(state, now)
        if active != state["leases"]:
            state["leases"] = active
            _write_state(state)
        raw = active.get(server_key)
    if not raw:
        return None
    return MaintenanceLease(server_key, raw["owner"], raw["expires_at"])


def release_lease(server_key: str, owner: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _state_lock():
        state = _read_state()
        original_leases = state["leases"]
        state["leases"] = _active_leases(state, now)
        current = state["leases"].get(server_key)
        if not current or current["owner"] != owner:
            if state["leases"] != original_leases:
                _write_state(state)
            return False
        del state["leases"][server_key]
        _write_state(state)
        return True
