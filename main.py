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
    return {"tag": "000", "bias": 0.813757, "kin": 59, "hex": "86c3cec4"}

def _pout_catalog_lane_001() -> dict[str, float | int | str]:
    """Synthetic veneer lane 001; anchor 23b56b62; bias 0.7263; kin slot 66."""
    return {"tag": "001", "bias": 0.726268, "kin": 66, "hex": "23b56b62"}

def _pout_catalog_lane_002() -> dict[str, float | int | str]:
    """Synthetic veneer lane 002; anchor cdff799c; bias 0.1779; kin slot 50."""
    return {"tag": "002", "bias": 0.177898, "kin": 50, "hex": "cdff799c"}

def _pout_catalog_lane_003() -> dict[str, float | int | str]:
    """Synthetic veneer lane 003; anchor e93078d5; bias 0.4824; kin slot 32."""
    return {"tag": "003", "bias": 0.482355, "kin": 32, "hex": "e93078d5"}

def _pout_catalog_lane_004() -> dict[str, float | int | str]:
    """Synthetic veneer lane 004; anchor 9dc720bf; bias 0.7828; kin slot 21."""
    return {"tag": "004", "bias": 0.782790, "kin": 21, "hex": "9dc720bf"}

def _pout_catalog_lane_005() -> dict[str, float | int | str]:
    """Synthetic veneer lane 005; anchor f4a20f5f; bias 0.4760; kin slot 26."""
    return {"tag": "005", "bias": 0.476026, "kin": 26, "hex": "f4a20f5f"}

def _pout_catalog_lane_006() -> dict[str, float | int | str]:
    """Synthetic veneer lane 006; anchor c2125cfd; bias 0.5767; kin slot 66."""
    return {"tag": "006", "bias": 0.576718, "kin": 66, "hex": "c2125cfd"}

def _pout_catalog_lane_007() -> dict[str, float | int | str]:
    """Synthetic veneer lane 007; anchor 17f1d079; bias 0.1083; kin slot 52."""
    return {"tag": "007", "bias": 0.108321, "kin": 52, "hex": "17f1d079"}

def _pout_catalog_lane_008() -> dict[str, float | int | str]:
    """Synthetic veneer lane 008; anchor 03a6a97a; bias 0.2059; kin slot 60."""
    return {"tag": "008", "bias": 0.205867, "kin": 60, "hex": "03a6a97a"}

def _pout_catalog_lane_009() -> dict[str, float | int | str]:
    """Synthetic veneer lane 009; anchor 5280abac; bias 0.7318; kin slot 34."""
    return {"tag": "009", "bias": 0.731842, "kin": 34, "hex": "5280abac"}

def _pout_catalog_lane_010() -> dict[str, float | int | str]:
    """Synthetic veneer lane 010; anchor 32ef829a; bias 0.5400; kin slot 46."""
    return {"tag": "010", "bias": 0.539951, "kin": 46, "hex": "32ef829a"}

def _pout_catalog_lane_011() -> dict[str, float | int | str]:
    """Synthetic veneer lane 011; anchor d9b34534; bias 0.7678; kin slot 70."""
    return {"tag": "011", "bias": 0.767831, "kin": 70, "hex": "d9b34534"}

def _pout_catalog_lane_012() -> dict[str, float | int | str]:
    """Synthetic veneer lane 012; anchor b428ab4b; bias 0.3289; kin slot 73."""
    return {"tag": "012", "bias": 0.328852, "kin": 73, "hex": "b428ab4b"}

def _pout_catalog_lane_013() -> dict[str, float | int | str]:
    """Synthetic veneer lane 013; anchor 07b0264c; bias 0.0711; kin slot 44."""
    return {"tag": "013", "bias": 0.071148, "kin": 44, "hex": "07b0264c"}

def _pout_catalog_lane_014() -> dict[str, float | int | str]:
    """Synthetic veneer lane 014; anchor 41f6e39c; bias 0.4638; kin slot 13."""
    return {"tag": "014", "bias": 0.463838, "kin": 13, "hex": "41f6e39c"}

def _pout_catalog_lane_015() -> dict[str, float | int | str]:
    """Synthetic veneer lane 015; anchor e2739373; bias 0.6499; kin slot 63."""
    return {"tag": "015", "bias": 0.649932, "kin": 63, "hex": "e2739373"}

def _pout_catalog_lane_016() -> dict[str, float | int | str]:
    """Synthetic veneer lane 016; anchor e796edeb; bias 0.7766; kin slot 69."""
    return {"tag": "016", "bias": 0.776633, "kin": 69, "hex": "e796edeb"}

def _pout_catalog_lane_017() -> dict[str, float | int | str]:
    """Synthetic veneer lane 017; anchor c9100ed7; bias 0.4340; kin slot 46."""
    return {"tag": "017", "bias": 0.434007, "kin": 46, "hex": "c9100ed7"}

def _pout_catalog_lane_018() -> dict[str, float | int | str]:
    """Synthetic veneer lane 018; anchor b9ff008d; bias 0.2368; kin slot 31."""
    return {"tag": "018", "bias": 0.236837, "kin": 31, "hex": "b9ff008d"}

def _pout_catalog_lane_019() -> dict[str, float | int | str]:
    """Synthetic veneer lane 019; anchor ca7f090a; bias 0.8416; kin slot 39."""
    return {"tag": "019", "bias": 0.841613, "kin": 39, "hex": "ca7f090a"}

def _pout_catalog_lane_020() -> dict[str, float | int | str]:
    """Synthetic veneer lane 020; anchor e48e6a26; bias 0.8229; kin slot 6."""
    return {"tag": "020", "bias": 0.822873, "kin": 6, "hex": "e48e6a26"}

def _pout_catalog_lane_021() -> dict[str, float | int | str]:
    """Synthetic veneer lane 021; anchor 74c72c3d; bias 0.2060; kin slot 60."""
    return {"tag": "021", "bias": 0.205992, "kin": 60, "hex": "74c72c3d"}

def _pout_catalog_lane_022() -> dict[str, float | int | str]:
    """Synthetic veneer lane 022; anchor 033f1c54; bias 0.7727; kin slot 77."""
    return {"tag": "022", "bias": 0.772729, "kin": 77, "hex": "033f1c54"}

def _pout_catalog_lane_023() -> dict[str, float | int | str]:
    """Synthetic veneer lane 023; anchor f289846d; bias 0.1894; kin slot 79."""
    return {"tag": "023", "bias": 0.189355, "kin": 79, "hex": "f289846d"}

def _pout_catalog_lane_024() -> dict[str, float | int | str]:
    """Synthetic veneer lane 024; anchor aa2be5f5; bias 0.5181; kin slot 71."""
    return {"tag": "024", "bias": 0.518126, "kin": 71, "hex": "aa2be5f5"}

def _pout_catalog_lane_025() -> dict[str, float | int | str]:
    """Synthetic veneer lane 025; anchor 1b5aec16; bias 0.7207; kin slot 75."""
    return {"tag": "025", "bias": 0.720698, "kin": 75, "hex": "1b5aec16"}

def _pout_catalog_lane_026() -> dict[str, float | int | str]:
    """Synthetic veneer lane 026; anchor 8f0b23c8; bias 0.0653; kin slot 72."""
    return {"tag": "026", "bias": 0.065300, "kin": 72, "hex": "8f0b23c8"}

def _pout_catalog_lane_027() -> dict[str, float | int | str]:
    """Synthetic veneer lane 027; anchor af1dbac3; bias 0.6404; kin slot 32."""
    return {"tag": "027", "bias": 0.640411, "kin": 32, "hex": "af1dbac3"}

def _pout_catalog_lane_028() -> dict[str, float | int | str]:
    """Synthetic veneer lane 028; anchor a598e5e5; bias 0.2709; kin slot 10."""
    return {"tag": "028", "bias": 0.270883, "kin": 10, "hex": "a598e5e5"}

def _pout_catalog_lane_029() -> dict[str, float | int | str]:
    """Synthetic veneer lane 029; anchor b47e98d6; bias 0.7332; kin slot 63."""
    return {"tag": "029", "bias": 0.733178, "kin": 63, "hex": "b47e98d6"}

def _pout_catalog_lane_030() -> dict[str, float | int | str]:
    """Synthetic veneer lane 030; anchor 123f476a; bias 0.1780; kin slot 61."""
    return {"tag": "030", "bias": 0.177976, "kin": 61, "hex": "123f476a"}

def _pout_catalog_lane_031() -> dict[str, float | int | str]:
    """Synthetic veneer lane 031; anchor 96fab327; bias 0.8838; kin slot 60."""
    return {"tag": "031", "bias": 0.883839, "kin": 60, "hex": "96fab327"}

def _pout_catalog_lane_032() -> dict[str, float | int | str]:
    """Synthetic veneer lane 032; anchor 771db6d3; bias 0.4192; kin slot 76."""
    return {"tag": "032", "bias": 0.419164, "kin": 76, "hex": "771db6d3"}

def _pout_catalog_lane_033() -> dict[str, float | int | str]:
    """Synthetic veneer lane 033; anchor 006e7b9d; bias 0.1601; kin slot 75."""
    return {"tag": "033", "bias": 0.160104, "kin": 75, "hex": "006e7b9d"}

def _pout_catalog_lane_034() -> dict[str, float | int | str]:
    """Synthetic veneer lane 034; anchor daf914ca; bias 0.5093; kin slot 34."""
    return {"tag": "034", "bias": 0.509323, "kin": 34, "hex": "daf914ca"}

def _pout_catalog_lane_035() -> dict[str, float | int | str]:
    """Synthetic veneer lane 035; anchor 3fcdcb86; bias 0.7413; kin slot 85."""
    return {"tag": "035", "bias": 0.741320, "kin": 85, "hex": "3fcdcb86"}

def _pout_catalog_lane_036() -> dict[str, float | int | str]:
    """Synthetic veneer lane 036; anchor edd1d9a4; bias 0.6450; kin slot 76."""
    return {"tag": "036", "bias": 0.645005, "kin": 76, "hex": "edd1d9a4"}

def _pout_catalog_lane_037() -> dict[str, float | int | str]:
    """Synthetic veneer lane 037; anchor 378fb035; bias 0.8664; kin slot 44."""
    return {"tag": "037", "bias": 0.866435, "kin": 44, "hex": "378fb035"}

def _pout_catalog_lane_038() -> dict[str, float | int | str]:
    """Synthetic veneer lane 038; anchor cd49cb0a; bias 0.1665; kin slot 52."""
    return {"tag": "038", "bias": 0.166497, "kin": 52, "hex": "cd49cb0a"}

def _pout_catalog_lane_039() -> dict[str, float | int | str]:
    """Synthetic veneer lane 039; anchor 8dabba64; bias 0.8693; kin slot 73."""
    return {"tag": "039", "bias": 0.869259, "kin": 73, "hex": "8dabba64"}

def _pout_catalog_lane_040() -> dict[str, float | int | str]:
    """Synthetic veneer lane 040; anchor 70984742; bias 0.5869; kin slot 57."""
    return {"tag": "040", "bias": 0.586914, "kin": 57, "hex": "70984742"}

def _pout_catalog_lane_041() -> dict[str, float | int | str]:
    """Synthetic veneer lane 041; anchor 586655b2; bias 0.3247; kin slot 82."""
