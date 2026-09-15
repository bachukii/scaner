"""
Barcode to PC — სერვერი (Windows).

ტელეფონი იმავე Wi-Fi-ში უერთდება ბრაუზერით, სკანირებული კოდი
კომპიუტერზე კლავიატურით იკრიფება (Excel, ბრაუზერი, ნებისმიერი ველი).

გაშვება:
    pip install aiohttp keyboard cryptography qrcode
    python server.py
"""

import asyncio
import json
import pathlib
import random
import socket
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from aiohttp import web, WSMsgType

try:
    import keyboard
except ImportError:
    keyboard = None

BASE = pathlib.Path(__file__).parent
STATIC = BASE / "static"
CERT = BASE / "cert.pem"
KEY = BASE / "key.pem"
ZXING = STATIC / "zxing.js"
ZXING_URL = "https://unpkg.com/@zxing/library@0.21.3/umd/index.min.js"
PORT = 8443

PIN = f"{random.randint(1000, 9999)}"
clients = set()


# ---------------------------------------------------------------- ქსელი

def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(ip: str) -> None:
    """თვითხელმოწერილი სერტიფიკატი — კამერა ბრაუზერში მხოლოდ HTTPS-ზე მუშაობს."""
    if CERT.exists() and KEY.exists():
        return
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    san = x509.SubjectAlternativeName([
        x509.IPAddress(__import__("ipaddress").ip_address(ip)),
        x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        x509.DNSName("localhost"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print("სერტიფიკატი შეიქმნა:", CERT.name)


def ensure_zxing() -> None:
    """iPhone-ს BarcodeDetector არ აქვს — ZXing ლოკალურად ვინახავთ, ერთხელ."""
    if ZXING.exists():
        return
    STATIC.mkdir(exist_ok=True)
    try:
        with urllib.request.urlopen(ZXING_URL, timeout=20) as r:
            ZXING.write_bytes(r.read())
        print("ZXing ჩამოიტვირთა (ერთჯერადად, შემდეგ ინტერნეტი აღარ სჭირდება)")
    except Exception as e:
        print("ZXing ვერ ჩამოიტვირთა:", e, "— iPhone-ზე სკანირება არ იმუშავებს")


# ---------------------------------------------------------------- აკრეფა

def type_code(text: str, suffix: str) -> None:
    if keyboard is None:
        print("[keyboard არ არის დაინსტალირებული]", text)
        return
    keyboard.write(text, delay=0)
    if suffix == "enter":
        keyboard.press_and_release("enter")
    elif suffix == "tab":
        keyboard.press_and_release("tab")


# ---------------------------------------------------------------- HTTP

async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    authed = False
    clients.add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "hello":
                authed = data.get("pin") == PIN
                await ws.send_json({"type": "hello", "ok": authed})
                if authed:
                    print(f"ტელეფონი დაუკავშირდა ({request.remote})")
                continue

            if data.get("type") == "scan":
                if not authed:
                    await ws.send_json({"type": "error", "message": "PIN არ ემთხვევა"})
                    continue
                code = str(data.get("code", "")).strip()
                if not code:
                    continue
                type_code(code, data.get("suffix", "enter"))
                print(f"{datetime.now():%H:%M:%S}  →  {code}")
                await ws.send_json({"type": "ack", "code": code})
    finally:
        clients.discard(ws)
    return ws


def print_banner(ip: str, url: str) -> None:
    print("\n" + "=" * 46)
    print("  Barcode to PC — სერვერი მუშაობს")
    print("=" * 46)
    print(f"  ტელეფონზე გახსენი:  {url}")
    print(f"  PIN:                {PIN}")
    print("  (სერტიფიკატის გაფრთხილებაზე → Advanced → Proceed)")
    print("=" * 46 + "\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass


def main():
    tunnel = "--tunnel" in sys.argv          # Cloudflare Tunnel: TLS-ს თვითონ ამთავრებს
    ip = lan_ip()
    if not tunnel:
        ensure_cert(ip)
    ensure_zxing()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)

    if tunnel:
        ctx = None
        url = f"http://localhost:{PORT}/?pin={PIN}"
        print(f"\n  cloudflared tunnel --url http://localhost:{PORT}")
        print(f"  მიღებულ მისამართს მიაწერე:  /?pin={PIN}\n")
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        url = f"https://{ip}:{PORT}/?pin={PIN}"
        print_banner(ip, url)
    if keyboard is None:
        print("გაფრთხილება: `pip install keyboard` — ამის გარეშე კოდი არ აიკრიფება\n")

    web.run_app(app, host="0.0.0.0", port=PORT, ssl_context=ctx, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
