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
