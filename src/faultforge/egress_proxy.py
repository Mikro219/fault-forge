"""The one way out for a dependency fetch: a CONNECT proxy that reaches registry.npmjs.org:443 only.

It runs in its own container with one leg on the install step's internal network and one on a
network made for the same run. It listens on the address it was given and nowhere else, so nothing
on the outward leg can use it. Every other host, port and method is refused. It resolves the name
itself, refuses an answer that isn't a public address, and connects to the address it checked
rather than to the name again. One JSON line is printed per connection.

Standard library only: this file is copied alone into the image and imports nothing from FaultForge.
"""

import argparse
import ipaddress
import json
import select
import socket
import sys
import threading
from collections.abc import Callable, Sequence

ALLOWED_HOST = "registry.npmjs.org"
ALLOWED_PORT = 443
MAX_HEAD_BYTES = 8192
MAX_CONNECTIONS = 128
HEAD_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 120.0
CHUNK = 64 * 1024
SELF = "self"

Resolve = Callable[[str, int], Sequence[str]]
Connect = Callable[[str, int], socket.socket]


def is_public_address(text: str) -> bool:
    """Global unicast only. An IPv6 address that carries an IPv4 one is refused whatever it carries."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if address.version == 6:
        embedded = address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None
        # ::a.b.c.d (IPv4-compatible) and 64:ff9b::/96 (NAT64).
        compatible = int(address) >> 32 == 0 or address in ipaddress.ip_network("64:ff9b::/96")
        if embedded or compatible:
            return False
    return bool(
        address.is_global and not address.is_multicast and not address.is_reserved and not address.is_loopback
        and not address.is_link_local and not address.is_private and not address.is_unspecified
    )


def system_resolve(host: str, port: int) -> list[str]:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(answer[4][0] for answer in answers))


def system_connect(address: str, port: int) -> socket.socket:
    return socket.create_connection((address, port), CONNECT_TIMEOUT_S)


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """(host, port) from `CONNECT host:port HTTP/1.x`, or None for anything else."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, separator, port = parts[1].rpartition(":")
    if not separator or not port.isdigit() or len(port) > 5:
        return None
    return host.lower(), int(port)


def _log(**fields: object) -> None:
    sys.stdout.write(json.dumps(fields, sort_keys=True) + "\n")
    sys.stdout.flush()


def _read_head(client: socket.socket) -> bytes | None:
    head = b""
    while b"\r\n\r\n" not in head:
        if len(head) > MAX_HEAD_BYTES:
            return None
        chunk = client.recv(1024)
        if not chunk:
            return None
        head += chunk
    return head


def _relay(client: socket.socket, upstream: socket.socket, early: bytes) -> tuple[int, int]:
    sent, received = len(early), 0
    if early:
        upstream.sendall(early)
    sockets = [client, upstream]
    while True:
        readable, _, _ = select.select(sockets, [], [], IDLE_TIMEOUT_S)
        if not readable:
            return sent, received
        for source in readable:
            data = source.recv(CHUNK)
            if not data:
                return sent, received
            if source is client:
                upstream.sendall(data)
                sent += len(data)
            else:
                client.sendall(data)
                received += len(data)


def handle(client: socket.socket, resolve: Resolve = system_resolve, connect: Connect = system_connect) -> None:
    upstream: socket.socket | None = None
    try:
        client.settimeout(HEAD_TIMEOUT_S)
        head = _read_head(client)
        target = parse_connect(head) if head is not None else None
        if target is None:
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            _log(allowed=False, reason="not_connect")
            return
        host, port = target
        # The host is printed only when it is the allowed one, so a log line never carries client text.
        if host != ALLOWED_HOST or port != ALLOWED_PORT:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            _log(allowed=False, reason="host_not_allowed", host="".join(c if c.isalnum() or c in ".-" else "?" for c in host)[:80], port=port)
            return
        addresses = [address for address in resolve(host, port) if is_public_address(address)]
        if not addresses:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            _log(allowed=False, reason="no_public_address", host=host, port=port)
            return
        # The address that was checked, never the name again.
        upstream = connect(addresses[0], port)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        client.settimeout(None)
        upstream.settimeout(None)
        sent, received = _relay(client, upstream, head.split(b"\r\n\r\n", 1)[1])
        _log(allowed=True, host=host, port=port, bytes_sent=sent, bytes_received=received)
    except OSError as error:
        _log(allowed=False, reason=f"failed_{type(error).__name__}")
    finally:
        for item in (client, upstream):
            if item is not None:
                try:
                    item.close()
                except OSError:
                    pass


def serve(server: socket.socket, resolve: Resolve = system_resolve, connect: Connect = system_connect) -> None:
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def worker(client: socket.socket) -> None:
        try:
            handle(client, resolve, connect)
        finally:
            slots.release()

    while True:
        try:
            client, _ = server.accept()
        except OSError:
            return
        if not slots.acquire(blocking=False):
            client.close()
            _log(allowed=False, reason="too_many_connections")
            continue
        threading.Thread(target=worker, args=(client,), daemon=True).start()


def listener(host: str, port: int) -> socket.socket:
    """Bound to the one address given. `self` is this container's own address, read at start, when
    its only network is the internal one."""
    if host == SELF:
        host = socket.gethostbyname(socket.gethostname())
    if ipaddress.ip_address(host).is_unspecified:
        raise ValueError("the proxy never listens on every address")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = socket.socket(family, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(64)
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True, help="address:port, or self:port")
    host, _, port = parser.parse_args().listen.rpartition(":")
    server = listener(host, int(port))
    _log(listening=server.getsockname()[0], port=server.getsockname()[1])
    serve(server)


if __name__ == "__main__":
    main()
