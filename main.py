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
