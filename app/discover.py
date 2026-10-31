"""Find RTSP cameras on the LAN: TCP port scan, then RTSP DESCRIBE on common vendor paths."""
import base64
import hashlib
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

PORTS = [554, 8554, 10554]
PATHS = [
    ("Hikvision sub", "/Streaming/Channels/102"),
    ("Hikvision main", "/Streaming/Channels/101"),
    ("Dahua/Amcrest sub", "/cam/realmonitor?channel=1&subtype=1"),
    ("Dahua/Amcrest main", "/cam/realmonitor?channel=1&subtype=0"),
    ("Tapo/TP-Link sub", "/stream2"),
    ("Tapo/TP-Link main", "/stream1"),
    ("Reolink sub", "/h264Preview_01_sub"),
    ("Reolink main", "/h264Preview_01_main"),
    ("XMEye/generic sub", "/user={user}&password={pass}&channel=1&stream=1.sdp"),
    ("Onvif generic", "/onvif1"),
    ("Generic", "/live/ch00_1"),
    ("Generic", "/h264"),
    ("Generic", "/live"),
    ("Generic", "/11"),
    ("Generic", "/"),
]


def _open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _describe(host, port, path, user, pwd, timeout=3.0):
    url = f"rtsp://{host}:{port}{path}"

    def send(auth=None):
        req = f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: cctv-mini\r\n"
        if auth:
            req += f"Authorization: {auth}\r\n"
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((req + "\r\n").encode())
            data = s.recv(4096).decode(errors="ignore")
        m = re.match(r"RTSP/1\.\d (\d{3})", data)
        return (int(m.group(1)) if m else 0), data

    try:
        code, data = send()
        if code == 401 and user:
            h = re.search(r'WWW-Authenticate:\s*Digest\s+(.*)', data, re.I)
            if h:
                p = dict(re.findall(r'(\w+)="([^"]*)"', h.group(1)))
                ha1 = hashlib.md5(f"{user}:{p.get('realm','')}:{pwd}".encode()).hexdigest()
                ha2 = hashlib.md5(f"DESCRIBE:{url}".encode()).hexdigest()
                resp = hashlib.md5(f"{ha1}:{p.get('nonce','')}:{ha2}".encode()).hexdigest()
                auth = (f'Digest username="{user}", realm="{p.get("realm","")}", nonce="{p.get("nonce","")}", '
                        f'uri="{url}", response="{resp}"')
            else:
                auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
            code, _ = send(auth)
        return code
    except OSError:
        return 0


def scan(subnet: str, user: str = "", pwd: str = "", timeout: float = 0.4) -> list[dict]:
    net = ipaddress.ip_network(subnet, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("subnet too large (max /22)")
    targets = [(str(h), p) for h in net.hosts() for p in PORTS]
    with ThreadPoolExecutor(128) as ex:
        open_ = [t for t, ok in zip(targets, ex.map(lambda t: _open(*t, timeout), targets)) if ok]

    results = []

    def probe(t):
        host, port = t
        found, auth_needed = [], False
        for label, path in PATHS:
            path = path.replace("{user}", quote(user)).replace("{pass}", quote(pwd))
            code = _describe(host, port, path, user, pwd)
            if code == 200:
                found.append({"label": label, "path": path})
                if len(found) >= 2:
                    break
            elif code == 401:
                auth_needed = True
        cred = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
        port_s = "" if port == 554 else f":{port}"
        return {"host": host, "port": port, "auth_needed": auth_needed and not found,
                "streams": [{"label": f["label"], "url": f"rtsp://{cred}{host}{port_s}{f['path']}"} for f in found]}

    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(probe, open_))
    return results
