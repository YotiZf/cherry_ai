"""Host checks shared by Cherry's web-reading plugins."""
import ipaddress
import socket


def read_prefix(response, limit):
    """Read at most limit decoded response bytes into the result buffer."""
    data = bytearray()
    for chunk in response.iter_bytes():
        remaining = limit - len(data)
        data.extend(chunk[:remaining])
        if len(data) == limit:
            break
    return bytes(data)


def host_is_public(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0].split('%')[0]).is_global for info in infos)
