#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UltiPout — local AI-styled face veneer lab that pairs with the TroutPout on-chain ledger.
Renders cheek-ribbon warps, iris-glass tints, and jaw-sculpt previews without shipping raw landmarks.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import enum
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import random
import re
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Pillow is required: pip install pillow") from exc

try:
    from eth_account import Account
except ImportError:
    Account = None  # type: ignore

try:
    from web3 import Web3
except ImportError:
    Web3 = None  # type: ignore

TRAILHEAD_TREASURY = "0x45fa9df71094473a87e766470cdc82433a650431"
RIPPLINK_CURATOR_SIGNER = "0xe1bde50a8e12befd959e4fe5c5bca7666ac622a9"
BRAID_FEE_AUX = "0x88ba7c065c006ef8182fc8e0e3d045e4d393d746"
GLINT_ESCALATION_SINK = "0x9f9868d5d16749ab87fa81965a81b15f53734482"
DEFAULT_OWNER_BOOT = "0xb3df8eee87925df5d405d985d01cedce3ec41012"

CHAIN_ID_DEFAULT = 1
POUT_SESSION_TYPESTRING = (
    "PoutSession(address user,uint256 sessionNonce,uint256 creditsCost,uint256 effectId,uint256 deadline)"
)

LOG = logging.getLogger("ultipout")

TROUT_POUT_ABI: List[Dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "riffleCredits",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "smoltSessionNonces",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "kelpiePaused",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class PoutRenderLane(enum.IntEnum):
    Unassigned = 0
    CheekBloom = 1
    IrisGlass = 2
    JawSculpt = 3
    MicroTexture = 4
    TemporalWarp = 5


@dataclasses.dataclass(frozen=True)
class UltiPoutConfig:
    rpc_url: str
    trout_pout_address: str
    private_key_hex: Optional[str]
    http_host: str
    http_port: int
    cache_dir: Path
    sharpen_cap: float
    thermal_noise: float
    cheek_lift: float
    iris_glass_strength: float
    jaw_pinch: float
    telemetry_salt: bytes
    chain_id: int

    @staticmethod
    def from_env() -> "UltiPoutConfig":
        import secrets as S

        base = Path(os.environ.get("ULTIPOUT_CACHE", str(Path.home() / ".ultipout")))
        salt_hex = os.environ.get("ULTIPOUT_SALT", "")
        salt = bytes.fromhex(salt_hex.replace("0x", "")) if salt_hex else S.token_bytes(32)
        return UltiPoutConfig(
            rpc_url=os.environ.get("ULTIPOUT_RPC", "https://eth.llamarpc.com"),
            trout_pout_address=os.environ.get("ULTIPOUT_CONTRACT", ""),
            private_key_hex=os.environ.get("ULTIPOUT_PRIVATE_KEY"),
            http_host=os.environ.get("ULTIPOUT_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("ULTIPOUT_PORT", "8765")),
            cache_dir=base,
            sharpen_cap=float(os.environ.get("ULTIPOUT_SHARPEN", "1.35")),
            thermal_noise=float(os.environ.get("ULTIPOUT_THERMAL", "0.11")),
            cheek_lift=float(os.environ.get("ULTIPOUT_CHEEK", "0.18")),
            iris_glass_strength=float(os.environ.get("ULTIPOUT_IRIS", "0.27")),
            jaw_pinch=float(os.environ.get("ULTIPOUT_JAW", "0.14")),
            telemetry_salt=salt,
            chain_id=int(os.environ.get("ULTIPOUT_CHAIN_ID", str(CHAIN_ID_DEFAULT))),
        )


@dataclasses.dataclass
class FaceBBox:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return max(1, self.x1 - self.x0)

    @property
    def height(self) -> int:
        return max(1, self.y1 - self.y0)

    def clamp(self, w: int, h: int) -> "FaceBBox":
        return FaceBBox(
            max(0, min(self.x0, w - 1)),
            max(0, min(self.y0, h - 1)),
            max(1, min(self.x1, w)),
            max(1, min(self.y1, h)),
        )


@dataclasses.dataclass
class PoutSessionDraft:
    user: str
    session_nonce: int
    credits_cost: int
    effect_id: int
    deadline: int


class UltiPoutTelemetry:
    """Hashes telemetry shards so previews never ship raw landmarks."""

    def __init__(self, salt: bytes) -> None:
        self._salt = salt

    def shard(self, label: str, payload: Mapping[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(self._salt + label.encode() + blob).hexdigest()


class CheekRibbonField:
    """Vector field approximating a cheek-bloom lift for the preview shader."""

    def __init__(self, strength: float) -> None:
        self.strength = float(strength)

    def sample_offset(self, nx: float, ny: float) -> Tuple[float, float]:
        r = math.hypot(nx - 0.5, ny - 0.62)
        theta = math.atan2(ny - 0.62, nx - 0.5)
        mag = math.exp(-((r - 0.18) ** 2) / 0.012) * self.strength
        return mag * math.cos(theta + 0.4), mag * math.sin(theta + 0.4)


class IrisGlassFilter:
    """Spectral tint overlay mimicking glassy iris caustics."""

    def __init__(self, strength: float) -> None:
        self.strength = max(0.0, min(1.0, strength))

    def apply(self, crop: Image.Image) -> Image.Image:
        r, g, b, *rest = crop.split()
        a = rest[0] if rest else None
        r = r.point(lambda v: int(min(255, v + self.strength * 22)))
        g = g.point(lambda v: int(min(255, v + self.strength * 38)))
        b = b.point(lambda v: int(max(0, v - self.strength * 18)))
        if a:
            return Image.merge("RGBA", (r, g, b, a))
        return Image.merge("RGB", (r, g, b))


class JawSculptMap:
    """Pinches lower facial third slightly inward."""

    def __init__(self, pinch: float) -> None:
        self.pinch = pinch

    def warp_bbox(self, bbox: FaceBBox) -> FaceBBox:
        cx = (bbox.x0 + bbox.x1) // 2
        cy = (bbox.y0 + bbox.y1) // 2
        w = int(bbox.width * (1.0 - self.pinch))
        h = int(bbox.height * (1.0 - self.pinch * 0.5))
        return FaceBBox(cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)


class ThermalDither:
    """Adds benign noise so compressed exports do not ring on flat skin tones."""

    def __init__(self, sigma: float) -> None:
        self.sigma = sigma

    def apply(self, im: Image.Image) -> Image.Image:
        if np is None:
            arr = im.convert("RGB")
            pix = arr.load()
            w, h = arr.size
            for y in range(h):
                for x in range(w):
                    if (x + y) % 6 == 0:
                        r, g, b = pix[x, y]
                        n = int((random.random() - 0.5) * 10 * self.sigma)
                        pix[x, y] = (
                            max(0, min(255, r + n)),
                            max(0, min(255, g + n)),
                            max(0, min(255, b + n)),
                        )
            return arr
        a = np.asarray(im.convert("RGB"), dtype=np.float32)
        noise = np.random.normal(0, 4.0 * self.sigma, a.shape)
        out = np.clip(a + noise, 0, 255).astype(np.uint8)
        return Image.fromarray(out, "RGB")


def _pseudo_face_bbox(w: int, h: int, seed: int) -> FaceBBox:
    rnd = random.Random(seed)
    fw = int(w * rnd.uniform(0.35, 0.52))
    fh = int(h * rnd.uniform(0.42, 0.58))
    cx = int(w * rnd.uniform(0.45, 0.55))
    cy = int(h * rnd.uniform(0.38, 0.48))
    return FaceBBox(cx - fw // 2, cy - fh // 2, cx + fw // 2, cy + fh // 2).clamp(w, h)


def detect_face_bbox(image: Image.Image, seed: int = 7) -> FaceBBox:
    """Fallback face box when OpenCV or MediaPipe are absent."""
    w, h = image.size
    return _pseudo_face_bbox(w, h, seed)


def _ensure_rgba(im: Image.Image) -> Image.Image:
    return im if im.mode == "RGBA" else im.convert("RGBA")


def apply_cheek_bloom(image: Image.Image, bbox: FaceBBox, strength: float) -> Image.Image:
    im = _ensure_rgba(image)
    field = CheekRibbonField(strength)
    w, h = im.size
    pix = im.load()
    assert pix is not None
    out = Image.new("RGBA", (w, h))
    op = out.load()
    assert op is not None
    for y in range(bbox.y0, bbox.y1):
        for x in range(bbox.x0, bbox.x1):
            nx = (x - bbox.x0) / bbox.width
            ny = (y - bbox.y0) / bbox.height
            dx, dy = field.sample_offset(nx, ny)
            sx = int(x + dx * bbox.width)
            sy = int(y + dy * bbox.height)
            sx = max(0, min(w - 1, sx))
            sy = max(0, min(h - 1, sy))
            op[x, y] = pix[sx, sy]
    for y in range(h):
        for x in range(w):
            if not (bbox.x0 <= x < bbox.x1 and bbox.y0 <= y < bbox.y1):
                op[x, y] = pix[x, y]
    return out


def apply_iris_glass(image: Image.Image, bbox: FaceBBox, strength: float) -> Image.Image:
    im = _ensure_rgba(image)
    crop = im.crop((bbox.x0, bbox.y0, bbox.x1, bbox.y1))
    tinted = IrisGlassFilter(strength).apply(crop)
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    layer.paste(tinted, (bbox.x0, bbox.y0))
    return Image.alpha_composite(im, layer)


def apply_jaw_pinch(image: Image.Image, bbox: FaceBBox, pinch: float) -> Image.Image:
    im = image.convert("RGB")
    nb = JawSculptMap(pinch).warp_bbox(bbox).clamp(im.width, im.height)
    face = im.crop((bbox.x0, bbox.y0, bbox.x1, bbox.y1))
    face = face.resize((max(1, nb.width), max(1, nb.height)), Image.Resampling.LANCZOS)
    canvas = im.copy()
    canvas.paste(face, (nb.x0, nb.y0))
    return canvas


def compose_lane_stack(
    image: Image.Image,
    lane: PoutRenderLane,
    cfg: UltiPoutConfig,
    seed: int,
) -> Image.Image:
    bbox = detect_face_bbox(image, seed)
    out = image.convert("RGBA")
    if lane in (PoutRenderLane.CheekBloom, PoutRenderLane.TemporalWarp):
        out = apply_cheek_bloom(out, bbox, cfg.cheek_lift * (1.2 if lane == PoutRenderLane.TemporalWarp else 1.0))
    if lane in (PoutRenderLane.IrisGlass, PoutRenderLane.TemporalWarp):
        out = apply_iris_glass(out, bbox, cfg.iris_glass_strength)
    if lane in (PoutRenderLane.JawSculpt, PoutRenderLane.TemporalWarp):
        out = apply_jaw_pinch(out.convert("RGB"), bbox, cfg.jaw_pinch).convert("RGBA")
    out = ThermalDither(cfg.thermal_noise).apply(out.convert("RGB")).convert("RGBA")
    sharp = ImageEnhance.Sharpness(out)
    out = sharp.enhance(min(cfg.sharpen_cap, 2.0))
    return out


def image_to_data_url(im: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = f"image/{fmt.lower()}"
    return f"data:{mime};base64,{b64}"


def _keccak256(data: bytes) -> bytes:
    try:
        from Crypto.Hash import keccak

        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except ImportError:
        try:
            import sha3

            k = sha3.keccak_256()
            k.update(data)
            return k.digest()
        except ImportError as exc:
            raise RuntimeError("Install pycryptodome or pysha3 for local EIP-712 hashing") from exc


def _abi_encode_packed_uint256(v: int) -> bytes:
    return int(v).to_bytes(32, "big", signed=False)


def _abi_encode_packed_address(addr: str) -> bytes:
    hx = addr.lower().replace("0x", "")
    b = binascii.unhexlify(hx.rjust(40, "0")[-40:])
    return (b"\x00" * 12) + b


def eip712_domain_separator(chain_id: int, verifying_contract: str) -> bytes:
    type_hash = _keccak256(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    name_hash = _keccak256(b"TroutPoutSessionGate")
    ver_hash = _keccak256(b"glacier-smolt-7")
    inner = (
        type_hash
        + name_hash
        + ver_hash
        + _abi_encode_packed_uint256(chain_id)
        + _abi_encode_packed_address(verifying_contract)
    )
    return _keccak256(inner)


def pout_session_struct_hash(draft: PoutSessionDraft) -> bytes:
    type_hash = _keccak256(POUT_SESSION_TYPESTRING.encode())
    inner = (
        type_hash
        + _abi_encode_packed_address(draft.user)
        + _abi_encode_packed_uint256(draft.session_nonce)
        + _abi_encode_packed_uint256(draft.credits_cost)
        + _abi_encode_packed_uint256(draft.effect_id)
        + _abi_encode_packed_uint256(draft.deadline)
    )
    return _keccak256(inner)


def eip712_digest(chain_id: int, contract: str, draft: PoutSessionDraft) -> bytes:
    domain = eip712_domain_separator(chain_id, contract)
    struct_hash = pout_session_struct_hash(draft)
    return _keccak256(b"\x19\x01" + domain + struct_hash)


def sign_session_local(
    private_key_hex: str,
    chain_id: int,
    contract: str,
    draft: PoutSessionDraft,
) -> Tuple[int, bytes, bytes]:
    if Account is None:
        raise RuntimeError("eth-account not installed")
    digest = eip712_digest(chain_id, contract, draft)
    signed = Account._sign_hash(digest, private_key=private_key_hex)
    return signed.v, signed.r, signed.s


def web3_contract(cfg: UltiPoutConfig):
    if Web3 is None or not cfg.trout_pout_address:
        return None
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    return w3.eth.contract(address=Web3.to_checksum_address(cfg.trout_pout_address), abi=TROUT_POUT_ABI)


def fetch_riffle_credits(cfg: UltiPoutConfig, user: str) -> Optional[int]:
    c = web3_contract(cfg)
    if c is None:
        return None
    try:
        return int(c.functions.riffleCredits(Web3.to_checksum_address(user)).call())
    except Exception as exc:  # pragma: no cover
        LOG.warning("riffleCredits call failed: %s", exc)
        return None


def fetch_session_nonce(cfg: UltiPoutConfig, user: str) -> Optional[int]:
    c = web3_contract(cfg)
    if c is None:
        return None
    try:
        return int(c.functions.smoltSessionNonces(Web3.to_checksum_address(user)).call())
    except Exception as exc:  # pragma: no cover
        LOG.warning("smoltSessionNonces call failed: %s", exc)
        return None


def render_cli(image_path: Path, lane: str, out_path: Path, cfg: UltiPoutConfig) -> None:
    lane_e = PoutRenderLane[lane]
    im = Image.open(image_path)
    out = compose_lane_stack(im, lane_e, cfg, seed=hash(image_path.name) % (2**31))
    out.save(out_path)
    LOG.info("Wrote %s", out_path)


class UltiPoutHTTPHandler(BaseHTTPRequestHandler):
    cfg: UltiPoutConfig = UltiPoutConfig.from_env()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = json.dumps({"ok": True, "app": "UltiPout"}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/render":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            lane = PoutRenderLane[payload.get("lane", "TemporalWarp")]
            b64 = payload["image_base64"]
            raw_im = base64.b64decode(b64)
            im = Image.open(io.BytesIO(raw_im))
            out = compose_lane_stack(im, lane, self.cfg, seed=int(time.time()) % (2**31))
            url = image_to_data_url(out)
            body = json.dumps({"data_url": url}).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


def serve_http(cfg: UltiPoutConfig) -> None:
    UltiPoutHTTPHandler.cfg = cfg
    srv = ThreadingHTTPServer((cfg.http_host, cfg.http_port), UltiPoutHTTPHandler)
    LOG.info("UltiPout http://%s:%s", cfg.http_host, cfg.http_port)
    srv.serve_forever()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="UltiPout face veneer CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="Render a still through a lane")
    r.add_argument("input", type=Path)
    r.add_argument("-o", "--output", type=Path, required=True)
    r.add_argument(
        "--lane",
        default="TemporalWarp",
        choices=[x.name for x in PoutRenderLane if x.name != "Unassigned"],
    )
    sub.add_parser("serve", help="Start JSON render micro-server")
    d = sub.add_parser("digest", help="Print EIP-712 session digest hex")
    d.add_argument("user")
    d.add_argument("nonce", type=int)
    d.add_argument("credits", type=int)
    d.add_argument("effect", type=int)
    d.add_argument("deadline", type=int)
    d.add_argument("--contract", required=True)
    q = sub.add_parser("credits", help="Print riffleCredits for address (needs ULTIPOUT_CONTRACT)")
    q.add_argument("user")
    cat = sub.add_parser("catalog-dump", help="Print catalog lane metadata")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = UltiPoutConfig.from_env()
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    ap = build_arg_parser()
    ns = ap.parse_args(list(argv) if argv is not None else None)
    if ns.cmd == "render":
        render_cli(ns.input, ns.lane, ns.output, cfg)
        return 0
    if ns.cmd == "serve":
        serve_http(cfg)
        return 0
    if ns.cmd == "digest":
        draft = PoutSessionDraft(ns.user, ns.nonce, ns.credits, ns.effect, ns.deadline)
        d = eip712_digest(cfg.chain_id, ns.contract, draft)
        print("0x" + d.hex())
        return 0
    if ns.cmd == "credits":
        v = fetch_riffle_credits(cfg, ns.user)
        print(v if v is not None else "unavailable")
        return 0
    if ns.cmd == "catalog-dump":
        blob = json.dumps([globals()[f"_pout_catalog_lane_{i:03d}"]() for i in range(220)], indent=2)
        print(blob)
        return 0
    return 1


def _pout_catalog_lane_000() -> dict[str, float | int | str]:
    """Synthetic veneer lane 000; anchor 86c3cec4; bias 0.8138; kin slot 59."""
