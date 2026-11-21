"""A minimal HTTP CONNECT proxy that logs every tunnel it opens.

Evidence harness only. Listens on 127.0.0.1, accepts CONNECT host:port,
tunnels the bytes both ways, and appends "CONNECT host:port" to a log file so
a test can see which hosts went through the proxy.
"""

from __future__ import annotations

import socket
import sys
import threading

HOST = "127.0.0.1"


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client: socket.socket, log_path: str) -> None:
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            head += chunk
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            client.close()
            return
        target = parts[1]
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"CONNECT {target}\n")
        host, _, port = target.rpartition(":")
        upstream = socket.create_connection((host, int(port)), timeout=20)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
    except OSError:
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    port = int(sys.argv[1])
    log_path = sys.argv[2]
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(64)
    print(f"logging proxy on {HOST}:{port}, log {log_path}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client, log_path), daemon=True).start()


if __name__ == "__main__":
    main()
