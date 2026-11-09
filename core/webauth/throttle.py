# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Política de IP de origen: IP real, lista blanca, redes denegadas o permitidas, y cuándo bloquear.

La IP real se toma de X-Forwarded-For SOLO si la conexión llega de un proxy de confianza (red de
Docker / proxy inverso de DSM). Si no, cualquiera podría falsear la cabecera y saltarse el bloqueo.
"""

import ipaddress
import socket
import struct

from .store import FAIL_LIMIT, AuthStore


def _networks(values) -> list:
    nets = []
    for v in values or []:
        v = v.strip()
        if not v:
            continue
        try:
            nets.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            nets.append(v)          # nombre literal (p.ej. "testclient" en los tests)
    return nets


def _in(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip in [n for n in nets if isinstance(n, str)]
    return any(not isinstance(n, str) and addr.version == n.version and addr in n for n in nets)


def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """Puerta de enlace del contenedor: por ella entran TODAS las conexiones publicadas por Docker
    (incluido el proxy inverso de DSM), así que es el proxy de confianza para X-Forwarded-For."""
    try:
        with open(route_file) as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError):
        pass
    return None


# Redes que cuentan como "locales" para el modo sin login. Lista explícita a propósito: `ipaddress.is_private` de Python
# incluye también rangos de documentación y reservados que no son una red local.
_PRIVATE = _networks([
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "100.64.0.0/10",                                 # CGNAT: Tailscale y similares
    "::1/128", "fc00::/7", "fe80::/10"])


def is_private_ip(ip: str) -> bool:
    """Red local, loopback, link-local o CGNAT (VPN tipo Tailscale). Una IP que no se entiende no cuenta como privada."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return _in(str(addr), _PRIVATE)


class IPPolicy:
    def __init__(self, trusted_proxies, allow, deny=(), only=()):
        self.trusted = _networks(trusted_proxies)
        self.allow = _networks(allow)
        self.deny = _networks(deny)
        self.only = _networks(only)

    def client_ip(self, peer: str, forwarded_for: str | None) -> str:
        """Recorre X-Forwarded-For de derecha a izquierda saltando proxies de confianza."""
        ip = peer or "0.0.0.0"
        if forwarded_for and _in(ip, self.trusted):
            for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
                ip = hop
                if not _in(hop, self.trusted):
                    break
        return ip

    def is_trusted(self, peer: str) -> bool:
        return _in(peer, self.trusted)

    def allowlisted(self, ip: str) -> bool:
        return _in(ip, self.allow)

    def denied(self, ip: str) -> str | None:
        """Motivo si la IP no puede entrar por configuración (independiente de los bloqueos)."""
        if _in(ip, self.deny):
            return "red denegada (AUTH_DENY_NETWORKS)"
        if self.only and not _in(ip, self.only) and not self.allowlisted(ip):
            return "fuera de las redes permitidas (AUTH_ONLY_NETWORKS)"
        return None


def register_failure(store: AuthStore, policy: IPPolicy, ip: str, user: str | None, kind: str) -> dict | None:
    """Apunta un fallo y bloquea la IP si llega al límite. Devuelve el bloqueo nuevo (para avisar)."""
    store.record_attempt(ip, user, False, kind)
    if policy.allowlisted(ip) or store.ban_until(ip):
        return None
    if store.recent_failures_ip(ip) >= FAIL_LIMIT:
        return store.ban(ip, f"{FAIL_LIMIT} fallos en 15 min (último: {kind})")
    return None


def user_delay(store: AuthStore, user: str) -> float:
    """Segundos que aún hay que esperar antes de otro intento para ese usuario (1, 2, 4… hasta 30)."""
    n, last = store.user_failures(user)
    if not n or last is None:
        return 0.0
    wait = min(30.0, 2.0 ** (n - 1))
    return max(0.0, last + wait - store.clock())
