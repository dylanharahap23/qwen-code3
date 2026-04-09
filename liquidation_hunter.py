#!/usr/bin/env python3
"""
🔥 BINANCE LIQUIDATION HUNTER - ULTIMATE EDITION v9 (LECTURER'S SARAN LOGIC)
🎯 Integrated: Liquidity Magnet Continuation, OFI Absorption Squeeze, Velocity Decay Reversal
🎯 Priority Ladder: MasterSqueezeRule (-1100) > LiquidityMagnet (-1000) > OFIAbsorption (-950) > VelocityDecay (-900) > EmptyBook (-850)
🎯 Golden Rule: LONG UNTIL SHORT LIQ SWEPT / SHORT UNTIL LONG LIQ SWEPT
🎯 Market Phase Detector: PREP (no trade) | BAIT (caution) | KILL (trade ok)
🎯 Greeks Final Screener: Theta (Prep) | Vega (Bait) | Delta+Gamma (Kill) — 7% Rule
"""

import requests
from datetime import datetime
import urllib3
import numpy as np
from typing import Optional, Dict, List, Tuple, Any, Literal
import time
import json
import threading
import websocket
import os
from collections import deque
from dataclasses import dataclass, field

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= CONFIG =================
IS_KOYEB = os.getenv('KOYEB', 'false').lower() == 'true'

WMI_STRONG_THRESHOLD = 50
WMI_MODERATE_THRESHOLD = 20
ENERGY_RATIO_THRESHOLD = 10.0
VACUUM_VOLUME_THRESHOLD = 0.1
DEAD_AGG_THRESHOLD = 0.2
DEAD_FLOW_THRESHOLD = 0.5
LIQ_PROXIMITY_THRESHOLD = 0.5
OVERBOUGHT_RSI = 80
OI_DELTA_THRESHOLD = 0.5
FLUSH_ZONE_THRESHOLD = 0.5
FLUSH_AGG_THRESHOLD = 0.2
VOTE_THRESHOLD = 0.65
TARGET_MOVE_PCT = 6.0
STOP_LOSS_PCT = 8.0
MIN_ENERGY_TO_MOVE = 0.5
ENERGY_ZERO_THRESHOLD = 0.01
EXTREME_OVERSOLD_RSI = 15
EXTREME_OVERSOLD_STOCH = 15
PANIC_DROP_THRESHOLD = -8.0
MAX_LATENCY_MS = 500
PERSISTENCE_THRESHOLD = 2.0
TRADES_MAXLEN = 200 if IS_KOYEB else 1000
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

SIGNAL_PERSISTENCE_SEC = 2.5
LATENCY_MS_ESTIMATE = 150
LIQ_SQUEEZE_THRESHOLD = 1.5
LOW_CAP_VOLUME_THRESHOLD = 100000

# ================= STABILITY FILTER GLOBAL =================
LAST_BIAS = None
LAST_BIAS_TIME = 0

# ================= KILL-ZONE FLIP TRAP DETECTOR =================
_kill_direction_history: Dict[str, List[Tuple[float, str]]] = {}

# ================= TIME DECAY GLOBAL =================
LAST_SIGNAL = None
LAST_SIGNAL_TIME = 0

# ================= MARKET PHASE DETECTOR =================

PhaseType = Literal["PREP", "BAIT", "KILL", "UNKNOWN"]
BiasType = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass
class PhaseResult:
    """Hasil deteksi fase market."""
    phase: PhaseType
    override: bool
    bias: BiasType
    confidence: str  # "BLOCK" | "CAUTION" | "PASS"
    priority: int  # negatif = override kuat
    reason: str
    sub_signals: dict = field(default_factory=dict)


def _check_prep_phase(data: dict) -> Optional[PhaseResult]:
    """
    Deteksi PREP phase = market lagi ngumpulin liquidity sebelum bergerak.
    Ciri khas: flat, sepi, buyer masuk pelan, tidak ada seller.

    Rule (semua harus terpenuhi):
      • |change_5m|  < 0.5%      → market flat
      • volume_ratio < 0.7       → volume kecil / sepi
      • down_energy  == 0        → tidak ada tekanan jual
      • ofi_bias     == "LONG"   → buyer masuk pelan-pelan
      • 40 < rsi6 < 65           → RSI netral, bukan ekstrem
    """
    change_5m = abs(data.get("change_5m", 0.0))
    volume_ratio = data.get("volume_ratio", 1.0)
    down_energy = data.get("down_energy", 0.0)
    ofi_bias = data.get("ofi_bias", "NEUTRAL")
    rsi6 = data.get("rsi6", 50.0)

    conditions = {
        "flat_market": change_5m < 0.5,
        "low_volume": volume_ratio < 0.7,
        "no_seller": down_energy == 0,
        "buyer_creeping": ofi_bias == "LONG",
        "rsi_neutral": 40 < rsi6 < 65,
    }

    triggered = {k: v for k, v in conditions.items() if v}

    if len(triggered) < 5:
        return None

    reason = (
        f"NO TRADE ZONE — Pre-manipulation accumulation detected. "
        f"Market flat ({change_5m:.2f}%), volume low ({volume_ratio:.2f}x), "
        f"down_energy=0, OFI=LONG, RSI neutral ({rsi6:.1f}). "
        f"Binance lagi ngumpulin korban, belum ada arah."
    )

    return PhaseResult(
        phase="PREP",
        override=True,
        bias="NEUTRAL",
        confidence="BLOCK",
        priority=-20000,  # LECTURER FIX 1: Lebih tinggi dari semua detector lain
        reason=reason,
        sub_signals=triggered,
    )


def _check_bait_phase(data: dict) -> Optional[PhaseResult]:
    """
    Deteksi BAIT phase = market bikin gerakan kecil palsu untuk jebak trader.
    Ciri khas: ada gerakan, tapi volume masih rendah dan OBV tidak konfirmasi.

    Rule (minimal 3 dari 6 harus terpenuhi):
      • 0.5 <= |change_5m| < 2.0  → ada gerakan tapi kecil
      • volume_ratio < 0.8        → volume tidak mendukung
      • obv_trend == "NEUTRAL"    → OBV tidak konfirmasi arah
      • up_energy > 0 AND down_energy == 0 (atau sebaliknya) → energy satu arah tapi sepi
      • RSI multi-TF divergence (RSI 1m > 70 & RSI 5m < 30, atau sebaliknya)
      • ask_wall_dominant (ask_slope / bid_slope > 5.0)
    
    TAMBAHAN FIX 1: Deteksi BAIT untuk move besar (>2%) dengan volume sangat rendah
    """
    change_5m = abs(data.get("change_5m", 0.0))
    volume_ratio = data.get("volume_ratio", 1.0)
    obv_trend = data.get("obv_trend", "NEUTRAL")
    up_energy = data.get("up_energy", 0.0)
    down_energy = data.get("down_energy", 0.0)
    
    # NEW: RSI multi-TF divergence = sinyal BAIT tambahan
    rsi6 = data.get("rsi6", 50.0)
    rsi6_5m = data.get("rsi6_5m", 50.0)
    rsi_tf_divergence = (rsi6 > 70 and rsi6_5m < 30) or (rsi6 < 30 and rsi6_5m > 70)
    
    # NEW: ask wall dominance = sinyal BAIT tambahan
    ask_slope = data.get("ask_slope", 0)
    bid_slope = data.get("bid_slope", 1)
    ask_wall_dominant = (bid_slope > 0 and ask_slope / bid_slope > 5.0)

    one_sided_energy = (up_energy > 0 and down_energy == 0) or \
                       (down_energy > 0 and up_energy == 0)

    conditions = {
        "small_move": 0.5 <= change_5m < 2.0,
        "low_volume": volume_ratio < 0.8,
        "obv_not_confirming": obv_trend == "NEUTRAL",
        "one_sided_energy": one_sided_energy,
        "rsi_tf_divergence": rsi_tf_divergence,   # NEW
        "ask_wall_dominant": ask_wall_dominant,    # NEW
    }

    triggered = {k: v for k, v in conditions.items() if v}

    if len(triggered) >= 3:
        reason = (
            f"BAIT PHASE — Gerakan kecil ({change_5m:.2f}%) tanpa volume (ratio={volume_ratio:.2f}x). "
            f"OBV tidak konfirmasi ({obv_trend}). Kemungkinan fake move untuk jebak posisi. "
            f"Signals: {list(triggered.keys())}"
        )

        return PhaseResult(
            phase="BAIT",
            override=False,
            bias="NEUTRAL",
            confidence="CAUTION",
            priority=-500,
            reason=reason,
            sub_signals=triggered,
        )
    
    # 🔥 FIX 1: Tambahan untuk move besar tapi volume kering (BAIT)
    # Kondisi: move 2-5%, volume <0.6x, OBV netral, one-sided energy
    if (abs(data.get("change_5m", 0.0)) >= 2.0 and abs(data.get("change_5m", 0.0)) < 5.0 and
        volume_ratio < 0.6 and
        obv_trend == "NEUTRAL" and
        (up_energy == 0 or down_energy == 0)):
        # Ini adalah bait dengan move besar
        return PhaseResult(
            phase="BAIT",
            override=False,
            bias="NEUTRAL",
            confidence="CAUTION",
            priority=-500,
            reason=f"BAIT PHASE (large move) — Gerakan {abs(data.get('change_5m', 0.0)):.1f}% tanpa volume ({volume_ratio:.2f}x), OBV netral → fake move",
            sub_signals={"large_fake_move": True, "low_volume": True}
        )

    return None


def _check_kill_phase(data: dict) -> Optional[PhaseResult]:
    """
    Deteksi KILL phase = market sudah siap bergerak besar, arah jelas.
    Ciri khas: OBV ekstrem, volume tinggi, ada energy besar satu arah.

    Rule (minimal 3 dari 5 harus terpenuhi):
      • |change_5m|  >= 0.8                   → ada momentum nyata
      • volume_ratio >= 0.9                   → volume mendukung
      • obv_trend in EXTREME                  → OBV konfirmasi kuat
      • max(up_energy, down_energy) >= 2.0    → energy besar
      • rsi6 > 70 atau rsi6 < 30              → momentum ekstrem
    """
    change_5m = abs(data.get("change_5m", 0.0))
    volume_ratio = data.get("volume_ratio", 1.0)
    obv_trend = data.get("obv_trend", "NEUTRAL")
    up_energy = data.get("up_energy", 0.0)
    down_energy = data.get("down_energy", 0.0)
    rsi6 = data.get("rsi6", 50.0)

    obv_extreme = obv_trend in ("POSITIVE_EXTREME", "NEGATIVE_EXTREME",
                                "POSITIVE", "NEGATIVE")
    max_energy = max(up_energy, down_energy)

    conditions = {
        "strong_move": change_5m >= 0.8,
        "good_volume": volume_ratio >= 0.9,
        "obv_extreme": obv_extreme,
        "high_energy": max_energy >= 2.0,
        "rsi_momentum": rsi6 > 70 or rsi6 < 30,
    }

    triggered = {k: v for k, v in conditions.items() if v}

    if len(triggered) < 3:
        return None

    reason = (
        f"KILL PHASE — Move nyata ({change_5m:.2f}%), volume={volume_ratio:.2f}x, "
        f"OBV={obv_trend}, energy={max_energy:.2f}, RSI={rsi6:.1f}. "
        f"Market sudah siap bergerak besar. Signals: {list(triggered.keys())}"
    )

    return PhaseResult(
        phase="KILL",
        override=False,
        bias="NEUTRAL",
        confidence="PASS",
        priority=0,
        reason=reason,
        sub_signals=triggered,
    )


# ================= KILL-ZONE FLIP TRAP DETECTOR FUNCTIONS =================

def _track_kill_direction(symbol: str, kill_direction: str) -> None:
    """Catat history greeks_kill_direction per symbol."""
    now = time.time()
    if symbol not in _kill_direction_history:
        _kill_direction_history[symbol] = []
    
    history = _kill_direction_history[symbol]
    history.append((now, kill_direction))
    
    # Simpan hanya 60 detik terakhir
    _kill_direction_history[symbol] = [
        (t, d) for t, d in history if now - t <= 60
    ]


def _check_kill_direction_stability(symbol: str) -> dict:
    """
    Cek apakah greeks_kill_direction stabil dalam 60 detik terakhir.
    
    Jika kill_direction sering flip → BOTH_POSSIBLE → DANGER ZONE.
    Jika kill_direction konsisten → aman untuk entry.
    
    Returns:
        stable: bool — arah konsisten?
        flip_count: int — berapa kali flip?
        dominant_direction: str — arah yang paling sering muncul
        danger: bool — apakah ini Kill-Zone Flip Trap?
    """
    history = _kill_direction_history.get(symbol, [])
    
    if len(history) < 2:
        return {
            "stable": True,
            "flip_count": 0,
            "dominant_direction": "UNKNOWN",
            "danger": False,
            "reason": "Insufficient history — assume stable"
        }
    
    directions = [d for _, d in history]
    
    # Hitung flip
    flip_count = sum(
        1 for i in range(1, len(directions))
        if directions[i] != directions[i-1]
    )
    
    # Dominant direction
    long_count = directions.count("LONG")
    short_count = directions.count("SHORT")
    both_count = directions.count("BOTH_POSSIBLE") + directions.count("BOTH")
    
    if long_count > short_count and long_count > both_count:
        dominant = "LONG"
    elif short_count > long_count and short_count > both_count:
        dominant = "SHORT"
    else:
        dominant = "UNSTABLE"
    
    # Bahaya jika: banyak flip atau dominant tidak jelas
    danger = flip_count >= 2 or dominant == "UNSTABLE" or both_count >= 2
    
    return {
        "stable": not danger,
        "flip_count": flip_count,
        "dominant_direction": dominant,
        "danger": danger,
        "reason": (
            f"Kill direction flipped {flip_count}x in last 60s. "
            f"Dominant: {dominant}. {'⚠️ FLIP TRAP DETECTED' if danger else '✅ Stable'}"
        )
    }


def _check_dual_liq_trap(data: dict) -> dict:
    """
    Deteksi Kill-Zone Flip Trap: kedua sisi likuiditas dekat,
    gamma belum executing, who_dies_first masih ambigu.
    
    Ini kondisi paling berbahaya — bandar belum commit arah.
    Entry apapun di sini = masuk jebakan.
    
    Trigger jika minimal 3 dari 5:
    - greeks_who_dies_first == "BOTH_POSSIBLE"
    - greeks_liq_7pct == "BOTH"
    - greeks_gamma_executing == False
    - short_liq < 3.0% DAN long_liq < 5.0%  (dua-duanya dekat)
    - greeks_kill_speed < 2.0  (belum ada momentum kill)
    """
    who_dies = data.get("greeks_who_dies_first", "")
    liq_7pct = data.get("greeks_liq_7pct", "")
    gamma_executing = data.get("greeks_gamma_executing", False)
    short_liq = data.get("short_liq", 99.0)
    long_liq = data.get("long_liq", 99.0)
    kill_speed = abs(data.get("greeks_kill_speed", 0))
    
    conditions = {
        "both_possible": who_dies in ("BOTH_POSSIBLE", "BOTH"),
        "liq_7pct_both": liq_7pct in ("BOTH", "BOTH_POSSIBLE"),
        "gamma_not_executing": not gamma_executing,
        "dual_liq_close": short_liq < 3.0 and long_liq < 5.0,
        "kill_speed_low": kill_speed < 2.0,
    }
    
    triggered = {k: v for k, v in conditions.items() if v}
    score = len(triggered)
    
    is_trap = score >= 3
    
    # ============================================================
    # 🔥 TAMBAHAN: DOUBLE KILL PROXIMITY ANALYSIS
    # Jika dual liq trap aktif, tentukan "first move direction"
    # berdasarkan mana yang lebih dekat
    # ============================================================
    first_move = "UNKNOWN"
    if is_trap:
        if short_liq < long_liq and short_liq < 3.0:
            first_move = "UP"   # short swept dulu
        elif long_liq < short_liq and long_liq < 3.0:
            first_move = "DOWN" # long swept dulu
        else:
            first_move = "UNKNOWN"  # terlalu simetris
    
    return {
        "dual_liq_trap": is_trap,
        "trap_score": score,
        "triggered_conditions": list(triggered.keys()),
        "first_move_direction": first_move,  # NEW
        "reason": (
            f"DUAL LIQ TRAP: {score}/5 conditions — "
            f"who_dies={who_dies}, liq_7pct={liq_7pct}, "
            f"gamma_exec={gamma_executing}, "
            f"short_liq={short_liq:.1f}%, long_liq={long_liq:.1f}%, "
            f"kill_speed={kill_speed:.2f}, first_move={first_move}. "
            f"{'🚨 SKIP ENTRY — direction not committed' if is_trap else '✅ OK'}"
        )
    }


def _check_bias_kill_conflict(data: dict, final_bias: str) -> dict:
    """
    KASUS DEGO: bias = LONG tapi greeks_kill_direction = SHORT.
    
    Ini adalah sinyal bahwa system salah interpret Greeks.
    greeks_kill_direction SHORT artinya LONG TRADERS YANG AKAN MATI.
    Jadi entry LONG = masuk ke zona yang akan dieksekusi.
    
    Block entry jika:
    - final_bias == "LONG" tapi greeks_kill_direction == "SHORT"
    - final_bias == "SHORT" tapi greeks_kill_direction == "LONG"  
    - Dan: greeks_gamma_executing == False (belum start, masih bisa flip)
    """
    kill_dir = data.get("greeks_kill_direction", "")
    gamma_executing = data.get("greeks_gamma_executing", False)
    who_dies = data.get("greeks_who_dies_first", "")
    
    # Mapping: kill_direction SHORT = LONG traders mati = jangan LONG
    conflict = False
    conflict_reason = ""
    
    if final_bias == "LONG" and kill_dir == "SHORT" and not gamma_executing:
        conflict = True
        conflict_reason = (
            f"BIAS CONFLICT: bias=LONG tapi kill_direction=SHORT "
            f"(LONG traders akan dieksekusi). gamma_executing=False → "
            f"belum start, tapi arah sudah jelas. BLOCK LONG entry."
        )
    elif final_bias == "SHORT" and kill_dir == "LONG" and not gamma_executing:
        conflict = True
        conflict_reason = (
            f"BIAS CONFLICT: bias=SHORT tapi kill_direction=LONG "
            f"(SHORT traders akan dieksekusi). gamma_executing=False → "
            f"belum start, tapi arah sudah jelas. BLOCK SHORT entry."
        )
    
    # Exception: jika who_dies sudah confirmed (bukan BOTH_POSSIBLE), 
    # dan kill_dir align dengan who_dies → boleh masuk sesuai kill_dir
    if conflict and who_dies not in ("BOTH_POSSIBLE", "BOTH", ""):
        # who_dies confirmed → override bias ke arah kill yang benar
        correct_bias = "SHORT" if kill_dir == "SHORT" else "LONG"
        conflict_reason += f" → Correct bias should be: {correct_bias}"
    
    return {
        "has_conflict": conflict,
        "correct_direction": kill_dir if conflict else final_bias,
        "reason": conflict_reason if conflict else "No bias-kill conflict"
    }


# ========== NEW DETECTORS FROM LECTURER FEEDBACK ==========

class BullishOrderFlowDivergence:
    """
    Detector: Bullish Order Flow Divergence (Distribution Trap)
    
    Kondisi:
    - agg > 0.75 (atau ofi_bias == "LONG" dengan strength > 0.7)
    - change_5m < -0.3% (harga turun atau flat)
    - volume_ratio < 0.8 (volume rendah – tipikal distribusi diam-diam)
    - greeks_kill_direction != "LONG" (opsional, untuk konfirmasi)
    
    Priority: -1101 (antara -1102 dan -1100)
    Bias: SHORT
    """
    @staticmethod
    def detect(agg: float, ofi_bias: str, ofi_strength: float,
               change_5m: float, volume_ratio: float,
               greeks_kill_direction: str) -> Dict:
        # Divergensi: order flow bullish tapi harga turun
        if (agg > 0.75 and change_5m < -0.3 and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Bullish order flow divergence: agg={agg:.2f} (buy dominant) tapi price turun {change_5m:.1f}%, volume rendah {volume_ratio:.2f}x → smart money distributing, forced SHORT",
                "priority": -1101
            }
        # Cek juga dengan ofi_bias
        if (ofi_bias == "LONG" and ofi_strength > 0.7 and change_5m < -0.3 and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Bullish OFI divergence: ofi_bias={ofi_bias} (strength={ofi_strength:.2f}) tapi price turun {change_5m:.1f}%, volume rendah {volume_ratio:.2f}x → smart money distributing, forced SHORT",
                "priority": -1101
            }
        return {"override": False}


class ExtremeShortLiqSqueezeOverride:
    """
    Detector: Extreme Short Liquidity Squeeze Override (Priority -1103)
    
    Kondisi:
    - short_dist < 1.5% (short liq super dekat)
    - short_dist < long_dist (short lebih dekat dari long)
    - agg > 0.6 atau up_energy > 0.1 (buy pressure)
    - down_energy < 0.01 (tidak ada resistance di atas)
    - change_5m > 0 (harga sedang naik)
    
    Priority: -1103 (tertinggi di priority ladder)
    Bias: LONG
    
    Logika: Short liq super dekat + buy pressure + tidak ada resistance → 
    HFT akan sweep short stops terlepas dari RSI/vega
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, agg: float, 
               down_energy: float, change_5m: float, up_energy: float = 0,
               ofi_bias: str = "NEUTRAL", ofi_strength: float = 0.0) -> Dict:
        # 🔥 GUARD: Jangan paksa LONG jika bearish confluence (agg sangat rendah + OFI SHORT)
        if agg < 0.3 and ofi_bias == "SHORT" and ofi_strength > 0.6:
            return {"override": False}
        
        # Short liq super dekat + buy pressure + tidak ada resistance
        if (short_dist < 1.5 and 
            short_dist < long_dist and
            down_energy < 0.01 and      # tidak ada seller di book
            (agg > 0.6 or up_energy > 0.1) and  # ada buy pressure
            change_5m > 0):             # harga naik
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"EXTREME SHORT LIQ SQUEEZE: short liq {short_dist:.2f}% super close, down_energy={down_energy:.3f} (no sellers), agg={agg:.2f}, price up {change_5m:.1f}% → HFT will sweep short stops regardless of RSI/volume",
                "priority": -1103
            }
        return {"override": False}


class ShortLiqSuperCloseOverride:
    """
    Detector: Short Liquidity Super Close Override (Priority -1104)
    
    Kondisi:
    - short_dist < 1.5% (short liq super dekat)
    - short_dist < long_dist (short lebih dekat)
    - down_energy < 0.01 (tidak ada seller resistance)
    - kill_direction == "LONG" (Greeks konfirmasi arah LONG)
    - change_5m > 0 (harga sudah naik)
    
    Priority: -1104 (lebih tinggi dari ExtremeShortLiqSqueezeOverride)
    Bias: LONG
    
    Logika: Ketika short liq super dekat + no sellers + Greeks konfirmasi LONG,
    paksa LONG tanpa peduli BAIT phase atau indikator lain.
    Ini adalah kondisi squeeze paling kuat.
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, down_energy: float,
               kill_direction: str, change_5m: float, agg: float,
               ofi_bias: str = "NEUTRAL", ofi_strength: float = 0.0) -> Dict:
        # 🔥 GUARD: Jangan paksa LONG jika bearish confluence (agg sangat rendah + OFI SHORT)
        if agg < 0.3 and ofi_bias == "SHORT" and ofi_strength > 0.6:
            return {"override": False}
        
        if (short_dist < 1.5 and 
            short_dist < long_dist and
            down_energy < 0.01 and
            kill_direction == "LONG" and
            change_5m > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"SHORT LIQ SUPER CLOSE OVERRIDE: short_liq={short_dist:.2f}%, kill_dir={kill_direction}, down_energy=0 → forced LONG even in BAIT phase",
                "priority": -1104
            }
        return {"override": False}


class LowVolumeOverboughtSqueeze:
    """
    Detector: Low Volume Overbought Squeeze (Priority -1105)
    
    Kondisi:
    - volume_ratio < 0.6 (volume sangat rendah)
    - rsi6_5m > 75 (overbought di 5m)
    - short_dist < 2.0 (short liq dekat)
    - down_energy < 0.01 (tidak ada seller)
    - agg > 0.5 (buy pressure ada)
    - change_5m > 0 (harga naik)
    
    Priority: -1105 (PALING TINGGI - prioritas absolut)
    Bias: LONG
    
    Logika: Kasus khusus di mana overbought + volume rendah biasanya berarti reversal,
    TAPI karena short liq dekat + no sellers, ini justru squeeze continuation.
    HFT menggunakan volume rendah untuk push harga ke short liq tanpa resistance.
    """
    @staticmethod
    def detect(volume_ratio: float, rsi6_5m: float, short_dist: float,
               down_energy: float, agg: float, change_5m: float) -> Dict:
        if (volume_ratio < 0.6 and 
            rsi6_5m > 75 and 
            short_dist < 2.0 and 
            down_energy < 0.01 and 
            agg > 0.5 and 
            change_5m > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"LOW VOLUME OVERBOUGHT SQUEEZE: vol={volume_ratio:.2f}x, RSI5m={rsi6_5m:.1f} overbought, short_liq={short_dist:.2f}%, no sellers → squeeze continuation, NOT reversal",
                "priority": -1105
            }
        return {"override": False}


class EmptyBookSqueezeContinuation:
    """
    Detector: Empty Book Squeeze Continuation (Priority -1102)
    
    Kondisi:
    - up_energy == 0 AND down_energy == 0 (order book kosong dua sisi)
    - short_dist < 1.5% OR long_dist < 1.5% (target likuiditas dekat)
    - abs(change_5m) > 0.5 (harga sudah bergerak)
    - agg > 0.6 (ada agresi)
    
    Priority: -1102
    Bias: LONG jika short_dist < long_dist, else SHORT
    
    Logika: Order book kosong dua sisi + target dekat + harga bergerak → 
    vacuum ke arah target likuiditas
    """
    @staticmethod
    def detect(up_energy: float, down_energy: float, short_dist: float,
               long_dist: float, change_5m: float, agg: float) -> Dict:
        # Order book kosong dua sisi + target dekat + harga bergerak
        if (up_energy == 0 and down_energy == 0 and
            (short_dist < 1.5 or long_dist < 1.5) and
            abs(change_5m) > 0.5 and
            agg > 0.6):
            bias = "LONG" if short_dist < long_dist else "SHORT"
            return {
                "override": True,
                "bias": bias,
                "reason": f"EMPTY BOOK SQUEEZE: no orders on both sides, liq target {short_dist if bias=='LONG' else long_dist:.2f}% close, price moving → vacuum to {bias}",
                "priority": -1102
            }
        return {"override": False}


class KillLiquidityConflict:
    """
    Detector: Kill Direction vs Liquidity Proximity Conflict
    
    Kondisi:
    - short_liq < long_liq (short lebih dekat) DAN greeks_kill_direction == "SHORT"
    - ATAU long_liq < short_liq DAN greeks_kill_direction == "LONG"
    
    Jika konflik, ikuti greeks_kill_direction (karena Greeks lebih akurat untuk jangka pendek)
    TAPI turunkan confidence atau tambahkan warning.
    
    Priority: -1100 (di atas Vega spike)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               kill_dir: str, gamma_executing: bool) -> Dict:
        if kill_dir == "SHORT" and short_dist < long_dist and short_dist < 3.0:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"KILL-LIQUIDITY CONFLICT: kill_dir={kill_dir} tapi short liq={short_dist:.2f}% lebih dekat. Greeks override liquidity → SHORT",
                "priority": -1100
            }
        if kill_dir == "LONG" and long_dist < short_dist and long_dist < 3.0:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"KILL-LIQUIDITY CONFLICT: kill_dir={kill_dir} tapi long liq={long_dist:.2f}% lebih dekat. Greeks override liquidity → LONG",
                "priority": -1100
            }
        return {"override": False}


class BlowOffTopShortLiqTrap:
    """
    Detector: Blow-Off Top Short Liquidity Trap (Priority -1102)
    
    Kondisi:
    - change_5m > 1.5% (harga sudah naik signifikan)
    - short_dist < 2.5% (short liquidity dekat)
    - rsi6_5m > 85 (overbought ekstrem di timeframe 5m)
    - volume_ratio < 0.8 (volume rendah – distribusi diam-diam)
    - ofi_bias == "SHORT" (order flow institusi SHORT)
    - agg > 0.6 (agresifitas buy tinggi – bisa jadi spoofing)
    
    Priority: -1102 (lebih tinggi dari VEGA-KILL CONFLICT -9991.5)
    Bias: SHORT
    """
    @staticmethod
    def detect(change_5m: float, short_dist: float, rsi6_5m: float,
               volume_ratio: float, ofi_bias: str, agg: float) -> Dict:
        # Blow-off top: harga sudah naik, short liq dekat, tapi overbought ekstrem
        if (change_5m > 1.5 and
            short_dist < 2.5 and
            rsi6_5m > 85 and
            volume_ratio < 0.8 and
            ofi_bias == "SHORT" and
            agg > 0.6):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"BLOW-OFF TOP TRAP: price up {change_5m:.1f}%, short liq {short_dist:.2f}% dekat, tapi RSI5m {rsi6_5m:.1f} overbought extreme, OFI SHORT {ofi_bias}, agg={agg:.2f} → distribution at peak, dump incoming",
                "priority": -1102
            }
        return {"override": False}


class PhantomBuyEnergyTrap:
    """
    🔥 BSBUSDT PATTERN: up_energy besar tapi ask_slope jauh lebih besar
    
    HFT spoof up_energy dengan bid order besar di order book,
    tapi pasang ask wall jauh lebih besar di atas.
    
    Result: sistem lihat up_energy tinggi → pikir LONG kuat
    Kenyataan: ask wall akan menyerap semua buy → dump
    
    Deteksi: ask_slope > bid_slope * RATIO dan up_energy > 1.0
    tapi change_5m flat/negatif = energy tidak bisa tembus ask wall
    
    Priority: -1101 (di atas MasterSqueezeRule)
    """
    @staticmethod
    def detect(ask_slope: float, bid_slope: float,
               up_energy: float, down_energy: float,
               change_5m: float, volume_ratio: float,
               funding_rate: float,
               long_liq: float, short_liq: float) -> Dict:
        
        if bid_slope <= 0:
            return {"override": False}
        
        ask_bid_ratio = ask_slope / bid_slope
        
        # Ask wall ekstrem + up_energy ada tapi harga tidak naik = phantom
        if (ask_bid_ratio > 50.0 and          # ask wall 50x lebih tebal
            up_energy > 1.0 and               # ada "buy energy" (spoofed)
            down_energy < 0.1 and             # tidak ada seller nyata
            change_5m < 1.0 and              # harga tidak naik meski energy ada
            volume_ratio < 0.8):             # volume rendah = spoof mudah
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"PHANTOM BUY ENERGY TRAP: ask_slope={ask_slope:.0f} vs "
                    f"bid={bid_slope:.0f} (ratio {ask_bid_ratio:.0f}x), "
                    f"up_energy={up_energy:.2f} tapi harga cuma {change_5m:.1f}% "
                    f"→ buy energy palsu (spoof bid), ask wall raksasa akan dump"
                ),
                "priority": -1101
            }
        
        # Versi moderat: ratio > 20x + funding negatif (double trap)
        if (ask_bid_ratio > 20.0 and
            up_energy > 2.0 and
            change_5m < 0.5 and
            funding_rate is not None and
            funding_rate < -0.0001 and        # funding negatif = sistem pikir LONG
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"PHANTOM BUY ENERGY + FUNDING BAIT: ask/bid={ask_bid_ratio:.0f}x, "
                    f"funding={funding_rate:.5f} (crowded short bait), "
                    f"up_energy={up_energy:.2f} spoof → sistem dipancing LONG, "
                    f"kenyataannya ask wall dump"
                ),
                "priority": -1101
            }
        
        return {"override": False}


class DipThenRipSweep:
    """
    🔥 TRUUSDT PATTERN: long_liq sangat dekat = sweep victim, bukan dump target
    
    Ketika long_liq < 1.0% dan short_liq jauh (> 2x jarak long_liq):
    HFT strategy = Dip Then Rip:
    1. Turun sedikit (0.67%) untuk sweep long stop
    2. Setelah long stop habis, tidak ada yang jual lagi
    3. Pump ke short liq (3.06%) untuk squeeze short
    
    Sinyal konfirmasi:
    - agg > 0.6 (mayoritas buy = retail sudah positioning LONG)
    - up_energy > down_energy (buy pressure lebih kuat)
    - RSI oversold (sudah turun cukup = bouncing point)
    - bid_slope > ask_slope (buy wall lebih tebal = support di bawah)
    
    KUNCI: OBV NEGATIVE_EXTREME tidak relevan di sini karena
    sweep jangka pendek (< 1%) + pump jangka menengah > 3%
    
    Priority: -1103 (di atas funding ban)
    """
    @staticmethod
    def detect(long_liq: float, short_liq: float,
               agg: float, up_energy: float, down_energy: float,
               rsi6: float, bid_slope: float, ask_slope: float,
               volume_ratio: float, change_5m: float,
               obv_trend: str) -> Dict:
        
        # Core: long_liq sangat dekat tapi short_liq jauh lebih besar
        if long_liq <= 0:
            return {"override": False}
            
        liq_ratio = short_liq / long_liq  # TRUUSDT: 3.06/0.67 = 4.56x
        
        if (long_liq < 1.5 and              # long liq sangat dekat
            liq_ratio > 2.5 and             # short liq jauh lebih besar
            agg > 0.55 and                  # mayoritas buy
            up_energy > down_energy and     # buy pressure lebih kuat
            down_energy < 0.1 and           # hampir tidak ada seller
            rsi6 < 40):                     # oversold = bouncing point
            
            # Konfirmasi tambahan: bid wall lebih tebal
            bid_stronger = (bid_slope > 0 and 
                           ask_slope > 0 and 
                           bid_slope > ask_slope * 0.5)  # bid minimal 50% dari ask
            
            if bid_stronger or volume_ratio > 0.5:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"DIP THEN RIP SWEEP: long_liq={long_liq:.2f}% dekat "
                        f"(sweep victim), short_liq={short_liq:.2f}% jauh "
                        f"(liq_ratio={liq_ratio:.1f}x), agg={agg:.2f} buy, "
                        f"rsi={rsi6:.1f} oversold, down_energy={down_energy:.2f} "
                        f"→ HFT dip {long_liq:.2f}% sweep long stop LALU pump "
                        f"ke short_liq {short_liq:.2f}%"
                    ),
                    "priority": -1103
                }
        
        # Mirror: short_liq sangat dekat = sweep then dump
        if short_liq > 0:
            liq_ratio_short = long_liq / short_liq
            if (short_liq < 1.5 and
                liq_ratio_short > 2.5 and
                agg < 0.45 and
                down_energy > up_energy and
                up_energy < 0.1 and
                rsi6 > 60):
                
                bid_weaker = (bid_slope > 0 and 
                             ask_slope > 0 and 
                             ask_slope > bid_slope * 0.5)
                
                if bid_weaker or volume_ratio > 0.5:
                    return {
                        "override": True,
                        "bias": "SHORT",
                        "reason": (
                            f"RIP THEN DIP SWEEP: short_liq={short_liq:.2f}% dekat "
                            f"(sweep victim), long_liq={long_liq:.2f}% jauh "
                            f"(ratio={liq_ratio_short:.1f}x) "
                            f"→ HFT pump sweep short stop LALU dump"
                        ),
                        "priority": -1103
                    }
        
        return {"override": False}


class VolumeDryUpReversal:
    """
    Detector: Volume Dry-Up Reversal (Priority -1080)
    
    Kondisi:
    - abs(change_5m) > 2.0% (pergerakan harga besar)
    - volume_ratio < 0.6 (volume mengering)
    
    Jika harga naik besar dengan volume kering + RSI overbought → SHORT reversal
    Jika harga turun besar dengan volume kering + RSI oversold → LONG reversal
    
    Priority: -1080
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, rsi6_5m: float) -> Dict:
        # Volume mengering setelah pergerakan besar → reversal
        if abs(change_5m) > 2.0 and volume_ratio < 0.6:
            if change_5m > 0 and rsi6_5m > 75:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Volume dry-up reversal: price up {change_5m:.1f}% with volume {volume_ratio:.2f}x, RSI5m {rsi6_5m:.1f} overbought → reversal down",
                    "priority": -1080
                }
            if change_5m < 0 and rsi6_5m < 25:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Volume dry-up reversal: price down {abs(change_5m):.1f}% with volume {volume_ratio:.2f}x, RSI5m {rsi6_5m:.1f} oversold → reversal up",
                    "priority": -1080
                }
        return {"override": False}


def detect_market_phase(data: dict) -> PhaseResult:
    """
    Fungsi utama. Panggil ini SEBELUM logika bias utama lo.

    Parameter
    ---------
    data : dict
        Dictionary signal lo. Keys yang dipakai:
          change_5m, volume_ratio, down_energy, up_energy,
          ofi_bias, rsi6, obv_trend

    Returns
    -------
    PhaseResult
        .phase      → "PREP" | "BAIT" | "KILL" | "UNKNOWN"
        .override   → True kalau harus di-block (PREP phase)
        .bias       → "NEUTRAL" kalau override, else biarkan bias original
        .confidence → "BLOCK" | "CAUTION" | "PASS"
        .priority   → integer, negatif = override lebih kuat
        .reason     → string penjelasan
        .sub_signals→ dict kondisi yang triggered
    """
    result = _check_prep_phase(data)
    if result:
        return result

    result = _check_bait_phase(data)
    if result:
        return result

    result = _check_kill_phase(data)
    if result:
        return result

    return PhaseResult(
        phase="UNKNOWN",
        override=False,
        bias="NEUTRAL",
        confidence="PASS",
        priority=0,
        reason="Phase tidak terdeteksi. Lanjut logika normal.",
        sub_signals={},
    )


def apply_phase_override(original_result: dict, phase_result: PhaseResult) -> dict:
    """
    Merge phase_result ke result bias lo yang sudah ada.

    Parameter
    ---------
    original_result : dict
        Output dict dari logika bias lo yang sudah jalan
        (minimal punya keys: "bias", "reason", "confidence", "priority_level")

    phase_result : PhaseResult
        Dari detect_market_phase()

    Returns
    -------
    dict
        original_result yang sudah di-patch dengan info fase
    """
    result = original_result.copy()

    result["market_phase"] = phase_result.phase
    result["phase_sub_signals"] = phase_result.sub_signals
    result["phase_reason"] = phase_result.reason

    if phase_result.override:
        result["bias"] = "NEUTRAL"
        result["confidence"] = "BLOCK"
        result["priority_level"] = phase_result.priority
        result["reason"] = (
            f"[PHASE OVERRIDE — {phase_result.phase}] "
            + phase_result.reason
            + " | Original reason: "
            + original_result.get("reason", "")
        )

    elif phase_result.confidence == "CAUTION":
        result["reason"] = (
            f"[PHASE CAUTION — {phase_result.phase}] "
            + phase_result.reason
            + " | Original: "
            + original_result.get("reason", "")
        )
        if result.get("confidence") == "ABSOLUTE":
            result["confidence"] = "HIGH"

    return result


# ================= GREEKS FINAL SCREENER =================

BiasTypeGreeks = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass
class GreeksResult:
    """Output dari Greeks Final Screener."""
    greeks_bias:       BiasTypeGreeks    # bias final dari Greeks analysis
    override:          bool              # True = paksa override result sebelumnya
    confidence:        str               # selalu "ABSOLUTE"
    priority:          int               # -9999 = tertinggi mutlak
    liq_target:        str               # "LONG_7PCT" | "SHORT_7PCT" | "BOTH" | "NONE"
    kill_direction:    BiasTypeGreeks    # arah yang akan "dibunuh" (posisi yang di-liquidate)
    who_dies_first:    str               # "LONG_TRADERS" | "SHORT_TRADERS" | "UNCLEAR"
    delta_exposure:    float             # estimasi dollar exposure massa
    gamma_acceleration: float            # kecepatan accelerasi kill
    theta_decay_active: bool             # apakah Theta sedang bekerja (Prep phase)
    vega_spike_active:  bool             # apakah Vega/IV spike sedang terjadi (Bait)
    reason:            str
    sub_signals:       dict = field(default_factory=dict)


def _theta_check(data: dict) -> dict:
    """
    Theta = time decay yang menghabiskan extrinsic value trader.
    
    Binance menggunakan sideways untuk:
    1. Membuat trader lelah (stop loss hit karena boredom)
    2. Menguras modal trader yang pakai leverage (funding cost)
    3. Reset bias retail sebelum actual move

    Sinyal Theta aktif:
    - change_5m flat (< 0.3%)
    - volume sangat rendah (< 0.5x)
    - funding rate negatif (retail yang long sedang bayar funding = modal terkuras)
    - RSI netral (40-60) = tidak ada momentum jelas
    - agg netral (0.4-0.6) = tidak ada dominasi buyer/seller
    """
    change_5m    = abs(data.get("change_5m", 0.0))
    volume_ratio = data.get("volume_ratio", 1.0)
    funding_rate = data.get("funding_rate") or 0.0
    rsi6         = data.get("rsi6", 50.0)
    agg          = data.get("agg", 0.5)
    down_energy  = data.get("down_energy", 0.0)
    up_energy    = data.get("up_energy", 0.0)

    theta_conditions = {
        "flat_price":      change_5m < 0.3,
        "low_volume":      volume_ratio < 0.5,
        "funding_draining": funding_rate < -0.001,
        "rsi_neutral":     38 < rsi6 < 62,
        "agg_neutral":     0.38 < agg < 0.62,
        "no_dominant_energy": max(up_energy, down_energy) < 0.5,
    }

    triggered = {k: v for k, v in theta_conditions.items() if v}
    score = len(triggered)

    return {
        "active": score >= 4,
        "score": score,
        "max_score": 6,
        "triggered": triggered,
        "funding_drain_rate": abs(funding_rate) * 100,
        "reason": (
            f"Theta aktif ({score}/6): pasar sideways, funding drain "
            f"{abs(funding_rate)*100:.4f}%/8h, retail terkuras sebelum move besar"
        ) if score >= 4 else "Theta tidak aktif"
    }


def _vega_check(data: dict) -> dict:
    """
    Vega = sensitivitas terhadap perubahan Implied Volatility (IV).
    
    Binance menggunakan IV spike untuk:
    1. Membuat harga kontrak terlihat "mahal" → memancing retail masuk posisi
    2. Menciptakan FOMO: "harga bergerak! harus masuk sekarang!"
    3. Setelah posisi retail terisi → IV collapse → harga balik

    Sinyal Vega/IV spike aktif:
    - change_5m moderate (1-4%) — cukup besar untuk menarik perhatian
    - volume rendah (< 0.7x) — gerakan tidak didukung volume nyata
    - RSI mulai overbought/oversold tapi belum ekstrem
    - OBV tidak konfirmasi arah (NEUTRAL) — smart money tidak ikut
    - OFI tidak searah — order book tidak mendukung arah harga
    """
    change_5m    = abs(data.get("change_5m", 0.0))
    volume_ratio = data.get("volume_ratio", 1.0)
    rsi6         = data.get("rsi6", 50.0)
    obv_trend    = data.get("obv_trend", "NEUTRAL")
    ofi_bias     = data.get("ofi_bias", "NEUTRAL")
    agg          = data.get("agg", 0.5)
    funding_rate = data.get("funding_rate") or 0.0
    up_energy    = data.get("up_energy", 0.0)
    down_energy  = data.get("down_energy", 0.0)

    price_direction = "UP" if data.get("change_5m", 0.0) > 0 else "DOWN"

    vega_conditions = {
        "moderate_move":        1.0 <= change_5m < 4.5,
        "low_supporting_volume": volume_ratio < 0.7,
        "obv_not_confirming":   obv_trend == "NEUTRAL",
        "ofi_diverging":        (
            (price_direction == "UP" and ofi_bias == "SHORT") or
            (price_direction == "DOWN" and ofi_bias == "LONG") or
            ofi_bias == "NEUTRAL"
        ),
        "rsi_moved_not_extreme": (
            (55 < rsi6 < 75 and price_direction == "UP") or
            (25 < rsi6 < 45 and price_direction == "DOWN")
        ),
        "agg_mismatch":         (
            (price_direction == "UP" and agg < 0.6) or
            (price_direction == "DOWN" and agg > 0.4)
        ),
    }

    triggered = {k: v for k, v in vega_conditions.items() if v}
    score = len(triggered)

    if score >= 4:
        bait_direction = price_direction
        trap_direction = "SHORT" if bait_direction == "UP" else "LONG"
        reason = (
            f"Vega/IV spike aktif ({score}/6): fake {bait_direction.lower()} "
            f"{change_5m:.1f}% tanpa konfirmasi volume ({volume_ratio:.2f}x), "
            f"OBV={obv_trend}, OFI={ofi_bias} → retail dipancing {bait_direction}, "
            f"setelah penuh akan dibalik ke {trap_direction}"
        )
    else:
        trap_direction = "NEUTRAL"
        reason = "Vega/IV spike tidak aktif"

    return {
        "active": score >= 4,
        "score": score,
        "max_score": 6,
        "triggered": triggered,
        "bait_direction": price_direction if score >= 4 else "NEUTRAL",
        "trap_direction": trap_direction,
        "reason": reason
    }


def _delta_calculate(data: dict) -> dict:
    """
    Delta = "Share Equivalency" dari posisi massa.
    
    Jika massa melakukan Long banyak dengan Delta 0.50,
    Binance melihat ini sebagai beban besar yang harus "dibersihkan".
    
    Untuk menghancurkan posisi Long dengan Delta tinggi:
    → Binance hanya perlu dump sampai margin call terpicu
    → Dengan leverage 10x: 7-8% drop = LIQUIDASI
    
    Logic:
    - Siapa yang lebih banyak posisi terbuka (proxy: funding rate + OFI)
    - Berapa % price move yang dibutuhkan untuk mencapai liq point
    - short_liq dan long_liq dari result = jarak ke titik kehancuran massa

    Output:
    - who_is_crowded: "LONG" atau "SHORT" (siapa yang lebih banyak posisi)
    - delta_long: estimasi exposure posisi long massa (0-1 scale)
    - delta_short: estimasi exposure posisi short massa (0-1 scale)
    - liq_7pct_touch: siapa yang kena 7% duluan
    """
    short_liq    = data.get("short_liq", 99.0)
    long_liq     = data.get("long_liq", 99.0)
    funding_rate = data.get("funding_rate") or 0.0
    ofi_bias     = data.get("ofi_bias", "NEUTRAL")
    ofi_strength = data.get("ofi_strength", 0.0)
    agg          = data.get("agg", 0.5)
    rsi6         = data.get("rsi6", 50.0)
    volume_ratio = data.get("volume_ratio", 1.0)
    up_energy    = data.get("up_energy", 0.0)
    down_energy  = data.get("down_energy", 0.0)
    change_5m    = data.get("change_5m", 0.0)

    LEVERAGE_KILL_PCT = 7.0

    funding_long_pressure = max(0.0, -funding_rate * 100)
    funding_short_pressure = max(0.0, funding_rate * 100)

    ofi_long_add  = ofi_strength if ofi_bias == "LONG" else 0.0
    ofi_short_add = ofi_strength if ofi_bias == "SHORT" else 0.0

    agg_long_pressure  = max(0.0, agg - 0.5) * 2
    agg_short_pressure = max(0.0, 0.5 - agg) * 2

    rsi_long  = max(0.0, (rsi6 - 50) / 50) if rsi6 > 50 else 0.0
    rsi_short = max(0.0, (50 - rsi6) / 50) if rsi6 < 50 else 0.0

    raw_delta_long  = (
        funding_long_pressure * 0.35 +
        ofi_long_add          * 0.25 +
        agg_long_pressure     * 0.20 +
        rsi_long              * 0.20
    )
    raw_delta_short = (
        funding_short_pressure * 0.35 +
        ofi_short_add          * 0.25 +
        agg_short_pressure     * 0.20 +
        rsi_short              * 0.20
    )

    total = raw_delta_long + raw_delta_short
    if total > 0:
        delta_long  = raw_delta_long / total
        delta_short = raw_delta_short / total
    else:
        delta_long = delta_short = 0.5

    short_dies_at  = short_liq
    long_dies_at   = long_liq

    if short_dies_at <= LEVERAGE_KILL_PCT and long_dies_at > LEVERAGE_KILL_PCT:
        liq_7pct_touch = "SHORT_TRADERS_DIE"
        kill_direction = "LONG"
        who_dies_first = "SHORT_TRADERS"
    elif long_dies_at <= LEVERAGE_KILL_PCT and short_dies_at > LEVERAGE_KILL_PCT:
        liq_7pct_touch = "LONG_TRADERS_DIE"
        kill_direction = "SHORT"
        who_dies_first = "LONG_TRADERS"
    elif short_dies_at <= LEVERAGE_KILL_PCT and long_dies_at <= LEVERAGE_KILL_PCT:
        liq_7pct_touch = "BOTH"
        kill_direction = "SHORT" if delta_long > delta_short else "LONG"
        who_dies_first = "BOTH_POSSIBLE"
    else:
        liq_7pct_touch = "NONE_IN_RANGE"
        kill_direction = "SHORT" if delta_long > delta_short else "LONG"
        who_dies_first = "LONG_TRADERS" if delta_long > delta_short else "SHORT_TRADERS"

    who_is_crowded = "LONG" if delta_long > delta_short else "SHORT"

    # ===== GUARD: Dip-Then-Rip Override =====
    # Jika long_liq sangat dekat (<1%) dan short_liq jauh (>2x),
    # kill_direction seharusnya LONG (sweep long stop dulu, lalu pump)
    # bukan SHORT meski delta_long > delta_short
    if (short_dies_at > long_dies_at * 2.0 and    # short liq jauh lebih besar
        long_dies_at < 1.0 and                     # long liq sangat dekat
        ofi_long_add > ofi_short_add):             # ada buy pressure
        # Override kill direction
        kill_direction = "LONG"  
        who_dies_first = "SHORT_TRADERS"  # setelah sweep long stop, short yang mati
        liq_7pct_touch = "SHORT_TRADERS_DIE"

    liq_pressure_short = max(0.0, 10.0 - short_dies_at) / 10.0
    liq_pressure_long  = max(0.0, 10.0 - long_dies_at)  / 10.0
    delta_exposure = max(liq_pressure_short, liq_pressure_long)

    return {
        "delta_long":      round(delta_long, 3),
        "delta_short":     round(delta_short, 3),
        "who_is_crowded":  who_is_crowded,
        "kill_direction":  kill_direction,
        "who_dies_first":  who_dies_first,
        "liq_7pct_touch":  liq_7pct_touch,
        "delta_exposure":  round(delta_exposure, 3),
        "short_dies_at":   short_dies_at,
        "long_dies_at":    long_dies_at,
        "reason": (
            f"Delta: Long exposure {delta_long:.2f} vs Short {delta_short:.2f}. "
            f"Crowded: {who_is_crowded}. Short dies at +{short_dies_at:.2f}%, "
            f"Long dies at -{long_dies_at:.2f}%. "
            f"7% kill zone: {liq_7pct_touch}. Kill arah: {kill_direction}"
        )
    }


def _gamma_calculate(data: dict, delta_data: dict) -> dict:
    """
    Gamma = akselerasi Delta saat harga mendekati liq point.
    
    Saat harga mendekati titik likuidasi massa:
    → Gamma membuat Delta posisi melonjak
    → Kerugian bertambah LEBIH CEPAT dari penurunan harga
    → Ini "bola salju" — semakin dekat liq, semakin cepat hancur

    Binance tahu ini dan menggunakannya:
    → Cukup bergerak sedikit saat dekat liq zone → Gamma yang lakukan sisanya

    Proxy Gamma dari data kita:
    - Semakin dekat ke liq zone = Gamma semakin tinggi
    - up_energy / down_energy = energi yang tersedia untuk akselerasi
    - Volume spike = Gamma executor (tanda HFT sudah mulai akselerasi)

    Formula approximation:
    Gamma_effect = (1 / distance_to_liq)^2 * available_energy
    """
    short_liq    = data.get("short_liq", 99.0)
    long_liq     = data.get("long_liq", 99.0)
    up_energy    = data.get("up_energy", 0.0)
    down_energy  = data.get("down_energy", 0.0)
    volume_ratio = data.get("volume_ratio", 1.0)
    change_5m    = data.get("change_5m", 0.0)
    latest_volume = data.get("latest_volume", 0.0)
    volume_ma10   = data.get("volume_ma10", 1.0)

    kill_direction = delta_data.get("kill_direction", "NEUTRAL")
    who_dies_first = delta_data.get("who_dies_first", "UNCLEAR")

    def gamma_from_distance(distance: float, energy: float) -> float:
        """Semakin dekat (distance kecil) = Gamma semakin tinggi."""
        if distance <= 0:
            return 10.0
        base_gamma = min(10.0, 1.0 / (distance ** 1.5))
        energy_boost = 1.0 + min(energy, 5.0) * 0.3
        return round(base_gamma * energy_boost, 3)

    if kill_direction == "LONG":
        gamma_active = gamma_from_distance(short_liq, up_energy)
        distance_to_kill = short_liq
        energy_for_kill = up_energy
    elif kill_direction == "SHORT":
        gamma_active = gamma_from_distance(long_liq, down_energy)
        distance_to_kill = long_liq
        energy_for_kill = down_energy
    else:
        gamma_active = 0.0
        distance_to_kill = min(short_liq, long_liq)
        energy_for_kill = max(up_energy, down_energy)

    vol_spike_ratio = latest_volume / volume_ma10 if volume_ma10 > 0 else 1.0
    
    # ===== UPGRADE: MULTI-DIMENSIONAL GAMMA EXECUTION SCORE =====
    gamma_exec_score = 0
    gamma_exec_signals = []
    
    # Signal 1: Volume spike (existing, tapi lebih sensitif)
    if vol_spike_ratio > 2.0:
        gamma_exec_score += 2
        gamma_exec_signals.append(f"vol_spike={vol_spike_ratio:.1f}x")
    elif vol_spike_ratio > 1.3:  # NEW: threshold lebih rendah
        gamma_exec_score += 1
        gamma_exec_signals.append(f"vol_mild_spike={vol_spike_ratio:.1f}x")
    
    # Signal 2: Price momentum vs energy mismatch
    # HFT execute dengan energy kecil tapi price move besar = thin book execution
    if abs(change_5m) > 1.5 and max(up_energy, down_energy) < 0.5:
        gamma_exec_score += 2
        gamma_exec_signals.append(
            f"thin_book_move: change={change_5m:.1f}% energy={max(up_energy, down_energy):.2f}"
        )
    
    # Signal 3: Proximity acceleration — harga mendekati kill zone dengan CEPAT
    # Jika distance_to_kill < 2% DAN abs(change_5m) > 0.8% = akselerasi nyata
    if distance_to_kill < 2.0 and abs(change_5m) > 0.8:
        gamma_exec_score += 1
        gamma_exec_signals.append(
            f"proximity_accel: dist={distance_to_kill:.2f}% change={change_5m:.1f}%"
        )
    
    # Signal 4: Kill direction consistency dengan price direction
    # Jika kill_direction = LONG dan change_5m > 0 (atau sebaliknya) = aligned execution
    price_aligned = (
        (kill_direction == "LONG" and change_5m > 0.5) or
        (kill_direction == "SHORT" and change_5m < -0.5)
    )
    if price_aligned and distance_to_kill < 3.0:
        gamma_exec_score += 1
        gamma_exec_signals.append(
            f"kill_aligned: dir={kill_direction} change={change_5m:.1f}%"
        )
    
    # Signal 5: OBV confirmation — OBV harus searah dengan move
    obv_trend = data.get("obv_trend", "NEUTRAL")
    obv_confirms = (
        (change_5m > 0 and obv_trend in ["POSITIVE", "POSITIVE_EXTREME"]) or
        (change_5m < 0 and obv_trend in ["NEGATIVE", "NEGATIVE_EXTREME"])
    )
    if obv_confirms and abs(change_5m) > 1.0:
        gamma_exec_score += 1
        gamma_exec_signals.append(f"obv_confirm={obv_trend}")
    
    # BAIT PHASE PENALTY: Jika market_phase = BAIT, kurangi score
    # Ini kunci utama untuk fix DUSDT/PLAYUSDT case
    market_phase = data.get("market_phase", "UNKNOWN")
    if market_phase == "BAIT":
        gamma_exec_score = max(0, gamma_exec_score - 2)
        gamma_exec_signals.append("BAIT_PENALTY(-2)")
    
    # Final: butuh score >= 3 untuk dianggap executing
    gamma_executing = gamma_exec_score >= 3

    if gamma_active >= 3.0:
        intensity = "EXTREME"
        description = f"Gamma EXTREME: {distance_to_kill:.2f}% dari kill zone, akselerasi brutal"
    elif gamma_active >= 1.0:
        intensity = "HIGH"
        description = f"Gamma HIGH: {distance_to_kill:.2f}% dari kill zone, akselerasi kuat"
    elif gamma_active >= 0.3:
        intensity = "MODERATE"
        description = f"Gamma MODERATE: {distance_to_kill:.2f}% dari kill zone, mulai akselerasi"
    else:
        intensity = "LOW"
        description = f"Gamma LOW: {distance_to_kill:.2f}% dari kill zone, belum akselerasi"

    kill_speed = min(10.0, gamma_active * 2.0 + (vol_spike_ratio - 1.0) * 0.5)

    return {
        "gamma_value":      gamma_active,
        "gamma_intensity":  intensity,
        "kill_speed":       round(kill_speed, 2),
        "distance_to_kill": distance_to_kill,
        "energy_for_kill":  energy_for_kill,
        "gamma_executing":  gamma_executing,
        "gamma_exec_score": gamma_exec_score,
        "gamma_exec_signals": gamma_exec_signals,
        "vol_spike_ratio":  round(vol_spike_ratio, 2),
        "reason": (
            f"Gamma {intensity}: jarak ke kill zone {distance_to_kill:.2f}%, "
            f"kill speed {kill_speed:.1f}/10, "
            f"exec_score={gamma_exec_score}/7 "
            f"({'SEDANG DIEKSEKUSI HFT' if gamma_executing else 'belum eksekusi'}) "
            f"| signals: {', '.join(gamma_exec_signals) if gamma_exec_signals else 'none'}"
        )
    }


def _greeks_absolute_score(
    theta:  dict,
    vega:   dict,
    delta:  dict,
    gamma:  dict,
    data:   dict
) -> dict:
    """
    Gabungkan semua Greeks menjadi satu scoring.
    
    Rules:
    1. Theta aktif = PREP phase → NO TRADE (confidence tetap ABSOLUTE)
    2. Vega aktif = BAIT phase → fade the move (masuk arah berlawanan)
    3. Delta + Gamma = KILL phase → ikuti arah kill
    
    Priority system:
    - Jika Theta aktif saja → NEUTRAL ABSOLUTE (pasar belum siap)
    - Jika Vega aktif → bias = fade direction (kebalikan bait)
    - Jika Delta menunjuk jelas → bias = kill_direction
    - Jika Gamma EXTREME → lock bias = kill_direction ABSOLUTE

    7% Rule (inti dari semua ini):
    "Siapa yang menyentuh 7% duluan = siapa yang dimakan Binance"
    """
    short_liq    = data.get("short_liq", 99.0)
    long_liq     = data.get("long_liq", 99.0)
    change_5m    = data.get("change_5m", 0.0)
    rsi6         = data.get("rsi6", 50.0)
    volume_ratio = data.get("volume_ratio", 1.0)
    funding_rate = data.get("funding_rate") or 0.0

    KILL_THRESHOLD_PCT = 7.0

    kill_direction  = delta.get("kill_direction", "NEUTRAL")
    who_dies_first  = delta.get("who_dies_first", "UNCLEAR")
    liq_7pct        = delta.get("liq_7pct_touch", "NONE_IN_RANGE")
    delta_exposure  = delta.get("delta_exposure", 0.0)
    gamma_intensity = gamma.get("gamma_intensity", "LOW")
    gamma_executing = gamma.get("gamma_executing", False)
    kill_speed      = gamma.get("kill_speed", 0.0)

    if theta["active"] and not vega["active"] and gamma_intensity == "LOW":
        return {
            "final_bias":   "NEUTRAL",
            "override":     True,
            "confidence":   "ABSOLUTE",
            "priority":     -9990,
            "score_reason": (
                f"GREEKS: Theta aktif ({theta['score']}/6) — PREP phase. "
                f"Funding drain {theta['funding_drain_rate']:.4f}%/8h. "
                f"Pasar sideways menguras trader sebelum move. NO TRADE."
            )
        }

    if vega["active"] and gamma_intensity in ("LOW", "MODERATE"):
        # ===== VEGA-KILL CONFLICT GUARD (LECTURER'S FIX) =====
        # Jika who_dies_first == "BOTH_POSSIBLE" dan gamma belum executing,
        # maka jangan ikuti kill_direction, gunakan first_move dari dual_liq_trap
        who_dies_first_local = delta.get("who_dies_first", "")
        gamma_executing_local = gamma.get("gamma_executing", False)
        kill_speed_local = gamma.get("kill_speed", 0.0)
        
        if VegaKillConflictGuard.should_block_vega_kill_conflict(
            who_dies_first_local, gamma_executing_local, kill_speed_local
        ):
            # Coba gunakan first_move dari dual trap jika tersedia
            dual_trap_data = data.get("dual_liq_trap", {})
            first_move = dual_trap_data.get("first_move_direction", "UNKNOWN")
            if first_move in ("UP", "DOWN"):
                fallback_bias = "LONG" if first_move == "UP" else "SHORT"
                return {
                    "final_bias": fallback_bias,
                    "override": True,
                    "confidence": "ABSOLUTE",
                    "priority": -9991.6,
                    "score_reason": f"VEGA-KILL CONFLICT GUARD: who_dies={who_dies_first_local}, kill_direction={kill_direction} tidak reliable. Mengikuti first_move={first_move} → {fallback_bias}"
                }
        
        fade_direction = "SHORT" if vega["bait_direction"] == "UP" else "LONG"
        
        # VEGA OVERRIDE GUARD (Priority -9991.8)
        # Guard: jika short liq super dekat + buy pressure, override Vega ke LONG
        short_liq_val = data.get("short_liq", 99.0)
        long_liq_val = data.get("long_liq", 99.0)
        agg_val = data.get("agg", 0.0)
        change_5m_val = data.get("change_5m", 0.0)
        
        if short_liq_val < 1.0 and agg_val > 0.7 and change_5m_val > 0:
            return {
                "final_bias": "LONG",
                "override": True,
                "confidence": "ABSOLUTE",
                "priority": -9991.8,
                "score_reason": f"VEGA OVERRIDDEN: short liq {short_liq_val:.2f}% super close, agg={agg_val:.2f} buy dominant → prioritize squeeze over fake move"
            }
        # Guard: jika long liq super dekat + sell pressure, override Vega ke SHORT
        if long_liq_val < 1.0 and agg_val < 0.3 and change_5m_val < 0:
            return {
                "final_bias": "SHORT",
                "override": True,
                "confidence": "ABSOLUTE",
                "priority": -9991.8,
                "score_reason": f"VEGA OVERRIDDEN: long liq {long_liq_val:.2f}% super close, agg={agg_val:.2f} sell dominant → prioritize squeeze over fake move"
            }
        
        # Cek konflik dengan kill direction
        if kill_direction != "NEUTRAL" and fade_direction != kill_direction:
            # Abaikan Vega, ikuti kill direction
            return {
                "final_bias": kill_direction,
                "override": True,
                "confidence": "ABSOLUTE",
                "priority": -9991.5,
                "score_reason": f"VEGA-KILL CONFLICT: Vega fade={fade_direction} tapi kill={kill_direction} → ikuti kill direction (Greeks lebih akurat)"
            }
        return {
            "final_bias":   fade_direction,
            "override":     True,
            "confidence":   "ABSOLUTE",
            "priority":     -9991,
            "score_reason": (
                f"GREEKS: Vega/IV spike aktif ({vega['score']}/6) — BAIT phase. "
                f"Fake {vega['bait_direction']} {abs(change_5m):.1f}% tanpa volume. "
                f"Retail dipancing {vega['bait_direction']}, fade ke {fade_direction}."
            )
        }

    if gamma_intensity == "EXTREME" or (gamma_executing and kill_speed >= 5.0):
        return {
            "final_bias":   kill_direction,
            "override":     True,
            "confidence":   "ABSOLUTE",
            "priority":     -9999,
            "score_reason": (
                f"GREEKS GAMMA EXTREME: kill speed {kill_speed:.1f}/10, "
                f"{who_dies_first} sedang dieksekusi. "
                f"Arah: {kill_direction}. LOCK ABSOLUTE."
            )
        }

    if liq_7pct in ("SHORT_TRADERS_DIE", "LONG_TRADERS_DIE"):
        return {
            "final_bias":   kill_direction,
            "override":     True,
            "confidence":   "ABSOLUTE",
            "priority":     -9995,
            "score_reason": (
                f"GREEKS DELTA: {liq_7pct} — {who_dies_first} menyentuh 7% kill zone. "
                f"Short liq {short_liq:.2f}%, Long liq {long_liq:.2f}%. "
                f"Binance eksekusi {kill_direction}. ABSOLUTE."
            )
        }

    if delta_exposure >= 0.6 and gamma_intensity == "HIGH":
        return {
            "final_bias":   kill_direction,
            "override":     True,
            "confidence":   "ABSOLUTE",
            "priority":     -9993,
            "score_reason": (
                f"GREEKS DELTA+GAMMA: exposure {delta_exposure:.2f}, "
                f"Gamma HIGH, {who_dies_first} dalam danger zone. "
                f"Kill arah: {kill_direction}. ABSOLUTE."
            )
        }

    if theta["active"] and vega["active"]:
        trap_target = vega.get("trap_direction", "NEUTRAL")
        if trap_target != "NEUTRAL":
            return {
                "final_bias":   trap_target,
                "override":     True,
                "confidence":   "ABSOLUTE",
                "priority":     -9992,
                "score_reason": (
                    f"GREEKS THETA+VEGA: sideways (Theta {theta['score']}/6) "
                    f"+ fake move (Vega {vega['score']}/6). "
                    f"Setup klasik: retail sudah ditipu {vega['bait_direction']}, "
                    f"kill akan ke {trap_target}. ABSOLUTE."
                )
            }

    if delta_exposure >= 0.4 and kill_direction != "NEUTRAL":
        return {
            "final_bias":   kill_direction,
            "override":     False,
            "confidence":   "ABSOLUTE",
            "priority":     -9980,
            "score_reason": (
                f"GREEKS DELTA: exposure {delta_exposure:.2f}, "
                f"crowded: {delta.get('who_is_crowded')}. "
                f"Kill arah {kill_direction}. Reinforcing existing bias."
            )
        }

    return {
        "final_bias":   "NEUTRAL",
        "override":     False,
        "confidence":   "ABSOLUTE",
        "priority":     -9970,
        "score_reason": (
            f"GREEKS: Tidak ada sinyal kuat. "
            f"Theta {theta['score']}/6, Vega {vega['score']}/6, "
            f"Delta exposure {delta_exposure:.2f}, Gamma {gamma_intensity}. "
            f"Biarkan sinyal sebelumnya."
        )
    }


def greeks_final_screen(result: dict) -> dict:
    """
    Layer terakhir — dipanggil setelah semua logika selesai.
    
    Parameter
    ---------
    result : dict
        Output dict dari analyzer.analyze() + apply_phase_override().
        Keys yang dipakai:
          change_5m, volume_ratio, funding_rate, rsi6, agg, ofi_bias,
          ofi_strength, short_liq, long_liq, up_energy, down_energy,
          obv_trend, latest_volume, volume_ma10, bias (existing)

    Returns
    -------
    dict
        result yang sudah di-patch dengan Greeks analysis.
        Keys tambahan:
          greeks_bias, greeks_override, greeks_confidence, greeks_priority
          greeks_theta_active, greeks_vega_active, greeks_delta_exposure
          greeks_gamma_intensity, greeks_gamma_executing, greeks_kill_speed
          greeks_who_dies_first, greeks_liq_7pct, greeks_reason

    Contoh integrate:
        result = analyzer.analyze()
        result = apply_phase_override(result, phase_result)
        result = greeks_final_screen(result)  # ← tambahkan ini
        
        # Output formatter lo akan otomatis punya field baru:
        # result["greeks_bias"], result["greeks_who_dies_first"], dll
    """
    output = result.copy()

    # Tambahkan _dual_trap_data untuk digunakan di _greeks_absolute_score
    result["_dual_trap_data"] = result.get("dual_liq_trap", {})

    theta = _theta_check(result)
    vega  = _vega_check(result)
    delta = _delta_calculate(result)
    gamma = _gamma_calculate(result, delta)

    score = _greeks_absolute_score(theta, vega, delta, gamma, result)

    output["greeks_bias"]             = score["final_bias"]
    output["greeks_override"]         = score["override"]
    output["greeks_confidence"]       = score["confidence"]
    output["greeks_priority"]         = score["priority"]
    output["greeks_reason"]           = score["score_reason"]
    output["greeks_theta_active"]     = theta["active"]
    output["greeks_theta_score"]      = theta["score"]
    output["greeks_vega_active"]      = vega["active"]
    output["greeks_vega_score"]       = vega["score"]
    output["greeks_delta_exposure"]   = delta["delta_exposure"]
    output["greeks_delta_crowded"]    = delta["who_is_crowded"]
    output["greeks_gamma_intensity"]  = gamma["gamma_intensity"]
    output["greeks_gamma_executing"]  = gamma["gamma_executing"]
    output["greeks_gamma_exec_score"] = gamma.get("gamma_exec_score", 0)    # NEW
    output["greeks_gamma_exec_signals"] = gamma.get("gamma_exec_signals", []) # NEW
    output["greeks_kill_speed"]       = gamma["kill_speed"]
    output["greeks_who_dies_first"]   = delta["who_dies_first"]
    output["greeks_liq_7pct"]         = delta["liq_7pct_touch"]
    output["greeks_kill_direction"]   = delta["kill_direction"]

    # ===== TAMBAH: PRE-KILL SIGNAL CHECK =====
    # Cek sebelum apply score override
    bias_conflict_data = result.get("bias_kill_conflict", {})
    
    pre_kill = PreKillSignal.detect(
        kill_direction=delta.get("kill_direction", ""),
        short_dist=result.get("short_liq", 99.0),
        long_dist=result.get("long_liq", 99.0),
        delta_crowded=delta.get("who_is_crowded", ""),
        kill_speed=gamma.get("kill_speed", 0),
        gamma_intensity=gamma.get("gamma_intensity", "LOW"),
        bias_kill_conflict=bias_conflict_data.get("has_conflict", False),
        down_energy=result.get("down_energy", 0),
        up_energy=result.get("up_energy", 0),
        agg=result.get("agg", 0.5),
        volume_ratio=result.get("volume_ratio", 1.0)
    )
    
    output["greeks_pre_kill"] = pre_kill  # expose ke output
    
    if pre_kill["override"]:
        output["bias"] = pre_kill["bias"]
        output["confidence"] = "ABSOLUTE"
        output["greeks_override"] = True
        output["greeks_priority"] = pre_kill["priority"]
        output["reason"] = (
            f"[PRE-KILL SIGNAL] {pre_kill['reason']} "
            f"| Original: {result.get('reason', '')}"
        )

    if score["override"]:
        output["bias"]       = score["final_bias"]
        output["confidence"] = "ABSOLUTE"
        output["reason"]     = (
            f"[GREEKS OVERRIDE] {score['score_reason']} "
            f"| Original: {result.get('reason', '')}"
        )
        output["priority_level"] = score["priority"]

    elif score["final_bias"] == result.get("bias") and score["final_bias"] != "NEUTRAL":
        output["reason"] = (
            result.get("reason", "") +
            f" | GREEKS CONFIRM: {score['score_reason']}"
        )
        output["confidence"] = "ABSOLUTE"

    else:
        output["greeks_summary"] = (
            f"Theta {theta['score']}/6 | Vega {vega['score']}/6 | "
            f"Delta {delta['delta_exposure']:.2f} ({delta['who_is_crowded']} crowded) | "
            f"Gamma {gamma['gamma_intensity']} speed {gamma['kill_speed']:.1f}/10 | "
            f"7% kill: {delta['liq_7pct_touch']} → {delta['kill_direction']}"
        )

    return output


# ================= LECTURER'S SARAN LOGIC =================

# Global tracker untuk delayed entry system
_bait_detected_symbols: Dict[str, float] = {}  # {symbol: timestamp_bait_detected}


def _check_kill_confirmation(data: dict) -> dict:
    """
    Konfirmasi bahwa HFT sudah BENAR-BENAR mulai execute.
    Harus semua terpenuhi sebelum entry diizinkan dari BAIT phase.
    """
    gamma_executing = data.get("greeks_gamma_executing", False)
    kill_speed = data.get("greeks_kill_speed", 0)
    volume_ratio = data.get("volume_ratio", 0)
    change_5m = abs(data.get("change_5m", 0))
    gamma_exec_score = data.get("gamma_exec_score", 0)  # NEW
    who_dies = data.get("greeks_who_dies_first", "")

    # 🔥 TAMBAHAN: jika who_dies masih BOTH_POSSIBLE, kill belum real
    if who_dies == "BOTH_POSSIBLE":
        return {
            "kill_confirmed": False,
            "kill_score": 0,
            "reason": "KILL NOT REAL – who_dies still BOTH_POSSIBLE"
        }

    # UPGRADE: score-based confirmation
    kill_score = sum([
        gamma_executing,                    # boolean → 0 atau 1
        abs(kill_speed) >= 3.0,
        volume_ratio >= 0.85,
        change_5m >= 1.5,
        gamma_exec_score >= 3,             # NEW signal
    ])
    
    # Butuh 3 dari 5 (turun dari 4) karena sekarang ada signal baru
    kill_started = kill_score >= 3
    
    return {
        "kill_confirmed": kill_started,
        "kill_score": kill_score,
        "gamma_exec_score": gamma_exec_score,
        "reason": "KILL CONFIRMED" if kill_started else "KILL NOT YET — tunggu execution"
    }


def _should_block_greeks_override(data: dict, phase: str) -> bool:
    """
    Block greeks override jika:
    - Fase masih BAIT, DAN
    - Gamma belum executing, DAN
    - Kill speed masih rendah
    """
    if phase != "BAIT":
        return False
    
    gamma_executing = data.get("greeks_gamma_executing", False)
    kill_speed = abs(data.get("greeks_kill_speed", 0))
    volume_ratio = data.get("volume_ratio", 1.0)
    gamma_exec_score = data.get("gamma_exec_score", 0)  # NEW
    
    # UPGRADE: gunakan exec_score, bukan hanya boolean
    # Block jika score < 3 (tidak cukup bukti execution nyata)
    if not gamma_executing and gamma_exec_score < 3:
        return True
    
    # Block jika speed rendah DAN volume lemah
    if kill_speed < 3.0 and volume_ratio < 0.8:
        return True
    
    return False


def _check_obv_conflict(data: dict, bias: str) -> bool:
    """
    Return True jika OBV bertentangan keras dengan bias.
    Ini tanda bahaya — kemungkinan FAKE signal.
    """
    obv_trend = data.get("obv_trend", "NEUTRAL")
    
    if bias == "LONG" and obv_trend == "NEGATIVE_EXTREME":
        return True  # OBV konfirmasi dump, tapi lo mau LONG → BAHAYA
    if bias == "SHORT" and obv_trend == "POSITIVE_EXTREME":
        return True  # OBV konfirmasi pump, tapi lo mau SHORT → BAHAYA
    
    return False


def _apply_delayed_entry_logic(symbol: str, data: dict, bias: str) -> dict:
    """
    Jika BAIT terdeteksi, catat waktu.
    Entry hanya boleh jika:
    - BAIT sudah terdeteksi sebelumnya (ada di history), DAN
    - Sekarang kill_confirmation = True
    """
    now = time.time()
    phase = data.get("market_phase", "UNKNOWN")
    kill_check = _check_kill_confirmation(data)
    
    if phase == "BAIT":
        _bait_detected_symbols[symbol] = now
        return {
            "entry_allowed": False,
            "entry_reason": "BAIT detected — waiting for kill execution to start"
        }
    
    # Cek apakah sebelumnya ada BAIT
    bait_time = _bait_detected_symbols.get(symbol)
    bait_age_sec = (now - bait_time) if bait_time else 9999
    
    if bait_time and bait_age_sec < 300:  # dalam 5 menit terakhir ada BAIT
        if kill_check["kill_confirmed"]:
            # Bait sudah ada, kill sudah mulai → ENTRY ALLOWED
            return {
                "entry_allowed": True,
                "entry_reason": f"BAIT→KILL confirmed (bait was {bait_age_sec:.0f}s ago)"
            }
        else:
            return {
                "entry_allowed": False,
                "entry_reason": f"BAIT seen {bait_age_sec:.0f}s ago — kill not confirmed yet"
            }
    
    return {"entry_allowed": True, "entry_reason": "No recent BAIT — normal entry"}


def print_greeks_section(result: dict):
    """
    Tambahkan ini ke OutputFormatter.print_signal() lo.
    
    Contoh:
        OutputFormatter.print_signal(result)
        print_greeks_section(result)
    """
    print("\n" + "="*40)
    print("🧬 GREEKS FINAL SCREENER:")
    print(f"   Kill direction : {result.get('greeks_kill_direction', 'N/A')}")
    print(f"   Who dies first : {result.get('greeks_who_dies_first', 'N/A')}")
    print(f"   7% liq touch   : {result.get('greeks_liq_7pct', 'N/A')}")
    print(f"   Delta exposure : {result.get('greeks_delta_exposure', 0):.3f}")
    print(f"   Gamma          : {result.get('greeks_gamma_intensity', 'N/A')} (speed {result.get('greeks_kill_speed', 0):.1f}/10)")
    print(f"   Theta active   : {result.get('greeks_theta_active', False)} ({result.get('greeks_theta_score', 0)}/6)")
    print(f"   Vega active    : {result.get('greeks_vega_active', False)} ({result.get('greeks_vega_score', 0)}/6)")
    print(f"   Greeks bias    : {result.get('greeks_bias', 'N/A')}")
    print(f"   Override       : {result.get('greeks_override', False)}")
    if result.get("greeks_reason"):
        print(f"   Reason         : {result['greeks_reason']}")


# ================= HELPER FUNCTIONS =================

def safe_get(data, key, default=None):
    try:
        if isinstance(data, dict):
            return data.get(key, default)
        return default
    except:
        return default


def safe_float(val, default=0.0):
    try:
        if val is None:
            return default
        return float(val)
    except:
        return default


def safe_div(a, b, default=1.0):
    try:
        if b == 0 or b is None:
            return default
        return a / b
    except:
        return default

# ================= MACD DUEL LOGIC =================

def calculate_macd(close_prices, fast=12, slow=26, signal=9):
    def ema(data, period):
        alpha = 2 / (period + 1)
        ema_arr = [data[0]]
        for price in data[1:]:
            ema_arr.append(alpha * price + (1 - alpha) * ema_arr[-1])
        return np.array(ema_arr)

    close = np.array(close_prices)
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd = fast_ema - slow_ema
    signal_line = ema(macd, signal)
    hist = macd - signal_line
    return macd, signal_line, hist


def scale_macd(hist):
    return (hist * 100000).astype(int)


def macd_duel_logic(hist_scaled):
    """
    Returns a dict:
        action: 'REVERSE', 'FOLLOW', or 'NONE'
        mode: '4vs2' or '2vs4'
        result: a - b
        final: last hist value
        pattern: the 6-element array
    """
    if len(hist_scaled) < 6:
        return {"action": "NONE"}

    last6 = hist_scaled[-6:]
    final = last6[5]

    if final < 0:
        a = last6[3]
        b = last6[1]
        mode = "4vs2"
    else:
        a = last6[1]
        b = last6[3]
        mode = "2vs4"

    duel = a - b
    action = "REVERSE" if duel < 0 else "FOLLOW"

    return {
        "action": action,
        "mode": mode,
        "a": a,
        "b": b,
        "duel": duel,
        "final": final,
        "pattern": last6
    }


# ================= LECTURER'S SARAN LOGIC: MACD DUEL SAFE FILTER =================

def apply_macd_duel_safe(
        macd_decision,
        final_bias,
        algo_type,
        hft_6pct,
        ofi,
        change_5m,
        liq,
        rsi6_5m,
        volume_ratio):
    """
    Filter pembatas agar MACD duel tidak membalik sinyal ketika sinyal asli
    sudah sangat kuat dan konsisten.
    """
    if macd_decision["action"] != "REVERSE":
        return final_bias, macd_decision["action"], "NONE"

    # Filter 1: kekuatan konsensus sinyal asli
    strength = 0
    if algo_type["bias"] == final_bias:
        strength += 2
    if hft_6pct["bias"] == final_bias:
        strength += 2
    if ofi["bias"] == final_bias:
        strength += 1
    if abs(change_5m) > 3:
        strength += 1

    if strength > 3:
        return final_bias, "BLOCKED", f"original_strength={strength}"

    # Filter 2: triple confirmation
    if ofi["bias"] == algo_type["bias"] == hft_6pct["bias"] != "NEUTRAL":
        return final_bias, "BLOCKED", "triple_confirmation"

    # Filter 3: duel terlalu kecil
    if abs(macd_decision.get("duel", 0)) < 5:
        return final_bias, "IGNORED", f"duel_too_small={macd_decision.get('duel', 0)}"

    # Filter 4: momentum besar & likuiditas dekat
    if abs(change_5m) > 2.0 and (liq["short_dist"] < 2.0 or liq["long_dist"] < 2.0):
        if (rsi6_5m >= 75 and final_bias == "LONG") or (rsi6_5m <= 25 and final_bias == "SHORT"):
            pass
        else:
            return final_bias, "BLOCKED", "momentum_and_liq_proximity"

    # Filter 5: Block reverse if it conflicts with strong OFI under low volume
    new_bias = "SHORT" if final_bias == "LONG" else "LONG"
    if new_bias == "LONG" and ofi["bias"] == "SHORT" and ofi["strength"] > 0.7 and volume_ratio < 0.6:
        return final_bias, "BLOCKED", "ofi_short_conflict"
    if new_bias == "SHORT" and ofi["bias"] == "LONG" and ofi["strength"] > 0.7 and volume_ratio < 0.6:
        return final_bias, "BLOCKED", "ofi_long_conflict"

    return new_bias, "REVERSE", "passed_all_filters"

# ================= WEBSOCKET CONNECTOR =================

class BinanceWebSocket:
    """Real-time WebSocket for order book and trades"""

    def __init__(self, symbol: str):
        self.symbol = symbol.lower()
        self.ws_url = (
            f"wss://fstream.binance.com/ws/{self.symbol}@depth20@100ms"
            f"/{self.symbol}@trade"
        )
        self.ws = None
        self.order_book = {}
        self.trades = deque(maxlen=TRADES_MAXLEN)
        self.lock = threading.Lock()
        self.connected = False
        self.last_update = time.time()
        self.thread = None

    def on_message(self, ws, message):
        data = json.loads(message)
        now = time.time()
        self.last_update = now
        with self.lock:
            if 'bids' in data:
                self.order_book = data
            elif 's' in data:
                self.trades.append(data)

    def on_error(self, ws, error):
        print(f"WebSocket error: {error}")
        self.connected = False

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False

    def on_open(self, ws):
        self.connected = True

    def start(self):
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        if self.ws:
            self.ws.close()
        self.connected = False

    def get_latest(self):
        with self.lock:
            return {
                "order_book": self.order_book.copy() if self.order_book else {},
                "trades": list(self.trades),
                "last_update": self.last_update
            }

# ================= NEW DETECTOR MODULES =================

# ========== LIQUIDITY EXTREME OVERRIDE DETECTORS (PRIORITY -2001 to -1998) ==========

class LiquidityExtremeOverride:
    """
    🔥 OVERRIDE BAIT PHASE: Kondisi ekstrem yang memaksa entry meskipun di BAIT
    Priority: -2001 (lebih tinggi dari BAIT HARD BLOCK)
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, funding_rate: float,
               change_5m: float, agg: float, up_energy: float) -> Dict:
        # Short squeeze: short liq super dekat + funding sangat negatif + harga naik
        if (short_liq < 0.8 and 
            funding_rate is not None and 
            funding_rate < -0.003 and 
            change_5m > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"LIQUIDITY EXTREME OVERRIDE: short_liq={short_liq:.2f}% super close, funding={funding_rate:.5f} (crowded short), price up {change_5m:.1f}% → SHORT SQUEEZE, forced LONG even in BAIT",
                "priority": -2001,
                "confidence": "ABSOLUTE"
            }
        
        # Long squeeze: long liq super dekat + funding sangat positif + harga turun
        if (long_liq < 0.8 and 
            funding_rate is not None and 
            funding_rate > 0.003 and 
            change_5m < 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"LIQUIDITY EXTREME OVERRIDE: long_liq={long_liq:.2f}% super close, funding={funding_rate:.5f} (crowded long), price down {change_5m:.1f}% → LONG SQUEEZE, forced SHORT even in BAIT",
                "priority": -2001,
                "confidence": "ABSOLUTE"
            }
        
        return {"override": False}


class FundingExtremeSqueeze:
    """
    🔥 Funding rate ekstrem + liquidity dekat
    Priority: -2000
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, funding_rate: float) -> Dict:
        if funding_rate is None:
            return {"override": False}
        
        # Funding sangat negatif + short liq dekat (<2%) = short squeeze
        if funding_rate < -0.005 and short_liq < 2.0:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"FUNDING EXTREME SQUEEZE: funding={funding_rate:.5f} (crowded short), short_liq={short_liq:.2f}% close → SHORT SQUEEZE",
                "priority": -2000,
                "confidence": "ABSOLUTE"
            }
        
        # Funding sangat positif + long liq dekat = long squeeze
        if funding_rate > 0.005 and long_liq < 2.0:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"FUNDING EXTREME SQUEEZE: funding={funding_rate:.5f} (crowded long), long_liq={long_liq:.2f}% close → LONG SQUEEZE",
                "priority": -2000,
                "confidence": "ABSOLUTE"
            }
        
        return {"override": False}


class Rsi6ExtremeSqueeze:
    """
    🔥 RSI6 > 95 atau < 5 dengan liquidity dekat = squeeze masih jalan
    Priority: -1999
    """
    @staticmethod
    def detect(rsi6: float, short_liq: float, long_liq: float, change_5m: float) -> Dict:
        # RSI sangat overbought (>95) tapi short liq lebih dekat = squeeze lanjut (LONG)
        if rsi6 > 95 and short_liq < long_liq and short_liq < 3.0:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"RSI EXTREME SQUEEZE: RSI6={rsi6:.1f} max overbought, short_liq={short_liq:.2f}% closer → squeeze continuation LONG",
                "priority": -1999,
                "confidence": "ABSOLUTE"
            }
        
        # RSI sangat oversold (<5) tapi long liq lebih dekat = dump lanjut (SHORT)
        if rsi6 < 5 and long_liq < short_liq and long_liq < 3.0:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"RSI EXTREME SQUEEZE: RSI6={rsi6:.1f} min oversold, long_liq={long_liq:.2f}% closer → dump continuation SHORT",
                "priority": -1999,
                "confidence": "ABSOLUTE"
            }
        
        return {"override": False}


class LiquidityProximityAbsolute:
    """
    🔥 Jika salah satu liquidity < 0.5%, paksa arah tersebut tanpa peduli apapun
    Priority: -1998
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, change_5m: float) -> Dict:
        if short_liq < 0.5 and short_liq < long_liq:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"LIQUIDITY ABSOLUTE: short_liq={short_liq:.2f}% < 0.5% → forced LONG regardless of phase",
                "priority": -1998,
                "confidence": "ABSOLUTE"
            }
        
        if long_liq < 0.5 and long_liq < short_liq:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"LIQUIDITY ABSOLUTE: long_liq={long_liq:.2f}% < 0.5% → forced SHORT regardless of phase",
                "priority": -1998,
                "confidence": "ABSOLUTE"
            }
        
        return {"override": False}


# ========== END LIQUIDITY EXTREME OVERRIDE DETECTORS ==========

class PostSqueezeReversal:
    """
    🔥 MENDETEKSI AKHIR SQUEEZE: Harga sudah melewati target likuiditas
    
    Kondisi SHORT (setelah pump):
    - Harga naik (change_5m > 0)
    - Short liq dekat (< 2%)
    - RSI overbought (> 70)
    - Volume rendah (< 0.6x)
    - Up energy habis (< 0.5)
    - **Harga sudah naik melebihi short_liq** (change_5m > short_liq)
    
    Priority: -1107 (sangat tinggi)
    """
    @staticmethod
    def detect(change_5m: float, short_liq: float, rsi6: float,
               volume_ratio: float, up_energy: float,
               long_liq: float = 99.0, down_energy: float = 0.0,
               obv_trend: str = "NEUTRAL") -> Dict:
        
        # Kasus SHORT: pump selesai, target short liq sudah tersapu
        if (change_5m > 0 and
            short_liq < 2.0 and
            rsi6 > 70 and
            volume_ratio < 0.6 and
            up_energy < 0.5 and
            change_5m > short_liq):   # 🔥 kunci: harga sudah lewati target
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"POST-SQUEEZE REVERSAL: price pumped {change_5m:.1f}% > short liq {short_liq:.2f}%, "
                    f"RSI {rsi6:.1f} overbought, volume {volume_ratio:.2f}x rendah, up_energy={up_energy:.2f} habis → "
                    f"short stop sudah tersapu, HFT akan dump"
                ),
                "priority": -1107
            }
        
        # Kasus LONG: dump selesai, target long liq sudah tersapu
        if (change_5m < 0 and
            long_liq < 2.0 and
            rsi6 < 30 and
            volume_ratio < 0.6 and
            down_energy < 0.5 and
            abs(change_5m) > long_liq):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"POST-DUMP REVERSAL: price dumped {abs(change_5m):.1f}% > long liq {long_liq:.2f}%, "
                    f"RSI {rsi6:.1f} oversold, volume rendah, down_energy habis → long stop tersapu, HFT akan pump"
                ),
                "priority": -1107
            }
        
        return {"override": False}


class OversoldDistributionContinuation:
    """
    🔥 FALLING KNIFE DETECTOR: Oversold + OBV negative extreme + long liq close = masih akan dump
    
    PLAYUSDT CASE: harga turun -3.39%, RSI 11, long liq 0.3%, tapi OBV NEGATIVE_EXTREME
    → smart money masih distribusi, harga akan terus turun untuk sapu long stop.
    
    Priority: -1104 (di atas GlobalPositionImbalance -1104? sejajar, tapi kita beri nilai -1104.5 agar lebih tinggi)
    Karena kondisi ini sangat berbahaya – jebakan bounce.
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float, long_liq: float, short_liq: float = 99.0,
               obv_trend: str = "NEUTRAL", obv_value: float = 0.0, volume_ratio: float = 1.0,
               agg: float = 0.5, ofi_bias: str = "NEUTRAL") -> Dict:
        
        # Kasus SHORT: oversold tapi OBV negatif ekstrem → masih dump
        if (change_5m < -2.0 and
            rsi6 < 30 and
            long_liq < 2.0 and
            obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"] and
            volume_ratio >= 0.7 and
            agg < 0.8):   # tidak terlalu buy dominant (bisa jadi retail beli di bottom)
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OVERSOLD DISTRIBUTION CONTINUATION: price down {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f} oversold, long liq {long_liq:.2f}% dekat, "
                    f"tapi OBV {obv_trend} (value={obv_value:,.0f}) menunjukkan distribusi aktif, "
                    f"volume {volume_ratio:.2f}x → smart money masih jual, dump lanjut untuk sweep long stop"
                ),
                "priority": -1104
            }
        
        # Mirror: overbought tapi OBV positif ekstrem → masih pump
        if (change_5m > 2.0 and
            rsi6 > 70 and
            short_liq < 2.0 and
            obv_trend in ["POSITIVE_EXTREME", "POSITIVE"] and
            volume_ratio >= 0.7 and
            agg > 0.2):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OVERBOUGHT ACCUMULATION CONTINUATION: price up {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f} overbought, short liq {short_liq:.2f}% dekat, "
                    f"OBV {obv_trend} menunjukkan akumulasi aktif → pump lanjut"
                ),
                "priority": -1104
            }
        
        return {"override": False}


class LowVolumeDistributionContinuation:
    """
    🔥 STOUSDT FIX: Oversold + OBV negative extreme + volume sangat rendah + long liq dekat = masih dump
    
    Volume rendah menunjukkan tidak ada buyer yang masuk. OBV negatif ekstrem menunjukkan distribusi aktif.
    Ini bukan fake move, tapi kelanjutan dump untuk sapu long stop.
    
    Kondisi SHORT:
    - change_5m < -2.0   (sudah turun)
    - rsi6 < 30          (oversold)
    - long_liq < 2.0     (target dekat)
    - obv_trend = NEGATIVE_EXTREME/NEGATIVE (distribusi aktif)
    - volume_ratio < 0.7 (tidak ada volume beli)
    - down_energy < 0.1  (tidak ada tekanan beli)
    
    Kondisi LONG (mirror):
    - change_5m > 2.0    (sudah naik)
    - rsi6 > 70          (overbought)
    - short_liq < 2.0    (target dekat)
    - obv_trend = POSITIVE_EXTREME/POSITIVE (akumulasi aktif)
    - volume_ratio < 0.7 (tidak ada volume jual)
    - up_energy < 0.1    (tidak ada tekanan jual)
    
    Priority: -1106 (di atas ProfitImbalanceReversal -1105)
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float,
               long_liq: float = 99.0, short_liq: float = 99.0,
               obv_trend: str = "NEUTRAL", volume_ratio: float = 1.0,
               down_energy: float = 0.0, up_energy: float = 0.0) -> Dict:
        
        # SHORT: dump lanjut
        if (change_5m < -2.0 and
            rsi6 < 30 and
            long_liq < 2.0 and
            obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"] and
            volume_ratio < 0.7 and
            down_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"LOW VOLUME DISTRIBUTION CONTINUATION: price down {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f} oversold, long liq {long_liq:.2f}% dekat, "
                    f"OBV {obv_trend} (distribusi), volume {volume_ratio:.2f}x rendah → dump lanjut"
                ),
                "priority": -1106
            }
        
        # LONG: pump lanjut
        if (change_5m > 2.0 and
            rsi6 > 70 and
            short_liq < 2.0 and
            obv_trend in ["POSITIVE_EXTREME", "POSITIVE"] and
            volume_ratio < 0.7 and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"LOW VOLUME ACCUMULATION CONTINUATION: price up {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f} overbought, short liq {short_liq:.2f}% dekat, "
                    f"OBV {obv_trend} (akumulasi), volume {volume_ratio:.2f}x rendah → pump lanjut"
                ),
                "priority": -1106
            }
        
        return {"override": False}


class OverboughtLowVolumeReversal:
    """
    🔥 OVERBOUGHT LOW VOLUME REVERSAL (OLVR) – Priority -1106
    
    Deteksi pump palsu di area overbought dengan volume kering.
    
    Kondisi:
    - change_5m > 1.5          (harga naik signifikan)
    - rsi6 > 70                (overbought)
    - rsi6_5m > 65             (overbought di TF 5m)
    - volume_ratio < 0.7       (volume rendah/kering)
    - short_liq < 5.0          (short liquidation dekat)
    
    Priority: -1106
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float, rsi6_5m: float,
               volume_ratio: float, short_liq: float) -> Dict:
        # Harga naik signifikan, overbought, volume rendah, short liq dekat
        if (change_5m > 1.5 and
            rsi6 > 70 and
            rsi6_5m > 65 and
            volume_ratio < 0.7 and
            short_liq < 5.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OVERBOUGHT LOW VOLUME REVERSAL: price up {change_5m:.1f}% with volume {volume_ratio:.2f}x, RSI {rsi6:.1f}/{rsi6_5m:.1f} overbought, short liq {short_liq:.2f}% close → fake pump, dump incoming",
                "priority": -1106
            }
        return {"override": False}


# ================= NEW DETECTOR MODULES (LECTURER'S ADDITIONS) =================

class FakeKillDirection:
    """
    🔥 FAKE KILL DIRECTION DETECTOR – Priority -1104
    
    Jangan ikuti kill_direction jika tidak ada momentum.
    
    Kondisi:
    - kill_direction ada (LONG/SHORT)
    - kill_speed < 1.0 (lambat)
    - volume_ratio < 0.6 (volume rendah)
    - gamma_executing = False (gamma belum jalan)
    
    Priority: -1104
    """
    @staticmethod
    def detect(kill_direction: str, kill_speed: float, volume_ratio: float, 
               gamma_executing: bool, who_dies_first: str) -> Dict:
        if (kill_direction in ("LONG", "SHORT") and 
            kill_speed < 1.0 and 
            volume_ratio < 0.6 and 
            not gamma_executing):
            return {
                "override": True,
                "bias": "NEUTRAL",
                "reason": f"FAKE KILL DIRECTION: kill={kill_direction} speed={kill_speed:.2f}<1.0, vol={volume_ratio:.2f}x, gamma not executing → ignore",
                "priority": -1104
            }
        return {"override": False}


class RsiDivergenceReversal:
    """
    🔥 RSI DIVERGENCE MULTI-TF REVERSAL – Priority -1105
    
    RSI 1m vs 5m divergensi ekstrem → ikuti RSI 1m (lebih cepat bereaksi).
    
    Kondisi LONG:
    - rsi6_1m < 40 (oversold di 1m)
    - rsi6_5m > 70 (overbought di 5m)
    - volume_ratio < 0.7 (volume rendah)
    
    Kondisi SHORT:
    - rsi6_1m > 60 (overbought di 1m)
    - rsi6_5m < 30 (oversold di 5m)
    - volume_ratio < 0.7 (volume rendah)
    
    Priority: -1105
    """
    @staticmethod
    def detect(rsi6_1m: float, rsi6_5m: float, volume_ratio: float) -> Dict:
        # 1m oversold, 5m overbought → bounce LONG
        if rsi6_1m < 40 and rsi6_5m > 70 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"RSI DIVERGENCE: 1m={rsi6_1m:.1f} oversold, 5m={rsi6_5m:.1f} overbought → bounce up",
                "priority": -1105
            }
        # 1m overbought, 5m oversold → dump SHORT
        if rsi6_1m > 60 and rsi6_5m < 30 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"RSI DIVERGENCE: 1m={rsi6_1m:.1f} overbought, 5m={rsi6_5m:.1f} oversold → dump down",
                "priority": -1105
            }
        return {"override": False}


class ObvPriceDivergence:
    """
    🔥 OBV-PRICE DIVERGENCE DETECTOR – Priority -1103
    
    OBV POSITIVE_EXTREME tapi harga turun atau flat → smart money distribusi.
    OBV NEGATIVE_EXTREME tapi harga naik → smart money akumulasi.
    
    Kondisi SHORT:
    - obv_trend = "POSITIVE_EXTREME"
    - change_5m < 0 (harga turun)
    - volume_ratio < 0.6 (volume rendah)
    
    Kondisi LONG:
    - obv_trend = "NEGATIVE_EXTREME"
    - change_5m > 0 (harga naik)
    - volume_ratio < 0.6 (volume rendah)
    
    Priority: -1103
    """
    @staticmethod
    def detect(obv_trend: str, change_5m: float, volume_ratio: float) -> Dict:
        if obv_trend == "POSITIVE_EXTREME" and change_5m < 0 and volume_ratio < 0.6:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OBV-PRICE DIVERGENCE: OBV positive extreme but price down {change_5m:.1f}%, volume low → distribution, dump",
                "priority": -1103
            }
        if obv_trend == "NEGATIVE_EXTREME" and change_5m > 0 and volume_ratio < 0.6:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OBV-PRICE DIVERGENCE: OBV negative extreme but price up → accumulation, pump",
                "priority": -1103
            }
        return {"override": False}


class ExtremeRsiShortSqueeze:
    """
    🔥 EXTREME RSI SHORT SQUEEZE DETECTOR – Priority -1102
    
    RSI6=100 + short_liq lebih dekat + volume rendah → squeeze lanjut (LONG), bukan blow-off.
    
    Kondisi LONG:
    - rsi6 > 98 (RSI maksimal)
    - short_liq < long_liq (short liquidation lebih dekat)
    - volume_ratio < 0.7 (volume rendah)
    
    Kondisi SHORT:
    - rsi6 < 2 (RSI minimal)
    - long_liq < short_liq (long liquidation lebih dekat)
    - volume_ratio < 0.7 (volume rendah)
    
    Priority: -1102
    """
    @staticmethod
    def detect(rsi6: float, short_liq: float, long_liq: float, volume_ratio: float) -> Dict:
        if rsi6 > 98 and short_liq < long_liq and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"EXTREME RSI SHORT SQUEEZE: RSI6={rsi6:.1f} max, short liq={short_liq:.2f}% closer than long liq, volume low → squeeze continues",
                "priority": -1102
            }
        if rsi6 < 2 and long_liq < short_liq and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"EXTREME RSI LONG SQUEEZE: RSI6={rsi6:.1f} min, long liq closer → dump continues",
                "priority": -1102
            }
        return {"override": False}


class OFIAggSpoofingDetector:
    """
    🔥 OFI-AGG SPOOFING DETECTOR – Priority -1100
    
    Jika OFI sangat bullish tapi agg sangat bearish (atau sebaliknya), percaya agg.
    Ini adalah spoofing oleh HFT untuk memancing trader retail.
    
    Kondisi SHORT (OFI LONG spoofing):
    - ofi_bias == "LONG" dan ofi_strength > 0.8
    - agg < 0.4                 (mayoritas SELL)
    - volume_ratio < 0.7        (volume rendah)
    - change_5m > 0             (harga naik sedikit untuk memancing)
    
    Kondisi LONG (OFI SHORT spoofing):
    - ofi_bias == "SHORT" dan ofi_strength > 0.8
    - agg > 0.6                 (mayoritas BUY)
    - volume_ratio < 0.7        (volume rendah)
    - change_5m < 0             (harga turun sedikit untuk memancing)
    
    Priority: -1100
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, agg: float,
               volume_ratio: float, change_5m: float) -> Dict:
        # OFI LONG kuat tapi agg rendah (<0.4) = spoofing
        if (ofi_bias == "LONG" and ofi_strength > 0.8 and
            agg < 0.4 and volume_ratio < 0.7 and change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OFI-AGG SPOOFING: OFI LONG {ofi_strength:.2f} tapi agg={agg:.2f} (mayoritas SELL), volume rendah → HFT memancing LONG, akan dump",
                "priority": -1100
            }
        # OFI SHORT kuat tapi agg tinggi (>0.6) = spoofing
        if (ofi_bias == "SHORT" and ofi_strength > 0.8 and
            agg > 0.6 and volume_ratio < 0.7 and change_5m < 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OFI-AGG SPOOFING: OFI SHORT {ofi_strength:.2f} tapi agg={agg:.2f} (mayoritas BUY), volume rendah → HFT memancing SHORT, akan pump",
                "priority": -1100
            }
        return {"override": False}


class KillDirectionWithoutMomentum:
    """
    🔥 KILL DIRECTION WITHOUT MOMENTUM – Priority -1104
    
    Jangan ikuti kill_direction jika gamma tidak executing dan kill speed terlalu rendah.
    Ini adalah fake signal dari HFT untuk menjebak trader.
    
    Kondisi:
    - kill_direction in ("LONG", "SHORT")  (ada arah kill)
    - gamma_executing == False             (gamma belum executing)
    - kill_speed < 1.0                     (kill speed sangat rendah)
    - volume_ratio < 0.8                   (volume rendah)
    
    Priority: -1104
    """
    @staticmethod
    def detect(kill_direction: str, gamma_executing: bool,
               kill_speed: float, volume_ratio: float) -> Dict:
        if (kill_direction in ("LONG", "SHORT") and
            not gamma_executing and
            kill_speed < 1.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "NEUTRAL",
                "reason": f"KILL DIRECTION WITHOUT MOMENTUM: kill={kill_direction} but gamma_executing=False, kill_speed={kill_speed:.2f}<1.0, volume low → fake signal, WAIT",
                "priority": -1104
            }
        return {"override": False}


class VolumeDryUpOverboughtTrap:
    """
    🔥 VOLUME DRY-UP OVERBOUGHT TRAP – Priority -1105
    
    Kombinasi volume sangat rendah (<0.5x) + overbought + harga naik.
    Ini adalah tanda exhaustion - harga akan reversal turun.
    
    Kondisi:
    - volume_ratio < 0.5         (volume sangat rendah/kering)
    - rsi6 > 70                  (overbought)
    - change_5m > 1.0            (harga naik)
    
    Priority: -1105
    """
    @staticmethod
    def detect(volume_ratio: float, rsi6: float, change_5m: float) -> Dict:
        if (volume_ratio < 0.5 and
            rsi6 > 70 and
            change_5m > 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"VOLUME DRY-UP OVERBOUGHT TRAP: volume {volume_ratio:.2f}x sangat rendah, RSI {rsi6:.1f} overbought, price up {change_5m:.1f}% → exhaustion, reversal down",
                "priority": -1105
            }
        return {"override": False}


class DoubleKillSequenceDetector:
    """
    🔥 SYSUSDT PATTERN: Double Kill Setup
    
    HFT mau bunuh DUA sisi, tapi ada urutan:
    1. Siapa yang lebih dekat = yang mati DULU
    2. Setelah yang dekat habis → baru giliran yang jauh
    
    Rule:
    - who_dies_first == "BOTH_POSSIBLE" (belum ada korban dipilih)
    - dual_liq_trap == True (arena pembantaian aktif)
    - short_liq < long_liq → pump DULU untuk kill SHORT, baru dump
    - long_liq < short_liq → dump DULU untuk kill LONG, baru pump
    
    Output: EXPECTED_FIRST_MOVE = arah gerak PERTAMA (bukan akhir)
    
    Priority: -1106 (di atas ProfitImbalanceReversal -1105)
    Karena ini override semua sinyal tentang arah akhir.
    """
    @staticmethod
    def detect(who_dies_first: str, dual_liq_trap: bool, trap_score: int,
               short_liq: float, long_liq: float,
               kill_confirmed: bool, ofi_bias: str, ofi_strength: float,
               agg: float, volume_ratio: float,
               kill_direction: str) -> Dict:
        
        # Hanya aktif jika belum ada korban dipilih
        if who_dies_first != "BOTH_POSSIBLE":
            return {"override": False}
        
        # Hanya aktif jika dual liq trap aktif
        if not dual_liq_trap or trap_score < 3:
            return {"override": False}
        
        # Hanya aktif jika kill belum dimulai
        if kill_confirmed:
            return {"override": False}
        
        # ============================================================
        # KAIDAH UTAMA: siapa yang lebih dekat = mati DULU
        # Setelah yang dekat mati, baru arah akhir (kill_direction) jalan
        # ============================================================
        
        liq_diff = abs(short_liq - long_liq)
        
        # Short liq lebih dekat → pump dulu (kill SHORT) → baru dump (kill LONG)
        if short_liq < long_liq and short_liq < 3.0:
            
            # Konfirmasi OFI palsu: ofi_strength tinggi + volume rendah = umpan
            ofi_is_fake = ofi_strength > 0.8 and volume_ratio < 0.8
            
            # Konfirmasi agg palsu: 100% buy tapi volume rendah
            agg_is_fake = agg > 0.85 and volume_ratio < 0.7
            
            # Jika ada umpan aktif = HFT sedang narik SHORT masuk
            has_bait = ofi_is_fake or agg_is_fake
            
            return {
                "override": True,
                "bias": "LONG",  # gerak PERTAMA = pump
                "expected_first_move": "UP",
                "expected_second_move": "DOWN",
                "reason": (
                    f"DOUBLE KILL SEQUENCE: who_dies=BOTH_POSSIBLE, "
                    f"short_liq={short_liq:.2f}% DEKAT vs long_liq={long_liq:.2f}% JAUH → "
                    f"HFT pump DULU untuk sweep short stop, "
                    f"BARU dump untuk kill long | "
                    f"bait_active={has_bait} (ofi={ofi_strength:.2f}, agg={agg:.2f})"
                ),
                "priority": -1106,
                "kill_sequence": "SHORT_FIRST_THEN_LONG"
            }
        
        # Long liq lebih dekat → dump dulu (kill LONG) → baru pump (kill SHORT)
        if long_liq < short_liq and long_liq < 3.0:
            
            ofi_is_fake = ofi_strength > 0.8 and volume_ratio < 0.8
            agg_is_fake = agg < 0.15 and volume_ratio < 0.7
            has_bait = ofi_is_fake or agg_is_fake
            
            return {
                "override": True,
                "bias": "SHORT",  # gerak PERTAMA = dump
                "expected_first_move": "DOWN",
                "expected_second_move": "UP",
                "reason": (
                    f"DOUBLE KILL SEQUENCE: who_dies=BOTH_POSSIBLE, "
                    f"long_liq={long_liq:.2f}% DEKAT vs short_liq={short_liq:.2f}% JAUH → "
                    f"HFT dump DULU untuk sweep long stop, "
                    f"BARU pump untuk kill short | "
                    f"bait_active={has_bait}"
                ),
                "priority": -1106,
                "kill_sequence": "LONG_FIRST_THEN_SHORT"
            }
        
        return {"override": False}


class OFIBaitValidator:
    """
    🔥 SYSUSDT: OFI 1.00 + agg 1.00 + volume rendah = UMPAN HFT
    
    Ketika OFI sangat tinggi (>0.9) tapi volume rendah:
    - Ini bukan akumulasi nyata
    - Ini adalah spoofing untuk menarik posisi ke arah tertentu
    - Setelah cukup posisi terisi, HFT akan balik arah
    
    Bedakan dengan OFI nyata:
    - OFI nyata: strength tinggi + volume tinggi (>1.0x)
    - OFI bait:  strength tinggi + volume RENDAH (<0.8x)
    
    Jika OFI terdeteksi sebagai bait:
    → Percaya LIQUIDITY PROXIMITY bukan OFI
    → Output arah sesuai liq terdekat
    
    Priority: -1099 (di atas OFISpoofingDetector yang ada)
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float,
               agg: float, volume_ratio: float,
               short_liq: float, long_liq: float,
               who_dies_first: str,
               dual_liq_trap: bool) -> Dict:
        
        # OFI bait: strength sangat tinggi tapi volume rendah
        ofi_bait = (
            ofi_strength > 0.85 and
            volume_ratio < 0.7 and
            agg > 0.75  # trades juga spoofed
        )
        
        if not ofi_bait:
            return {"override": False}
        
        # Jika who_dies = BOTH_POSSIBLE + dual trap = setup double kill
        # OFI sedang menarik korban ke arah yang AKAN DISAPU DULU
        if who_dies_first == "BOTH_POSSIBLE" and dual_liq_trap:
            # OFI LONG tapi short_liq dekat = HFT mau sweep short dulu
            if ofi_bias == "LONG" and short_liq < long_liq and short_liq < 3.0:
                return {
                    "override": True,
                    "bias": "LONG",  # ikuti OFI bait = ikuti first move
                    "ofi_is_bait": True,
                    "reason": (
                        f"OFI BAIT VALIDATOR: OFI {ofi_bias} ({ofi_strength:.2f}) "
                        f"dengan volume {volume_ratio:.2f}x = UMPAN. "
                        f"HFT menarik SHORT masuk untuk di-sweep dulu. "
                        f"short_liq {short_liq:.2f}% dekat → first move = UP"
                    ),
                    "priority": -1099
                }
            
            # OFI SHORT tapi long_liq dekat = HFT mau sweep long dulu
            if ofi_bias == "SHORT" and long_liq < short_liq and long_liq < 3.0:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "ofi_is_bait": True,
                    "reason": (
                        f"OFI BAIT VALIDATOR: OFI {ofi_bias} ({ofi_strength:.2f}) "
                        f"dengan volume {volume_ratio:.2f}x = UMPAN. "
                        f"HFT menarik LONG masuk untuk di-sweep dulu. "
                        f"long_liq {long_liq:.2f}% dekat → first move = DOWN"
                    ),
                    "priority": -1099
                }
        
        return {"override": False}


class GammaLiquidityAlignment:
    """
    🔥 GAMMA SPOOFING DETECTOR: Gamma EXTREME tapi liquidity proximity bertentangan
    
    TRIAUSDT CASE:
    - Gamma EXTREME kill_direction = SHORT
    - Tapi long_liq = 0.36% (sangat dekat) vs short_liq = 3.62% (jauh)
    - agg = 0.9 (90% buy), ofi = LONG
    - Gamma executing = false → ini sinyal palsu
    
    Rule:
    Jika Gamma EXTREME dan kill_direction bertentangan dengan liquidity proximity
    (misal: kill SHORT tapi long_liq lebih dekat), maka override ke arah liquidity.
    
    Priority: -10000 (di atas Gamma EXTREME -9999)
    
    🔥 KAIDAH BARU: VALIDASI KONSISTENSI SEBELUM SPOOFING CLAIM
    Spoofing = kill_direction BERLAWANAN dengan liquidity proximity
    DAN gamma belum executing DAN volume rendah
    
    BUKAN spoofing jika:
    - kill_direction SEARAH dengan liquidity proximity
    - Contoh: kill=LONG + short_liq lebih dekat = KONSISTEN
      (short yang akan mati karena dekat target)
    """
    @staticmethod
    def detect(gamma_intensity: str, kill_direction: str,
               short_dist: float, long_dist: float,
               gamma_executing: bool, volume_ratio: float,
               market_phase: str = "UNKNOWN",
               kill_speed: float = 0.0) -> Dict:
        
        if gamma_intensity != "EXTREME":
            return {"override": False}
        
        # ============================================================
        # 🔥 KAIDAH BARU: VALIDASI KONSISTENSI SEBELUM SPOOFING CLAIM
        # 
        # Spoofing = kill_direction BERLAWANAN dengan liquidity proximity
        # DAN gamma belum executing DAN volume rendah
        #
        # BUKAN spoofing jika:
        # - kill_direction SEARAH dengan liquidity proximity
        # - Contoh: kill=LONG + short_liq lebih dekat = KONSISTEN
        #   (short yang akan mati karena dekat target)
        # ============================================================
        
        # Cek konsistensi kill_direction vs liquidity proximity
        liq_says_long = short_dist < long_dist   # short lebih dekat = LONG target
        liq_says_short = long_dist < short_dist  # long lebih dekat = SHORT target
        
        kill_says_long = kill_direction == "LONG"
        kill_says_short = kill_direction == "SHORT"
        
        # Konsisten = kill_direction SEARAH dengan liquidity proximity
        is_consistent = (
            (kill_says_long and liq_says_long) or   # STOUSDT case ini
            (kill_says_short and liq_says_short)
        )
        
        # Inkonsisten = kill_direction BERLAWANAN dengan liquidity proximity
        is_inconsistent = (
            (kill_says_long and liq_says_short) or
            (kill_says_short and liq_says_long)
        )
        
        # ============================================================
        # JIKA KONSISTEN = INI PRE-KILL SIGNAL, BUKAN SPOOFING
        # Jangan override, biarkan Greeks yang handle
        # ============================================================
        if is_consistent:
            return {"override": False}  # tidak ada spoofing
        
        # ============================================================
        # JIKA INKONSISTEN = BARU cek apakah ini spoofing
        # Syarat spoofing: inkonsisten + belum executing + volume rendah
        # 🔥 SARAN DOSEN: Tambahkan kondisi kill_speed < 2.0 untuk override tanpa peduli gamma_executing
        # ============================================================
        if is_inconsistent:
            
            # Kasus A: Gamma bilang SHORT tapi short_liq lebih dekat
            # (harusnya LONG karena short yang akan mati)
            if (kill_direction == "SHORT" and
                short_dist < long_dist and
                short_dist < 1.5):
                
                # 🔥 TAMBAHAN: Jika kill_speed < 2.0 dan volume_ratio < 0.8, override tanpa peduli gamma_executing
                if (not gamma_executing and volume_ratio < 0.8) or (kill_speed < 2.0 and volume_ratio < 0.8):
                    return {
                        "override": True,
                        "bias": "LONG",
                        "reason": (
                            f"GAMMA SPOOFING: kill=SHORT tapi short_liq={short_dist:.2f}% "
                            f"lebih dekat → inkonsisten, HFT spoof SHORT, "
                            f"sebenarnya LONG squeeze (kill_speed={kill_speed:.1f})"
                        ),
                        "priority": -10000
                    }
            
            # Kasus B: Gamma bilang LONG tapi long_liq lebih dekat
            # (harusnya SHORT karena long yang akan mati)
            if (kill_direction == "LONG" and
                long_dist < short_dist and
                long_dist < 1.5):
                
                # 🔥 TAMBAHAN: Jika kill_speed < 2.0 dan volume_ratio < 0.8, override tanpa peduli gamma_executing
                if (not gamma_executing and volume_ratio < 0.8) or (kill_speed < 2.0 and volume_ratio < 0.8):
                    return {
                        "override": True,
                        "bias": "SHORT",
                        "reason": (
                            f"GAMMA SPOOFING: kill=LONG tapi long_liq={long_dist:.2f}% "
                            f"lebih dekat → inkonsisten, HFT spoof LONG, "
                            f"sebenarnya SHORT dump (kill_speed={kill_speed:.1f})"
                        ),
                        "priority": -10000
                    }
        
        return {"override": False}


class PreKillSignal:
    """
    🔥 KAIDAH BARU: PRE-KILL = arah sudah fix sebelum gamma execute
    
    Dosen: "gamma tidak perlu executing dulu untuk menentukan arah"
    
    Pre-Kill terkonfirmasi jika:
    1. kill_direction jelas (bukan NEUTRAL)
    2. Liquidity proximity KONSISTEN dengan kill_direction
    3. delta_crowded KONSISTEN (crowded = yang akan mati)
    4. kill_speed > 3.0 (momentum sudah tinggi)
    5. bias_kill_conflict = True (sistem sendiri tahu ada konflik)
    
    Jika semua terpenuhi → langsung ikuti kill_direction
    TANPA nunggu gamma_executing = True
    
    Priority: -9998 (tepat di bawah Gamma EXTREME -9999)
    """
    @staticmethod
    def detect(kill_direction: str,
               short_dist: float, long_dist: float,
               delta_crowded: str,
               kill_speed: float,
               gamma_intensity: str,
               bias_kill_conflict: bool,
               down_energy: float, up_energy: float,
               agg: float, volume_ratio: float) -> Dict:
        
        if kill_direction == "NEUTRAL" or kill_direction == "":
            return {"override": False}
        
        # Cek konsistensi kill_direction vs liquidity
        liq_consistent = (
            (kill_direction == "LONG" and short_dist < long_dist) or
            (kill_direction == "SHORT" and long_dist < short_dist)
        )
        
        # Cek konsistensi delta_crowded
        # crowded SHORT + kill LONG = shorts yang mati = konsisten
        # crowded LONG + kill SHORT = longs yang mati = konsisten
        delta_consistent = (
            (kill_direction == "LONG" and delta_crowded == "SHORT") or
            (kill_direction == "SHORT" and delta_crowded == "LONG")
        )
        
        # Cek energy: tidak ada perlawanan
        no_resistance = (
            (kill_direction == "LONG" and down_energy < 0.1) or
            (kill_direction == "SHORT" and up_energy < 0.1)
        )
        
        # Hitung skor konfirmasi Pre-Kill
        pre_kill_score = sum([
            liq_consistent,                    # +1
            delta_consistent,                  # +1
            kill_speed > 3.0,                  # +1
            bias_kill_conflict,                # +1 (sistem sendiri tahu)
            no_resistance,                     # +1
            gamma_intensity in ("HIGH",
                                "EXTREME"),    # +1
        ])
        
        # Butuh minimal 4 dari 6 konfirmasi
        if pre_kill_score >= 4:
            
            # Tentukan target liquidity distance
            target_liq = short_dist if kill_direction == "LONG" else long_dist
            
            return {
                "override": True,
                "bias": kill_direction,
                "reason": (
                    f"PRE-KILL SIGNAL ({pre_kill_score}/6): "
                    f"kill={kill_direction}, "
                    f"liq_consistent={liq_consistent}, "
                    f"delta_crowded={delta_crowded} akan mati, "
                    f"kill_speed={kill_speed:.1f}/10, "
                    f"target_liq={target_liq:.2f}% "
                    f"→ arah sudah fix, tidak perlu tunggu gamma execute"
                ),
                "priority": -9998
            }
        
        return {"override": False}


class LowVolumeContinuation:
    @staticmethod
    def detect(volume_ratio: float, obv_trend: str, price: float,
               ma25: float, ma99: float, down_energy: float) -> Dict:
        """Low volume continuation → force SHORT (block fake reversals)"""
        if (volume_ratio < 0.6
                and obv_trend == "NEGATIVE_EXTREME"
                and price < ma25
                and price < ma99):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": "Low volume continuation: no buyers → dump easier",
                "priority": -230
            }
        return {"override": False}


class AntiReversalGuard:
    @staticmethod
    def should_block_long(obv_trend: str, rsi6: float, volume_ratio: float,
                          ofi_bias: str, ofi_strength: float, long_dist: float) -> bool:
        if (obv_trend == "NEGATIVE_EXTREME"
                and rsi6 < 30
                and volume_ratio < 0.7):
            if rsi6 < 20 and volume_ratio < 0.6:
                return False
            if ofi_bias == "LONG" and ofi_strength > 0.5:
                return False
            if long_dist < 1.0:
                return False
            return True
        return False

    @staticmethod
    def should_block_short(obv_trend: str, rsi6: float, volume_ratio: float,
                           ofi_bias: str, ofi_strength: float, short_dist: float) -> bool:
        if (obv_trend == "POSITIVE_EXTREME"
                and rsi6 > 70
                and volume_ratio < 0.7):
            if rsi6 > 80 and volume_ratio < 0.6:
                return False
            if ofi_bias == "SHORT" and ofi_strength > 0.5:
                return False
            if short_dist < 1.0:
                return False
            return True
        return False


class CascadeDumpDetector:
    @staticmethod
    def detect(change_5m: float, short_liq: float, down_energy: float,
               volume_ratio: float) -> Dict:
        """Detect cascade dump with no support"""
        if (change_5m < -3
                and short_liq > 10
                and down_energy < 0.05
                and volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": "Cascade dump: no support + high liq target",
                "priority": -240
            }
        return {"override": False}


class FakeBounceTrap:
    @staticmethod
    def detect(rsi6: float, change_5m: float, volume_ratio: float,
               short_dist: float, long_dist: float,
               up_energy: float, down_energy: float,
               ofi_bias: str, ofi_strength: float) -> Dict:
        """Detect fake pump to trap longs"""
        if (rsi6 < 35
                and change_5m > 1.0
                and volume_ratio < 0.7
                and short_dist > long_dist
                and down_energy < up_energy * 0.3
                and ofi_bias == "LONG"
                and ofi_strength > 0.3):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": "Fake bounce: weak pump to trap longs → dump incoming",
                "priority": -235
            }
        return {"override": False}


class PostDropBounceOverride:
    """
    🔥 Memaksa LONG setelah drop >3.5% dalam 5m, volume rendah, dan OFI tidak SHORT kuat.
    Priority -140.
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, ofi_bias: str,
               ofi_strength: float, short_liq: float) -> Dict:
        if change_5m < -3.5 and volume_ratio < 0.6:
            if short_liq < 2.0:
                pass
            else:
                if ofi_bias != "SHORT" or ofi_strength < 0.6:
                    return {
                        "override": True,
                        "bias": "LONG",
                        "reason": (
                            f"Exhaustion drop: price dropped {change_5m:.1f}% with low volume "
                            f"({volume_ratio:.2f}x), no strong selling → bounce likely"
                        ),
                        "priority": -140
                    }
        return {"override": False}


class OrderBookSlope:
    @staticmethod
    def calculate(order_book: Dict) -> Tuple[float, float]:
        """Compute bid and ask slopes from top 10 levels"""
        if not order_book or not order_book.get("bids") or not order_book.get("asks"):
            return 0.0, 0.0

        bids = order_book["bids"][:10]
        asks = order_book["asks"][:10]

        bid_slope = 0.0
        ask_slope = 0.0

        for i in range(1, len(bids)):
            price_diff = bids[0][0] - bids[i][0]
            volume = bids[i][1]
            if price_diff > 0:
                bid_slope += volume / price_diff

        for i in range(1, len(asks)):
            price_diff = asks[i][0] - asks[0][0]
            volume = asks[i][1]
            if price_diff > 0:
                ask_slope += volume / price_diff

        return bid_slope, ask_slope

    @staticmethod
    def signal(bid_slope: float, ask_slope: float) -> Dict:
        """Return slope bias if one side is significantly stronger"""
        if bid_slope > ask_slope * 2:
            return {"bias": "SHORT", "reason": "Strong bid wall → resistance above"}
        if ask_slope > bid_slope * 2:
            return {"bias": "LONG", "reason": "Thin asks → easy pump"}
        return {"bias": "NEUTRAL", "reason": "Balanced order book"}


class AskBidSlopeImbalanceDetector:
    """
    🔥 RLSUSDT PATTERN: ask_slope 4-5x lebih besar dari bid_slope
    
    Ini artinya sell wall jauh lebih tebal dari buy wall.
    Harga tidak bisa naik karena ada tembok jual raksasa.
    
    Current logic hanya trigger di 2x — upgrade ke detector terpisah
    dengan priority lebih tinggi untuk rasio ekstrem (>3x).
    
    Priority: -210 (di atas EnergyGapTrap -215)
    """
    @staticmethod
    def detect(ask_slope: float, bid_slope: float,
               change_5m: float, rsi6: float,
               down_energy: float, up_energy: float) -> Dict:
        
        if bid_slope <= 0 or ask_slope <= 0:
            return {"override": False}
        
        ask_bid_ratio = ask_slope / bid_slope
        bid_ask_ratio = bid_slope / ask_slope
        
        # Sell wall sangat tebal (>3x) + harga turun = SHORT kuat
        if (ask_bid_ratio > 3.0 and
            change_5m < 0 and
            rsi6 < 50):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Ask slope dominance: ask={ask_slope:.0f} vs bid={bid_slope:.0f} (ratio {ask_bid_ratio:.1f}x), harga turun {change_5m:.1f}% → sell wall raksasa, dump continues",
                "priority": -210
            }
        
        # Buy wall sangat tebal (>3x) + harga naik = LONG kuat
        if (bid_ask_ratio > 3.0 and
            change_5m > 0 and
            rsi6 > 50):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Bid slope dominance: bid={bid_slope:.0f} vs ask={ask_slope:.0f} (ratio {bid_ask_ratio:.1f}x), harga naik {change_5m:.1f}% → buy wall raksasa, pump continues",
                "priority": -210
            }
        
        return {"override": False}


class LatencyArbitragePredictor:
    @staticmethod
    def predict_next_price(price: float, change_5m: float, up_energy: float,
                           down_energy: float, latency_ms: float) -> float:
        """Estimate current price based on latency"""
        velocity = change_5m / 300000.0

        if down_energy < up_energy:
            velocity *= 1.5
        elif up_energy < down_energy:
            velocity *= 1.5

        predicted = price * (1.0 + velocity * latency_ms)
        return predicted

    @staticmethod
    def is_safe(bias: str, current_price: float, predicted_price: float) -> bool:
        """Check if bias is still valid given predicted price"""
        if bias == "LONG" and predicted_price < current_price * 0.99:
            return False
        if bias == "SHORT" and predicted_price > current_price * 1.01:
            return False
        return True


class ProbabilisticEngine:
    def __init__(self):
        self.score_long = 0.0
        self.score_short = 0.0

    def add(self, bias: str, weight: float):
        if bias == "LONG":
            self.score_long += weight
        elif bias == "SHORT":
            self.score_short += weight

    def result(self) -> Tuple[str, float]:
        total = self.score_long + self.score_short
        if total == 0:
            return "NEUTRAL", 0.5
        prob_long = self.score_long / total
        prob_short = self.score_short / total
        if prob_long > prob_short:
            return "LONG", prob_long
        else:
            return "SHORT", prob_short


class PositionSizer:
    @staticmethod
    def size(confidence: float, trap_strength: float, volume_ratio: float) -> float:
        """Return position size multiplier (1.0 = base)"""
        base = 1.0

        if confidence > 0.8:
            base *= 1.5
        elif confidence < 0.6:
            base *= 0.5

        base *= (1.0 + trap_strength)

        if volume_ratio < 0.5:
            base *= 0.7

        return min(base, 2.0)


class TimeDecayFilter:
    @staticmethod
    def apply(new_bias: str) -> str:
        global LAST_SIGNAL, LAST_SIGNAL_TIME
        now = time.time()

        if LAST_SIGNAL is None:
            LAST_SIGNAL = new_bias
            LAST_SIGNAL_TIME = now
            return new_bias

        if new_bias == LAST_SIGNAL:
            LAST_SIGNAL_TIME = now
            return new_bias

        if (now - LAST_SIGNAL_TIME) < SIGNAL_PERSISTENCE_SEC:
            return LAST_SIGNAL

        LAST_SIGNAL = new_bias
        LAST_SIGNAL_TIME = now
        return new_bias

# ================= OVERBOUGHT/OVERSOLD TRAPS =================

class OverboughtDistributionTrap:
    @staticmethod
    def detect(rsi6: float, short_dist: float, long_dist: float, volume_ratio: float,
               down_energy: float, up_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float, agg: float = 0.5) -> Dict:
        # ============================================================
        # 🔥 PATCH AIOTUSDT: Jika short_liq dekat DAN down_energy=0
        # ini adalah SHORT SQUEEZE SETUP, bukan distribusi!
        # Jangan paksa SHORT dalam kondisi ini.
        # ============================================================
        if short_dist < 4.0 and short_dist < long_dist and down_energy < 0.01:
            return {"override": False}  # biarkan squeeze detector yang handle

        # 🔥 PATCH #2: agg = 1.00 (100% BUY) → tidak mungkin distribusi
        # Distribusi = smart money JUAL ke retail. Kalau 100% trades BUY,
        # artinya tidak ada yang jual = bukan distribusi.
        if agg > 0.85 and down_energy < 0.01:
            return {"override": False}

        # Guard: agg tinggi + up_energy ada = momentum beli nyata
        if agg > 0.7 and up_energy > 0.3 and short_dist < long_dist:
            return {"override": False}

        # PATCH: short liq SANGAT dekat (<0.5%) dan down_energy=0
        if short_dist < 0.5 and down_energy < 0.01 and change_5m > 0:
            return {"override": False}

        # EXTREME OVERBOUGHT
        if rsi6 > 85 and volume_ratio < 0.6 and down_energy < 0.01:
            if short_dist < 1.0 and ofi_bias == "SHORT" and ofi_strength > 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme overbought: RSI {rsi6:.1f} > 85, volume {volume_ratio:.2f}x, "
                    f"no sellers → forced dump"
                ),
                "priority": -262
            }

        if short_dist < 2.0 and short_dist < long_dist:
            if rsi6 > 75 and volume_ratio < 0.7:
                pass
            else:
                return {"override": False}

        # Kasus 1
        if (rsi6 > 70
                and short_dist < 2.0
                and volume_ratio < 0.8
                and down_energy < 0.1
                and up_energy < 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Overbought short squeeze trap: RSI {rsi6:.1f} overbought, "
                    f"short liq close, low volume, no bids → akan dump"
                ),
                "priority": -261
            }

        # Kasus 2
        if (rsi6 > 70
                and ofi_bias == "LONG"
                and ofi_strength > 0.6
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Overbought distribution: RSI {rsi6:.1f} + OFI LONG {ofi_strength:.2f} "
                    f"with low volume → smart money distributing"
                ),
                "priority": -261
            }

        # Kasus 3
        if rsi6 > 70 and change_5m > 2.0 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Overbought exhaustion: price up {change_5m:.1f}% with low volume "
                    f"→ pullback likely"
                ),
                "priority": -261
            }

        return {"override": False}


class OversoldSqueezeTrap:
    @staticmethod
    def detect(rsi6: float, long_dist: float, short_dist: float, volume_ratio: float,
               up_energy: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float) -> Dict:
        # EXTREME OVERSOLD
        if rsi6 < 15 and volume_ratio < 0.6 and up_energy < 0.01:
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme oversold: RSI {rsi6:.1f} < 15, volume {volume_ratio:.2f}x, "
                    f"no buyers → forced bounce"
                ),
                "priority": -262
            }

        if long_dist < 2.0 and long_dist < short_dist:
            return {"override": False}

        if (rsi6 < 30
                and long_dist < 2.0
                and volume_ratio < 0.8
                and up_energy < 0.1
                and down_energy < 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Oversold squeeze trap: RSI {rsi6:.1f} oversold, long liq close "
                    f"({long_dist}%), low volume, no asks → akan pump"
                ),
                "priority": -261
            }

        return {"override": False}


class EmptyBookTrapDetector:
    @staticmethod
    def detect(down_energy: float, up_energy: float, short_dist: float, long_dist: float,
               rsi6_5m: float, volume_ratio: float, obv_trend: str, rsi6: float,
               ofi_bias: str, ofi_strength: float,
               funding_rate: float = None) -> Dict:  # ← TAMBAH parameter funding_rate
        if short_dist < 0.5:
            return {"override": False}

        # CABANG LONG (bid kosong)
        if down_energy < 0.1 and short_dist < 2.0:
            # 🔥 PATCH: Jangan paksa LONG saat funding sangat negatif
            if funding_rate is not None and funding_rate < -0.003:
                return {"override": False}  # funding sangat negatif = akan dump
            
            if (rsi6 > 90 or rsi6_5m > 90) and volume_ratio < 1.5:
                return {"override": False}
            if rsi6 > 75 and obv_trend == "POSITIVE_EXTREME" and volume_ratio < 0.8:
                return {"override": False}
            if rsi6_5m >= 75 and volume_ratio < 0.6:
                return {"override": False}
            if long_dist < short_dist:
                return {"override": False}
            if ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Empty Book Trap: No bid support ({down_energy:.2f}) + "
                    f"Short Liq dekat ({short_dist:.2f}%) → Rawan Short Squeeze"
                ),
                "priority": -260
            }

        # CABANG SHORT (ask kosong)
        if up_energy < 0.1 and long_dist < 2.0:
            # 🔥 PATCH: Jangan paksa SHORT saat funding sangat positif
            if funding_rate is not None and funding_rate > 0.003:
                return {"override": False}  # funding sangat positif = akan pump
            
            if (rsi6 < 10 or rsi6_5m < 10) and volume_ratio < 1.5:
                return {"override": False}
            if rsi6 < 25 and obv_trend == "NEGATIVE_EXTREME" and volume_ratio < 0.8:
                return {"override": False}
            if rsi6_5m <= 25 and volume_ratio < 0.6:
                return {"override": False}
            if short_dist < long_dist:
                return {"override": False}
            if long_dist < 0.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Empty Book Trap: No ask resistance ({up_energy:.2f}) + "
                    f"Long Liq dekat ({long_dist:.2f}%) → Rawan Long Squeeze"
                ),
                "priority": -260
            }

        return {"override": False}


class ExhaustedLiquidityReversal:
    """
    🔥 DETECTS WHEN LIQUIDITY TARGET IS NEARLY EXHAUSTED AND MARKET OVERBOUGHT/OVERSOLD
    Priority -1060
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, volume_ratio: float,
               rsi6_5m: float, ofi_bias: str, ofi_strength: float) -> Dict:
        if long_dist < 0.5 and rsi6 < 25 and volume_ratio < 1.0:
            return {"override": False}
        if short_dist < 0.5 and rsi6 > 85 and volume_ratio < 1.0:
            return {"override": False}

        if short_dist < 0.5 and rsi6 > 70 and volume_ratio < 1.0:
            if volume_ratio < 0.7 and rsi6_5m > 60:
                return {"override": False}
            if volume_ratio < 0.7 and ofi_bias == "LONG" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Exhausted liquidity reversal: short liq {short_dist:.2f}% sudah hampir habis, "
                    f"RSI {rsi6:.1f} overbought → HFT akan ambil long stop setelah sapu short"
                ),
                "priority": -1060
            }

        if long_dist < 0.5 and rsi6 < 30 and volume_ratio < 1.0:
            if volume_ratio < 0.7 and rsi6_5m < 40:
                return {"override": False}
            if volume_ratio < 0.7 and ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Exhausted liquidity reversal: long liq {long_dist:.2f}% sudah hampir habis, "
                    f"RSI {rsi6:.1f} oversold → HFT akan ambil short stop setelah sapu long"
                ),
                "priority": -1060
            }

        return {"override": False}


class NearExhaustedLiquidityReversal:
    """
    🔥 DETECTS WHEN LIQUIDITY TARGET IS NEARLY EXHAUSTED (<1.5%) AND MARKET OVERBOUGHT/OVERSOLD
    Priority -1055
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, volume_ratio: float,
               rsi6_5m: float, ofi_bias: str, ofi_strength: float) -> Dict:
        if long_dist < 1.5 and rsi6 < 15 and volume_ratio < 1.0:
            return {"override": False}
        if short_dist < 1.5 and rsi6 > 85 and volume_ratio < 1.0:
            return {"override": False}

        if short_dist < 1.5 and rsi6 > 70 and volume_ratio < 1.0:
            if volume_ratio < 0.7 and rsi6_5m > 60:
                return {"override": False}
            if volume_ratio < 0.7 and ofi_bias == "LONG" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Near exhausted liquidity reversal: short liq {short_dist:.2f}% sudah mendekati habis, "
                    f"RSI {rsi6:.1f} overbought → HFT akan ambil long stop setelah sapu short"
                ),
                "priority": -1055
            }

        if long_dist < 1.5 and rsi6 < 30 and volume_ratio < 1.0:
            if volume_ratio < 0.7 and rsi6_5m < 40:
                return {"override": False}
            if volume_ratio < 0.7 and ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Near exhausted liquidity reversal: long liq {long_dist:.2f}% sudah mendekati habis, "
                    f"RSI {rsi6:.1f} oversold → HFT akan ambil short stop setelah sapu long"
                ),
                "priority": -1055
            }

        return {"override": False}


class ShortSqueezeTrapOverride:
    """
    🔥 Mencegah SHORT trap pada long liq dekat ketika ada buy pressure dan OFI SHORT (short trapped).
    Priority -1060
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, up_energy: float,
               down_energy: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float, change_5m: float) -> Dict:
        if (long_liq < short_liq
                and long_liq < 2.0
                and up_energy > 1.0
                and down_energy == 0
                and volume_ratio < 1.0
                and ofi_bias == "SHORT"
                and ofi_strength > 0.6
                and change_5m > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Short squeeze trap: long liq {long_liq:.2f}% closer, "
                    f"up_energy={up_energy:.2f}, OFI SHORT {ofi_strength:.2f} "
                    f"→ short sellers trapped, squeeze up"
                ),
                "priority": -1060
            }

        if (short_liq < long_liq
                and short_liq < 2.0
                and down_energy > 1.0
                and up_energy == 0
                and volume_ratio < 1.0
                and ofi_bias == "LONG"
                and ofi_strength > 0.6
                and change_5m < 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Long squeeze trap: short liq {short_liq:.2f}% closer, "
                    f"down_energy={down_energy:.2f}, OFI LONG {ofi_strength:.2f} "
                    f"→ long sellers trapped, dump down"
                ),
                "priority": -1060
            }

        return {"override": False}


# ================= LECTURER'S SARAN LOGIC =================

class LiquidityMagnetContinuation:
    """
    🔥 LIQUIDITY MAGNET MOMENTUM OVERRIDE
    Priority -1000
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, change_5m: float,
               up_energy: float, down_energy: float, volume_ratio: float) -> Dict:
        if (short_dist < 0.8
                and change_5m > 4.0
                and up_energy > 0
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity Magnet Continuation: Short liq {short_dist:.2f}% terlalu dekat "
                    f"+ momentum {change_5m:.1f}% → HFT akan naikin dikit lagi buat sapu short stop sebelum dump"
                ),
                "priority": -1000
            }

        if (long_dist < 0.8
                and change_5m < -4.0
                and down_energy > 0
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity Magnet Continuation: Long liq {long_dist:.2f}% terlalu dekat "
                    f"+ momentum {change_5m:.1f}% → HFT akan turunin dikit lagi buat sapu long stop sebelum pump"
                ),
                "priority": -1000
            }

        return {"override": False}


class OFIAbsorptionSqueeze:
    """
    🚨 OFI SHORT bisa bullish saat squeeze
    Priority -950
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, change_5m: float,
               short_dist: float, long_dist: float) -> Dict:
        if (ofi_bias == "SHORT"
                and ofi_strength > 0.8
                and change_5m > 5
                and short_dist < 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OFI Absorption Squeeze: Heavy selling (OFI SHORT {ofi_strength:.2f}) absorbed "
                    f"+ price up {change_5m:.1f}% + short liq {short_dist:.2f}% "
                    f"→ squeeze continues, sell order = short baru masuk atau trapped short averaging"
                ),
                "priority": -950
            }

        if (ofi_bias == "LONG"
                and ofi_strength > 0.8
                and change_5m < -5
                and long_dist < 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OFI Absorption Squeeze: Heavy buying (OFI LONG {ofi_strength:.2f}) absorbed "
                    f"+ price down {change_5m:.1f}% + long liq {long_dist:.2f}% "
                    f"→ dump continues, buy order = long baru masuk atau trapped long averaging"
                ),
                "priority": -950
            }

        return {"override": False}


class VelocityDecayReversal:
    """
    ⚡ Velocity decay detector
    Priority -900
    """
    @staticmethod
    def detect(change_5m: float, change_30s: float,
               short_dist: float, long_dist: float) -> Dict:
        if (change_5m > 5
                and change_30s < 0.3
                and short_dist > 1.5):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Velocity Decay Reversal: 5m pump {change_5m:.1f}% tapi 30s cuma {change_30s:.1f}% "
                    f"+ short liq {short_dist:.2f}% sudah jauh → squeeze selesai, reversal incoming"
                ),
                "priority": -900
            }

        if (change_5m < -5
                and change_30s > -0.3
                and long_dist > 1.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Velocity Decay Reversal: 5m dump {change_5m:.1f}% tapi 30s cuma {change_30s:.1f}% "
                    f"+ long liq {long_dist:.2f}% sudah jauh → exhaustion selesai, bounce incoming"
                ),
                "priority": -900
            }

        return {"override": False}


class EmptyBookMomentum:
    """
    ⚡ down_energy = 0 bukan bearish saat momentum tinggi
    Priority -850
    """
    @staticmethod
    def detect(down_energy: float, up_energy: float, change_5m: float,
               short_dist: float, long_dist: float) -> Dict:
        if (down_energy == 0
                and change_5m > 3
                and short_dist < 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Empty Book Momentum: No seller pressure (down_energy=0) "
                    f"+ price up {change_5m:.1f}% + short liq {short_dist:.2f}% "
                    f"→ path ke atas kosong, squeeze continuation"
                ),
                "priority": -850
            }

        if (up_energy == 0
                and change_5m < -3
                and long_dist < 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Empty Book Momentum: No buyer pressure (up_energy=0) "
                    f"+ price down {change_5m:.1f}% + long liq {long_dist:.2f}% "
                    f"→ path ke bawah kosong, dump continuation"
                ),
                "priority": -850
            }

        return {"override": False}


class MasterSqueezeRule:
    """
    💎 MASTER RULE: GOLDEN RULE
    Priority -1100 (sekarang di bawah ExtremeFundingRateLongBan -1101)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, change_5m: float,
               down_energy: float, up_energy: float, volume_ratio: float) -> Dict:
        if (short_dist < 0.8
                and change_5m > 5
                and down_energy == 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"MASTER SQUEEZE RULE: Short liq {short_dist:.2f}% terlalu dekat "
                    f"+ momentum {change_5m:.1f}% + no seller (down_energy=0) "
                    f"→ LONG UNTIL SHORT LIQ SWEPT"
                ),
                "priority": -1100
            }

        if (long_dist < 0.8
                and change_5m < -5
                and up_energy == 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"MASTER SQUEEZE RULE: Long liq {long_dist:.2f}% terlalu dekat "
                    f"+ momentum {change_5m:.1f}% + no buyer (up_energy=0) "
                    f"→ SHORT UNTIL LONG LIQ SWEPT"
                ),
                "priority": -1100
            }

        return {"override": False}


class ExtremeRsi6OverboughtReversal:
    """
    🔥 RSI6 = 100 KILLER: Kondisi overbought absolut — tidak ada ruang naik lagi.
    
    Ketika RSI6 menyentuh 99-100, ini adalah kondisi paling ekstrem.
    Tidak peduli sinyal lain (termasuk funding ban), koreksi PASTI terjadi.
    
    Indikator:
    1. RSI6 >= 99 (overbought absolut)
    2. Volume turun (< 0.6x) — tidak ada buyer baru
    3. OFI LONG atau up_energy > 0 — trap sedang berlangsung
    
    Priority: -1103 (TERTINGGI MUTLAK — di atas MomentumVolumeSpike -1102!)
    Karena RSI6 = 100 adalah kondisi matematis ekstrem, tidak bisa dinegosiasikan.
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, up_energy: float) -> Dict:
        # RSI6 >= 99 adalah kondisi overbought absolut
        if rsi6 >= 99 and volume_ratio < 0.6:
            # Jika OFI LONG dan up_energy > 0, ini trap besar
            # Tidak peduli sinyal lain, harus SHORT
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"EXTREME RSI6 OVERBOUGHT REVERSAL: RSI6={rsi6:.1f} (99+), volume {volume_ratio:.2f}x, OFI {ofi_bias} → blow-off top, immediate dump",
                "priority": -1103
            }
        return {"override": False}


class ExtremeFundingRateLongBan:
    """
    🔥 CUSDT KILLER: Funding rate < -0.003 = LONG BAN ABSOLUT
    
    Pada level ini, longs membayar 0.3%+ per 8 jam.
    HFT PASTI akan dump untuk paksa liquidasi longs.
    Tidak ada sinyal teknikal yang bisa override ini.
    
    Skala funding rate:
    - Normal:    ±0.0001
    - Elevated:  ±0.001  
    - Extreme:   ±0.003  ← LONG BAN threshold
    - Critical:  ±0.005  ← CUSDT level
    
    Priority: -1101 (di atas MasterSqueezeRule -1100!)
    Ini satu-satunya sinyal yang override MasterSqueezeRule.
    """
    @staticmethod
    def detect(funding_rate: float, rsi6_5m: float,
               rsi14: float, change_5m: float) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # LONG BAN: Funding sangat negatif = semua longs terperangkap
        if funding_rate < -0.003:
            # Jika RSI juga overbought di semua TF = konfirmasi blow-off top
            if rsi6_5m > 70 or rsi14 > 70:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"EXTREME FUNDING LONG BAN: funding={funding_rate:.4f} (longs paying {abs(funding_rate)*100:.3f}%/8h), RSI5m={rsi6_5m:.1f}, RSI14={rsi14:.1f} overbought → HFT WILL DUMP to liquidate trapped longs, NO LONG ALLOWED",
                    "priority": -1101
                }
            # Bahkan tanpa RSI overbought, funding < -0.005 = ban absolut
            if funding_rate < -0.005:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"CRITICAL FUNDING LONG BAN: funding={funding_rate:.4f} = {abs(funding_rate)*100:.3f}%/8h → forced liquidation incoming regardless of other signals",
                    "priority": -1101
                }
        
        # SHORT BAN: Funding sangat positif = semua shorts terperangkap
        if funding_rate > 0.003:
            if rsi6_5m < 30 or rsi14 < 30:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"EXTREME FUNDING SHORT BAN: funding={funding_rate:.4f} (shorts paying {funding_rate*100:.3f}%/8h), RSI5m={rsi6_5m:.1f} oversold → HFT WILL PUMP to liquidate trapped shorts, NO SHORT ALLOWED",
                    "priority": -1101
                }
            if funding_rate > 0.005:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"CRITICAL FUNDING SHORT BAN: funding={funding_rate:.4f} = {funding_rate*100:.3f}%/8h → forced short liquidation incoming",
                    "priority": -1101
                }
        
        return {"override": False}


class BlowOffTopDetector:
    """
    🔥 RSI_5m > 90 bukan squeeze continuation — ini BLOW-OFF TOP.
    
    Perbedaan squeeze vs blow-off:
    - Squeeze:    RSI naik karena buying pressure nyata, short liq dekat
    - Blow-off:   RSI sangat tinggi karena FOMO, volume turun, funding negatif
    
    Indikator blow-off top:
    1. RSI_5m > 90 (ekstrem parah)
    2. RSI_14 > 75 (multi-bar overbought)  
    3. Volume turun (< 0.6x) — tidak ada buyer baru
    4. Funding negatif (longs sudah crowded)
    5. change_5m masih positif (momentum melambat)
    
    Priority: -1099 (tepat di bawah ExtremeFundingLongBan -1101)
    Ini lebih tinggi dari MasterSqueezeRule -1100.
    """
    @staticmethod
    def detect(rsi6_5m: float, rsi14: float, volume_ratio: float,
               funding_rate: float, change_5m: float,
               short_dist: float, up_energy: float) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # Blow-off top: semua kondisi terpenuhi
        is_blow_off = (
            rsi6_5m > 90 and
            rsi14 > 70 and
            volume_ratio < 0.6 and
            funding_rate < -0.001 and  # ada crowding
            change_5m > 0              # masih naik = FOMO terakhir
        )
        
        if is_blow_off:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Blow-off top: RSI5m={rsi6_5m:.1f}>90, RSI14={rsi14:.1f}>70, volume={volume_ratio:.2f}x turun, funding={funding_rate:.4f} negatif → FOMO top, reversal brutal incoming",
                "priority": -1099
            }
        
        # Mirror: blow-off bottom (capitulation)
        is_capitulation = (
            rsi6_5m < 10 and
            rsi14 < 30 and
            volume_ratio < 0.6 and
            funding_rate > 0.001 and
            change_5m < 0
        )
        
        if is_capitulation:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Capitulation bottom: RSI5m={rsi6_5m:.1f}<10, RSI14={rsi14:.1f}<30, volume={volume_ratio:.2f}x turun, funding={funding_rate:.4f} positif → panic bottom, bounce brutal incoming",
                "priority": -1099
            }
        
        return {"override": False}


class AbsoluteAggOverride:
    """
    🔥 KRIMINAL DETECTOR #1: agg = 1.00 (100% BUY) + down_energy = 0
    Priority -1098
    """
    @staticmethod
    def detect(agg: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, up_energy: float,
               funding_rate: float,
               long_dist: float = 99.0,    # ← TAMBAH
               short_dist: float = 99.0,   # ← TAMBAH
               obv_value: float = 0.0) -> Dict:  # ← TAMBAH
        
        # 🔥 PATCH JCTUSDT: Jika long_liq < short_liq, agg = 1.00 bisa jadi spoofed
        # HFT spoof agg dengan trade kecil semua BUY tapi order book distribusi
        # Guard: jika long_liq lebih dekat, jangan paksa LONG meski agg=1.00
        if long_dist < short_dist and long_dist < 3.0:
            return {"override": False}  # long_liq lebih dekat = akan dump
        
        # 🔥 PATCH: OBV sangat besar positif tapi agg=1.00 dengan long_liq dekat
        # = distribusi dengan spoof agg
        if obv_value > 500_000_000 and long_dist < short_dist:
            return {"override": False}
        
        if (agg >= 0.95
                and down_energy < 0.01
                and ofi_bias == "LONG"
                and ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"ABSOLUTE AGG: agg={agg:.2f} (100% buy trades), "
                    f"down_energy=0, OFI LONG {ofi_strength:.2f} → tidak ada seller, forced LONG"
                ),
                "priority": -1098
            }

        if (agg <= 0.05
                and up_energy < 0.01
                and ofi_bias == "SHORT"
                and ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"ABSOLUTE AGG: agg={agg:.2f} (100% sell trades), "
                    f"up_energy=0, OFI SHORT {ofi_strength:.2f} → tidak ada buyer, forced SHORT"
                ),
                "priority": -1098
            }

        return {"override": False}


class RSI5mVs1mDivergence:
    """
    🔥 DUSDT PATTERN: RSI 5m jauh lebih oversold dari RSI 1m
    Priority -1068
    """
    @staticmethod
    def detect(rsi6_1m: float, rsi6_5m: float,
               down_energy: float, up_energy: float,
               agg: float, funding_rate: float) -> Dict:
        if (rsi6_5m < 25
                and rsi6_1m > 28
                and rsi6_1m - rsi6_5m > 8
                and down_energy < 0.1
                and up_energy > 0):
            funding_bonus = (
                " + funding negatif (crowded short)"
                if funding_rate is not None and funding_rate < -0.001
                else ""
            )
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"RSI multi-TF divergence: RSI_5m={rsi6_5m:.1f} oversold tapi "
                    f"RSI_1m={rsi6_1m:.1f} recovering, no sellers{funding_bonus} → bounce imminent"
                ),
                "priority": -1068
            }

        if (rsi6_5m > 75
                and rsi6_1m < 72
                and rsi6_5m - rsi6_1m > 8
                and up_energy < 0.1
                and down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"RSI multi-TF divergence: RSI_5m={rsi6_5m:.1f} overbought tapi "
                    f"RSI_1m={rsi6_1m:.1f} declining, no buyers → dump imminent"
                ),
                "priority": -1068
            }

        return {"override": False}


class FundingNegativeAggLongOverride:
    """
    🔥 DUSDT PATTERN: Funding negatif + agg tinggi + OFI LONG
    Priority -1072
    """
    @staticmethod
    def detect(funding_rate: float, agg: float, ofi_bias: str,
               ofi_strength: float, down_energy: float,
               rsi6_5m: float, volume_ratio: float) -> Dict:
        if funding_rate is None:
            return {"override": False}

        if (funding_rate < -0.001
                and agg > 0.65
                and ofi_bias == "LONG"
                and ofi_strength > 0.5
                and down_energy < 0.1
                and rsi6_5m < 40):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Funding crowded short squeeze: funding={funding_rate:.4f} (semua orang short), "
                    f"agg={agg:.2f} (majority buy), OFI LONG {ofi_strength:.2f}, "
                    f"no sellers → short squeeze incoming"
                ),
                "priority": -1072
            }

        if (funding_rate > 0.001
                and agg < 0.35
                and ofi_bias == "SHORT"
                and ofi_strength > 0.5
                and up_energy < 0.1
                and rsi6_5m > 60):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Funding crowded long dump: funding={funding_rate:.4f} (semua orang long), "
                    f"agg={agg:.2f} (majority sell), OFI SHORT {ofi_strength:.2f}, "
                    f"no buyers → long liquidation incoming"
                ),
                "priority": -1072
            }

        return {"override": False}


class ExtremeOversoldIgnoreLiquidity:
    """
    🔥 Memaksa LONG ketika RSI6 < 10, abaikan liquidity proximity.
    Priority -1080
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float) -> Dict:
        if rsi6 < 10:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme oversold (RSI6 {rsi6:.1f} < 10) → ignore liquidity, forced bounce",
                "priority": -1080
            }
        return {"override": False}


class ExtremeOverboughtIgnoreLiquidity:
    """
    🔥 Memaksa SHORT ketika RSI6 > 90, abaikan liquidity proximity.
    Priority -1080
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float) -> Dict:
        if rsi6 > 90:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme overbought (RSI6 {rsi6:.1f} > 90) → ignore liquidity, forced dump",
                "priority": -1080
            }
        return {"override": False}


class FreshShortTrapDetector:
    """
    🔥 Mendeteksi "Fresh Short Trap" - short baru yang masuk setelah dump panjang.
    
    Binance logic: BUKAN lihat mana likuiditas terdekat (lama), tapi lihat
    "SIAPA YANG BARU MASUK DAN BISA DIBUNUH SEKARANG"
    
    Skenario khas (ARIA, DUSDT, AIOT):
    1. Market sudah dump panjang (change_5m < -2.0)
    2. RSI oversold (rsi6 < 30)
    3. OFI mulai LONG (buyer masuk, market STOP turun)
    4. Volume rendah (<0.8) → market tipis, mudah dipompa
    5. Ada short liq yang lebih dekat? TIDAK relevan - fresh short yang baru masuk targetnya
    
    Priority: -1082 (antara ExtremeOversoldIgnoreLiquidity -1080 dan ExtremeFundingRateTrap -1085)
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float, ofi_bias: str, volume_ratio: float,
               long_liq: float, short_liq: float, agg: float, up_energy: float,
               down_energy: float = 0.0) -> Dict:
        # Kondisi utama: fresh short trap (LONG bias)
        # 🔥 SARAN DOSEN: Turunkan threshold change_5m dari -2.0 ke -1.5 agar lebih sensitif
        if (change_5m < -1.5          # baru dump (turunkan threshold dari -2.0)
                and rsi6 < 30          # oversold
                and ofi_bias == "LONG" # buyer mulai masuk (market STOP turun)
                and volume_ratio < 0.8 # volume rendah (market tipis)
                and agg < 0.7):        # tidak terlalu banyak buy (bukan FOMO besar)
            # Pengecualian: jika long liq sudah sangat dekat (<1.5%) dan up_energy tinggi -> bisa jadi short squeeze biasa
            if long_liq < 1.5 and up_energy > 1.0:
                return {"override": False}
            
            # Bonus: jika ada short liq juga dekat, tapi fresh short sudah masuk -> tetap LONG
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Fresh Short Trap: market dump {change_5m:.1f}%, RSI {rsi6:.1f} oversold, "
                    f"OFI LONG {ofi_bias}, volume rendah ({volume_ratio:.2f}x) → short baru masuk, "
                    f"Binance akan pump untuk bunuh mereka"
                ),
                "priority": -1082
            }
        
        # Mirror untuk fresh long trap (jarang, tapi simetris)
        if (change_5m > 2.0
                and rsi6 > 70
                and ofi_bias == "SHORT"
                and volume_ratio < 0.8
                and agg > 0.3):
            if short_liq < 1.5 and down_energy > 1.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Fresh Long Trap: market pump {change_5m:.1f}%, RSI {rsi6:.1f} overbought, "
                    f"OFI SHORT, volume rendah → fresh long masuk, Binance akan dump"
                ),
                "priority": -1082
            }
        
        return {"override": False}


class CrowdedLongDistribution:
    """
    🔥 Deteksi ketika semua orang sudah LONG → paksa SHORT.
    Priority -165
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, change_5m: float) -> Dict:
        if (rsi6 > 70
                and volume_ratio < 0.9
                and (ofi_bias == "LONG" or ofi_bias == "NEUTRAL")
                and change_5m > 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Crowded long distribution: RSI {rsi6:.1f}, volume {volume_ratio:.2f}x, "
                    f"OFI {ofi_bias} → smart money distributing, forced SHORT"
                ),
                "priority": -165
            }
        return {"override": False}


class CrowdedShortAccumulation:
    """
    🔥 Deteksi ketika semua orang sudah SHORT → paksa LONG.
    Priority -165
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, change_5m: float,
               ask_slope: float = 0, bid_slope: float = 0,          # ← TAMBAH
               obv_value: float = 0, rsi6_5m: float = 50) -> Dict:  # ← TAMBAH
        
        # 🔥 PATCH RLSUSDT: Jangan LONG kalau sell wall jauh lebih tebal
        if bid_slope > 0 and ask_slope > bid_slope * 2.5:
            return {"override": False}  # sell wall raksasa, bukan accumulation
        
        # 🔥 PATCH: Jangan LONG kalau OBV sangat negatif (distribusi sudah lama)
        if obv_value < -20_000_000:
            return {"override": False}  # distribusi panjang, bukan crowded short
        
        # 🔥 PATCH: RSI_5m juga oversold = falling knife, bukan bounce
        # Jika RSI_1m DAN RSI_5m keduanya < 25 = trend turun kuat, jangan LONG
        if rsi6 < 25 and rsi6_5m < 25 and change_5m < -1.5:
            return {"override": False}  # double oversold + turun = falling knife

        # Logic asli
        if (rsi6 < 30 and
            volume_ratio < 0.9 and
            (ofi_bias == "SHORT" or ofi_bias == "NEUTRAL") and
            change_5m < -1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Crowded short accumulation: RSI {rsi6:.1f}, volume {volume_ratio:.2f}x, "
                    f"OFI {ofi_bias} → smart money accumulating, forced LONG"
                ),
                "priority": -165
            }
        return {"override": False}


class HFTAlgoConsensusOverride:
    """
    🔥 Memaksa mengikuti arah HFT dan Algo Type ketika mereka konsisten.
    Priority -170
    """
    @staticmethod
    def detect(algo_bias: str, hft_bias: str, volume_ratio: float, change_5m: float,
               short_liq: float = 99.0, long_liq: float = 99.0) -> Dict:
        if algo_bias == hft_bias and algo_bias != "NEUTRAL":
            # Jika konsensus searah dengan liquidity, confidence ABSOLUTE
            liquidity_align = (
                (algo_bias == "LONG" and short_liq < long_liq) or
                (algo_bias == "SHORT" and long_liq < short_liq)
            )
            confidence = "ABSOLUTE" if liquidity_align else "HIGH"
            
            if volume_ratio < 0.7:
                if (algo_bias == "SHORT" and change_5m < 0) or (algo_bias == "LONG" and change_5m > 0):
                    return {
                        "override": True,
                        "bias": algo_bias,
                        "reason": (
                            f"HFT-Algo consensus: both {algo_bias}, volume {volume_ratio:.2f}x, "
                            f"price moving {change_5m:+.1f}%, liquidity_aligned={liquidity_align} → forcing {algo_bias}"
                        ),
                        "priority": -170,
                        "confidence": confidence
                    }
                else:
                    return {
                        "override": True,
                        "bias": algo_bias,
                        "reason": (
                            f"HFT-Algo consensus: both {algo_bias} with low volume "
                            f"({volume_ratio:.2f}x), liquidity_aligned={liquidity_align} → forcing {algo_bias}"
                        ),
                        "priority": -170,
                        "confidence": confidence
                    }
            else:
                # Volume normal/tinggi
                return {
                    "override": True,
                    "bias": algo_bias,
                    "reason": (
                        f"HFT-ALGO CONSENSUS: both {algo_bias}, aligned with liquidity={liquidity_align}, "
                        f"volume={volume_ratio:.2f}x"
                    ),
                    "priority": -170,
                    "confidence": confidence
                }
        return {"override": False}


class TwoPhaseHFTSweepDetector:
    """
    Mendeteksi rencana HFT 2 fase.
    Priority -1095
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float,
               down_energy: float, up_energy: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float) -> Dict:
        if (short_dist < 0.5
                and short_dist < long_dist * 0.1
                and rsi6 > 75
                and down_energy < 0.01
                and change_5m > 2.0):
            return {
                "override": True,
                "bias": "LONG",
                "phase_type": "TWO_PHASE_SWEEP_PHASE1",
                "reason": (
                    f"Two-phase HFT sweep: short liq {short_dist:.2f}% << long liq {long_dist:.2f}%, "
                    f"RSI {rsi6:.1f} overbought, no sellers "
                    f"→ HFT pump dulu untuk sapu short stop, baru dump"
                ),
                "priority": -1095
            }
        return {"override": False}


class VolumeSpikeBounceDetector:
    """
    🔥 KUNCI EDGEUSDT: Volume spike 4x+ dari MA10 saat oversold
    Priority -1092
    """
    @staticmethod
    def detect(latest_volume: float, volume_ma10: float, rsi6: float,
               up_energy: float, down_energy: float, agg: float,
               change_5m: float, long_liq: float, short_liq: float) -> Dict:
        if volume_ma10 <= 0:
            return {"override": False}

        volume_spike_ratio = latest_volume / volume_ma10

        if (volume_spike_ratio > 2.5
                and rsi6 < 30
                and up_energy > 0.5
                and down_energy < 0.1
                and agg > 0.55
                and change_5m < 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Volume spike bounce: volume {volume_spike_ratio:.1f}x MA10 saat oversold "
                    f"RSI {rsi6:.1f}, up_energy={up_energy:.2f}, agg={agg:.2f} (majority buy) "
                    f"→ institutional accumulation, bounce imminent"
                ),
                "priority": -1092
            }

        if (volume_spike_ratio > 2.5
                and rsi6 > 70
                and down_energy > 0.5
                and up_energy < 0.1
                and agg < 0.45
                and change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Volume spike distribution: volume {volume_spike_ratio:.1f}x MA10 saat overbought "
                    f"RSI {rsi6:.1f}, down_energy={down_energy:.2f}, agg={agg:.2f} (majority sell) "
                    f"→ institutional distribution, dump imminent"
                ),
                "priority": -1092
            }

        return {"override": False}


class ShortLiqProximityAggSqueeze:
    """
    🔥 AIOTUSDT EXACT PATTERN DETECTOR:
    
    Kondisi:
    - short_liq < 4% (dekat, bisa disapu)
    - short_liq < long_liq (short lebih dekat)
    - agg > 0.7 (mayoritas buy)
    - down_energy = 0 (tidak ada seller di book)
    - up_energy > 0 (ada buy pressure)
    - change_5m > 0 (momentum naik)
    
    Ini adalah DEFINISI short squeeze:
    Harga naik dengan buyer aktif, tidak ada seller,
    dan short stop hanya 3% di atas → HFT akan gas ke atas
    untuk sapu semua short.
    
    RSI overbought justru MEMPERKUAT squeeze karena:
    RSI tinggi = short sellers panik average → lebih banyak stop di atas.
    
    Priority: -1096 (di bawah AbsoluteAgg -1098, di atas TwoPhase -1095)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, agg: float,
               down_energy: float, up_energy: float, change_5m: float,
               rsi6: float, volume_ratio: float) -> Dict:

        # Core squeeze pattern
        if (short_dist < 4.0 and
            short_dist < long_dist and
            agg > 0.65 and
            down_energy < 0.01 and
            up_energy > 0.1 and
            change_5m > 0):
            
            # Tentukan kekuatan squeeze
            if short_dist < 2.0:
                strength = "EXTREME"
                priority = -1097
            elif short_dist < 3.0:
                strength = "STRONG"
                priority = -1096
            else:
                strength = "MODERATE"
                priority = -1094

            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Short liq proximity + agg squeeze ({strength}): short liq {short_dist:.2f}% dekat, agg={agg:.2f} (majority buy), down_energy={down_energy:.3f}, up_energy={up_energy:.2f}, price up {change_5m:.1f}% → HFT will sweep short stops",
                "priority": priority
            }

        # Mirror: long liq dekat + agg rendah + no buyer
        if (long_dist < 4.0 and
            long_dist < short_dist and
            agg < 0.35 and
            up_energy < 0.01 and
            down_energy > 0.1 and
            change_5m < 0):

            if long_dist < 2.0:
                priority = -1097
            elif long_dist < 3.0:
                priority = -1096
            else:
                priority = -1094

            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Long liq proximity + agg dump: long liq {long_dist:.2f}% dekat, agg={agg:.2f} (majority sell), up_energy={up_energy:.3f}, down_energy={down_energy:.2f}, price down {change_5m:.1f}% → HFT will sweep long stops",
                "priority": priority
            }

        return {"override": False}


class OFIConsistencyValidator:
    """
    🔥 FIX BUG AIOTUSDT: OFI display ≠ OFI JSON
    
    Masalah: OFI dihitung dengan window berbeda menghasilkan nilai berbeda.
    Satu window (display) bilang LONG 1.00, window lain (json) bilang NEUTRAL.
    
    Solusi: Gunakan agg sebagai tiebreaker.
    Jika agg > 0.7 dan OFI = NEUTRAL, upgrade OFI ke LONG.
    Jika agg < 0.3 dan OFI = NEUTRAL, upgrade OFI ke SHORT.
    
    Ini memastikan agg dan OFI konsisten.
    """
    @staticmethod
    def validate_and_fix(ofi_bias: str, ofi_strength: float,
                         agg: float, flow: float,
                         up_energy: float, down_energy: float) -> Dict:
        
        # Jika OFI NEUTRAL tapi agg sangat bullish + no seller
        if (ofi_bias == "NEUTRAL" and
            agg > 0.75 and
            up_energy > 0 and
            down_energy < 0.01):
            # Upgrade OFI ke LONG berdasarkan agg
            inferred_strength = min((agg - 0.5) * 2, 1.0)  # scale 0.5→1.0 ke 0→1.0
            return {
                "bias": "LONG",
                "strength": inferred_strength,
                "inferred": True,
                "reason": f"OFI inferred LONG from agg={agg:.2f}, up_energy={up_energy:.2f}, no sellers"
            }

        # Jika OFI NEUTRAL tapi agg sangat bearish + no buyer
        if (ofi_bias == "NEUTRAL" and
            agg < 0.25 and
            down_energy > 0 and
            up_energy < 0.01):
            inferred_strength = min((0.5 - agg) * 2, 1.0)
            return {
                "bias": "SHORT",
                "strength": inferred_strength,
                "inferred": True,
                "reason": f"OFI inferred SHORT from agg={agg:.2f}, down_energy={down_energy:.2f}, no buyers"
            }

        # OFI sudah valid, kembalikan apa adanya
        return {"bias": ofi_bias, "strength": ofi_strength, "inferred": False}


class DownEnergyZeroShortLiqClose:
    """
    🔥 Rule paling sederhana yang belum ada:
    
    Jika:
    - down_energy = 0 (TIDAK ADA SELLER di order book)
    - short_liq < 4% (target sweep dekat)
    - short_liq < long_liq (short lebih dekat dari long)
    
    Maka TIDAK MUNGKIN dump — tidak ada yang mau jual.
    HFT PASTI akan naik dulu untuk ambil short liq.
    
    Ini seharusnya override SEMUA sinyal overbought/distribusi.
    Priority: -1093 (tepat di atas TwoPhase -1095)
    """
    @staticmethod
    def detect(down_energy: float, short_dist: float, long_dist: float,
               up_energy: float, agg: float) -> Dict:

        if (down_energy < 0.01 and
            short_dist < 4.0 and
            short_dist < long_dist):
            
            # Konfirmasi dengan minimal satu sinyal beli
            has_buy_signal = (up_energy > 0.1 or agg > 0.55)
            
            if has_buy_signal:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Down energy zero + short liq close: down_energy={down_energy:.3f} (no sellers), short liq {short_dist:.2f}% < long liq {long_dist:.2f}%, up_energy={up_energy:.2f}, agg={agg:.2f} → path ke atas kosong, HFT sweep short stops",
                    "priority": -1093
                }

        # Mirror: up_energy=0 + long_liq dekat + ada sell signal
        if (up_energy < 0.01 and
            long_dist < 4.0 and
            long_dist < short_dist):

            has_sell_signal = (down_energy > 0.1 or agg < 0.45)

            if has_sell_signal:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Up energy zero + long liq close: up_energy={up_energy:.3f} (no buyers), long liq {long_dist:.2f}% < short liq {short_dist:.2f}%, down_energy={down_energy:.2f}, agg={agg:.2f} → path ke bawah kosong, HFT sweep long stops",
                    "priority": -1093
                }

        return {"override": False}


class AggMajorityBounce:
    """
    🔥 Ketika agg > 0.55, RSI oversold, long_liq dekat, volume rendah
    Priority -1072
    """
    @staticmethod
    def detect(agg: float, rsi6: float, long_liq: float, change_5m: float,
               up_energy: float, down_energy: float) -> Dict:
        if (agg > 0.55
                and rsi6 < 35
                and long_liq < 3.0
                and change_5m < 0
                and up_energy > down_energy
                and down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Agg majority bounce: agg={agg:.2f} (majority buy), RSI {rsi6:.1f} oversold, "
                    f"long liq {long_liq:.2f}%, up_energy={up_energy:.2f} → accumulation, bounce imminent"
                ),
                "priority": -1072
            }
        return {"override": False}


class OFIDivergenceTrap:
    """
    Ketika OFI SHORT kuat tapi down_energy = 0, harga naik, short liq dekat
    Priority -855
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               change_5m: float, short_dist: float, volume_ratio: float) -> Dict:
        if (ofi_bias == "SHORT"
                and ofi_strength > 0.3
                and down_energy < 0.01
                and change_5m > 0
                and short_dist < 1.5
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OFI divergence trap: OFI SHORT {ofi_strength:.2f} tapi down_energy=0 "
                    f"dan harga naik {change_5m:.1f}% → sell orders diserap buyer, squeeze lanjut"
                ),
                "priority": -855
            }
        return {"override": False}


class RSIEnergyDivergence:
    """
    RSI overbought tapi energy = 0 di kedua sisi = pasar frozen.
    Priority -856
    """
    @staticmethod
    def detect(rsi6: float, up_energy: float, down_energy: float,
               short_dist: float, long_dist: float) -> Dict:
        if (rsi6 > 75
                and up_energy == 0
                and down_energy == 0
                and short_dist < long_dist):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"RSI-Energy divergence: RSI {rsi6:.1f} overbought tapi energy nol di keduanya, "
                    f"short liq {short_dist:.2f}% lebih dekat → frozen market, harga ke short liq"
                ),
                "priority": -856
            }
        return {"override": False}


class HFTExplicitDumpOverride:
    """
    🚨 KRIMINAL DETECTOR: HFT 6% bias = SHORT dengan reason eksplisit
    Priority -1090
    """
    @staticmethod
    def detect(hft_bias: str, hft_reason: str, agg: float,
               ofi_bias: str, change_5m: float,
               funding_rate: float, volume_ratio: float) -> Dict:
        hft_dump_signal = (
            hft_bias == "SHORT"
            and hft_reason != ""
            and ("dump" in hft_reason.lower() or "energi down" in hft_reason.lower())
        )

        if not hft_dump_signal:
            return {"override": False}
        if agg > 0.35:
            return {"override": False}
        if ofi_bias != "SHORT":
            return {"override": False}
        if change_5m < 1.0:
            return {"override": False}

        return {
            "override": True,
            "bias": "SHORT",
            "reason": (
                f"HFT explicit dump signal: HFT={hft_reason}, agg={agg:.2f} (79%+ sell), "
                f"OFI SHORT, price pumped {change_5m:.1f}% → HFT distributing before dump"
            ),
            "priority": -1090
        }


class ExtremeFundingRateTrap:
    """
    💰 FUNDING RATE EXTREME TRAP
    Priority -1085
    """
    @staticmethod
    def detect(funding_rate: float, agg: float, hft_bias: str,
               ofi_bias: str, ofi_strength: float,
               change_5m: float, volume_ratio: float) -> Dict:
        if funding_rate is None:
            return {"override": False}

        if (funding_rate < -0.005
                and agg < 0.35
                and hft_bias == "SHORT"
                and ofi_bias == "SHORT"
                and change_5m > 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme funding trap SHORT: funding {funding_rate:.4f} "
                    f"(longs paying {abs(funding_rate) * 100:.2f}%), "
                    f"agg={agg:.2f}, HFT SHORT → mass long liquidation incoming"
                ),
                "priority": -1085
            }

        if (funding_rate > 0.005
                and agg > 0.65
                and hft_bias == "LONG"
                and ofi_bias == "LONG"
                and change_5m < -1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme funding trap LONG: funding {funding_rate:.4f} "
                    f"(shorts paying {funding_rate * 100:.2f}%), "
                    f"agg={agg:.2f}, HFT LONG → mass short liquidation incoming"
                ),
                "priority": -1085
            }

        return {"override": False}


class ExtremeFundingLiquidityOverride:
    """
    🔥 Funding rate sangat negatif tapi long liq lebih dekat dari short liq → SHORT
    Priority -1086
    """
    @staticmethod
    def detect(funding_rate: float, long_liq: float, short_liq: float,
               hft_bias: str, agg: float, change_5m: float) -> Dict:
        if funding_rate is None:
            return {"override": False}

        if (funding_rate < -0.005
                and long_liq < short_liq
                and hft_bias == "SHORT"
                and agg < 0.8
                and abs(change_5m) < 2.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme negative funding ({funding_rate:.4f}) but long liq closer "
                    f"({long_liq:.2f}% < {short_liq:.2f}%) "
                    f"→ HFT will take long stops first, forced SHORT"
                ),
                "priority": -1086
            }

        if (funding_rate > 0.005
                and short_liq < long_liq
                and hft_bias == "LONG"
                and agg > 0.2
                and abs(change_5m) < 2.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme positive funding ({funding_rate:.4f}) but short liq closer "
                    f"({short_liq:.2f}% < {long_liq:.2f}%) "
                    f"→ HFT will take short stops first, forced LONG"
                ),
                "priority": -1086
            }

        return {"override": False}


class AggFlowDivergenceFilter:
    """
    🔥 agg/flow < 0.25 (75%+ trades adalah SELL) dan harga pump > 2% = distribusi
    Priority -175
    """
    @staticmethod
    def detect(agg: float, change_5m: float, ofi_bias: str,
               hft_bias: str, volume_ratio: float) -> Dict:
        if (agg < 0.25
                and change_5m > 2.0
                and ofi_bias == "SHORT"
                and volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Agg/Flow divergence: {agg:.2f} agg (75%+ sell trades) with price up "
                    f"{change_5m:.1f}% + low volume → distribution, dump incoming"
                ),
                "priority": -175
            }

        if (agg > 0.75
                and change_5m < -2.0
                and ofi_bias == "LONG"
                and volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Agg/Flow divergence: {agg:.2f} agg (75%+ buy trades) with price down "
                    f"{change_5m:.1f}% + low volume → accumulation, bounce incoming"
                ),
                "priority": -175
            }

        return {"override": False}


class LiquidityProximityStrict:
    @staticmethod
    def detect(short_dist: float, long_dist: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float, rsi6: float, obv_trend: str,
               change_5m: float) -> Dict:
        if long_dist < 1.0 and long_dist < short_dist:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Ultra close long liq ({long_dist:.2f}%) → forced SHORT regardless",
                "priority": -1051
            }
        if short_dist < 1.0 and short_dist < long_dist:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Ultra close short liq ({short_dist:.2f}%) → forced LONG regardless",
                "priority": -1051
            }

        if volume_ratio < 1.5:
            if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
                return {"override": False}

            if short_dist < 2.0 and short_dist < long_dist:
                if ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.6:
                    return {"override": False}
                if rsi6 < 20 and obv_trend == "NEGATIVE_EXTREME" and volume_ratio < 0.6:
                    return {"override": False}
                if rsi6 > 80:
                    return {"override": False}
                if rsi6 > 65 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                    return {"override": False}
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Strict liquidity proximity: short liq {short_dist:.2f}% < 2%, forcing LONG",
                    "priority": -1050
                }

            if long_dist < 2.0 and long_dist < short_dist:
                if rsi6 < 35 and ofi_bias == "LONG" and ofi_strength > 0.7 and volume_ratio < 0.7:
                    return {"override": False}
                if ofi_bias == "NEUTRAL" and rsi6_5m < 35 and volume_ratio < 0.6:
                    return {"override": False}
                if rsi6 < 35 and volume_ratio < 0.6 and ofi_strength < 0.5 and change_5m < -1.0:
                    return {"override": False}
                if ofi_bias == "LONG" and ofi_strength > 0.7 and volume_ratio < 0.6:
                    return {"override": False}
                if rsi6 > 80 and obv_trend == "POSITIVE_EXTREME" and volume_ratio < 0.6:
                    return {"override": False}
                if rsi6 < 20:
                    return {"override": False}
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Strict liquidity proximity: long liq {long_dist:.2f}% < 2%, forcing SHORT",
                    "priority": -1050
                }

        return {"override": False}


class LiquidityMagnetOverride:
    """
    💎 FORCE DIRECTION BASED ON LIQUIDITY MAGNET WHEN CLOSE (<3%) AND VOLUME LOW (<0.7x)
    Priority -1075
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, volume_ratio: float,
               rsi6_5m: float, change_5m: float) -> Dict:
        if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
            return {"override": False}

        if short_dist < 3.0 and volume_ratio < 0.7 and short_dist < long_dist:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity squeeze override: short liq {short_dist:.2f}% dekat dengan volume "
                    f"{volume_ratio:.2f}x, lebih dekat dari long liq "
                    f"→ force LONG (HFT will sweep short stops)"
                ),
                "priority": -1075
            }

        if long_dist < 3.0 and volume_ratio < 0.7 and long_dist < short_dist:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity squeeze override: long liq {long_dist:.2f}% dekat dengan volume "
                    f"{volume_ratio:.2f}x, lebih dekat dari short liq "
                    f"→ force SHORT (HFT will dump to sweep long stops)"
                ),
                "priority": -1075
            }

        return {"override": False}


class FundingRateCrowdedShortOverride:
    """
    🔥 Memaksa LONG ketika funding rate sangat negatif (crowded short).
    Priority -1076
    """
    @staticmethod
    def detect(funding_rate: float, rsi6: float, ofi_bias: str, ofi_strength: float,
               long_dist: float, short_dist: float, volume_ratio: float) -> Dict:
        if (funding_rate is not None
                and funding_rate < -0.001
                and rsi6 < 35
                and ofi_bias == "LONG"
                and ofi_strength > 0.4
                and short_dist < long_dist
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Crowded short trap: funding rate {funding_rate:.4f} sangat negatif, "
                    f"RSI {rsi6:.1f} oversold, OFI LONG {ofi_strength:.2f}, "
                    f"short liq {short_dist:.2f}% < long liq {long_dist:.2f}% "
                    f"→ shorts trapped, squeeze up"
                ),
                "priority": -1076
            }
        return {"override": False}


class AggConfirmedBounce:
    """
    🔥 Ketika agg > 0.55, RSI oversold, long_liq dekat = bounce kuat.
    Priority -1070
    """
    @staticmethod
    def detect(agg: float, rsi6: float, long_liq: float, short_liq: float,
               up_energy: float, down_energy: float, change_5m: float) -> Dict:
        if (agg > 0.55
                and rsi6 < 35
                and long_liq < 2.0
                and up_energy > down_energy
                and down_energy < 0.1
                and change_5m < 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Agg-confirmed bounce: agg={agg:.2f} (majority buy), RSI {rsi6:.1f} oversold, "
                    f"long liq {long_liq:.2f}% close, up_energy={up_energy:.2f} > down_energy={down_energy:.2f} "
                    f"→ HFT sweep long liq then pump"
                ),
                "priority": -1070
            }

        if (agg < 0.45
                and rsi6 > 65
                and short_liq < 2.0
                and down_energy > up_energy
                and up_energy < 0.1
                and change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Agg-confirmed dump: agg={agg:.2f} (majority sell), RSI {rsi6:.1f} overbought, "
                    f"short liq {short_liq:.2f}% close, down_energy={down_energy:.2f} > up_energy={up_energy:.2f} "
                    f"→ HFT sweep short liq then dump"
                ),
                "priority": -1070
            }

        return {"override": False}


class FlushExhaustionReversal:
    """
    🚀 Detects sharp drop with oversold, low volume, and no sellers → bounce.
    Priority -250
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float, volume_ratio: float,
               down_energy: float, long_dist: float) -> Dict:
        if (change_5m < -5.0
                and rsi6 < 30
                and volume_ratio < 0.7
                and down_energy < 0.05
                and long_dist < 3.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Flush exhaustion reversal: dropped {change_5m:.1f}% with low volume, "
                    f"RSI {rsi6:.1f} oversold, no sellers, long liq {long_dist}% close → bounce likely"
                ),
                "priority": -250
            }
        return {"override": False}


class ExtremeOverboughtContinuation:
    """
    🔥 Memaksa LONG ketika overbought ekstrem (RSI5m > 80), volume sangat rendah (<0.5x).
    Priority -200
    
    PATCH CUSDT: Tambah funding check dan RSI14 check untuk menghindari blow-off top trap.
    PATCH RSI6=100: Jangan paksa LONG jika RSI6 > 98 — biarkan ExtremeRsi6OverboughtReversal handle.
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               up_energy: float, short_liq: float,
               funding_rate: float = 0.0, rsi14: float = 50.0,
               rsi6: float = 50.0) -> Dict:  # ← TAMBAH parameter rsi6
        
        # 🔥 PATCH RSI6=100: Jika RSI6 > 98, jangan paksa LONG
        if rsi6 > 98:
            return {"override": False}  # biarkan ExtremeRsi6OverboughtReversal handle
        
        # 🔥 PATCH CUSDT: Jika funding sangat negatif, ini BUKAN squeeze
        # Ini blow-off top yang akan dibalikkan HFT
        if funding_rate is not None and funding_rate < -0.002:
            return {"override": False}
        
        # 🔥 PATCH: RSI14 > 80 + RSI5m > 90 = overbought semua TF = reversal
        if rsi14 > 80 and rsi6_5m > 90:
            return {"override": False}  # biarkan BlowOffTop yang handle
        
        # Logic asli tetap
        if (rsi6_5m > 80 and
            volume_ratio < 0.5 and
            ofi_bias == "LONG" and
            ofi_strength > 0.5 and
            up_energy > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme overbought with strong OFI LONG: RSI5m {rsi6_5m:.1f}, volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → squeeze continuation",
                "priority": -200
            }
        return {"override": False}


class ExtremeOversoldContinuation:
    """
    🔥 Memaksa SHORT ketika oversold ekstrem (RSI5m < 20), volume sangat rendah (<0.5x).
    Priority -200
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, long_liq: float) -> Dict:
        if (rsi6_5m < 20
                and volume_ratio < 0.5
                and ofi_bias == "SHORT"
                and ofi_strength > 0.5
                and down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme oversold with strong OFI SHORT: RSI5m {rsi6_5m:.1f}, "
                    f"volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → dump continuation"
                ),
                "priority": -200
            }
        return {"override": False}


class ExtremeOversoldShortContinuation:
    """
    🔥 Memaksa SHORT ketika oversold ekstrem (RSI6 < 20), volume sangat rendah (<0.6x).
    Priority -203
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, long_liq: float) -> Dict:
        if (rsi6 < 20
                and volume_ratio < 0.6
                and ofi_bias == "SHORT"
                and ofi_strength > 0.6
                and down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme oversold with strong OFI SHORT: RSI6 {rsi6:.1f}, "
                    f"volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → dump continuation"
                ),
                "priority": -203
            }
        return {"override": False}


class ExtremeOverboughtLongContinuation:
    """
    🔥 Memaksa LONG ketika overbought ekstrem (RSI6 > 80), volume sangat rendah (<0.6x).
    Priority -202
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               up_energy: float, short_liq: float) -> Dict:
        if (rsi6 > 80
                and volume_ratio < 0.6
                and ofi_bias == "LONG"
                and ofi_strength > 0.6
                and up_energy > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme overbought with strong OFI LONG: RSI6 {rsi6:.1f}, "
                    f"volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → squeeze continuation"
                ),
                "priority": -202
            }
        return {"override": False}


class OversoldFalseBounceTrap:
    """
    🔥 Mendeteksi false bounce pada oversold: OFI LONG kuat tetapi harga masih turun.
    Priority -201
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               change_5m: float, long_liq: float) -> Dict:
        if (rsi6 < 25
                and volume_ratio < 0.8
                and ofi_bias == "LONG"
                and ofi_strength > 0.8
                and change_5m < -2.0):
            if long_liq < 2.0:
                return {"override": False}
            if change_5m < -5.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Oversold false bounce: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, "
                    f"strong OFI LONG {ofi_strength:.2f} but price still down {change_5m:.1f}% "
                    f"→ dump continues"
                ),
                "priority": -201
            }
        return {"override": False}


class OverboughtFalseBounceTrap:
    """
    🔥 Mendeteksi false bounce pada overbought: OFI SHORT kuat tetapi harga masih naik.
    Priority -201
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               change_5m: float, short_liq: float) -> Dict:
        if (rsi6 > 75
                and volume_ratio < 0.8
                and ofi_bias == "SHORT"
                and ofi_strength > 0.8
                and change_5m > 2.0):
            if short_liq < 2.0:
                return {"override": False}
            if change_5m > 5.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Overbought false bounce: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, "
                    f"strong OFI SHORT {ofi_strength:.2f} but price still up {change_5m:.1f}% "
                    f"→ pump continues"
                ),
                "priority": -201
            }
        return {"override": False}


class ExtremeOversoldBounceOverride:
    """
    🔥 Memaksa LONG pada oversold ekstrem dengan OFI LONG kuat.
    Priority -150
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, long_liq: float) -> Dict:
        if (rsi6 < 25
                and volume_ratio < 0.8
                and change_5m < -5.0
                and ofi_bias == "LONG"
                and ofi_strength > 0.7):
            if long_liq < 1.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme oversold with strong OFI LONG: price down {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f}, volume {volume_ratio:.2f}x → bounce imminent"
                ),
                "priority": -150
            }
        return {"override": False}


class ExtremeOverboughtDumpOverride:
    """
    🔥 Memaksa SHORT pada overbought ekstrem dengan OFI SHORT kuat.
    Priority -150
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, short_liq: float,
               up_energy: float) -> Dict:
        if short_liq > 2.0 and up_energy > 0.1:
            return {"override": False}

        if (rsi6 > 75
                and volume_ratio < 0.8
                and change_5m > 5.0
                and ofi_bias == "SHORT"
                and ofi_strength > 0.7):
            if short_liq < 1.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme overbought with strong OFI SHORT: price up {change_5m:.1f}%, "
                    f"RSI {rsi6:.1f}, volume {volume_ratio:.2f}x → dump imminent"
                ),
                "priority": -150
            }
        return {"override": False}


class ExhaustionDumpOverride:
    """
    🔥 Mendeteksi blow-off top: harga naik tinggi, volume rendah, energy collapse.
    Priority -130
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, change_5m: float,
               up_energy: float, short_liq: float) -> Dict:
        if (rsi6_5m > 85
                and volume_ratio < 0.5
                and change_5m > 5.0
                and up_energy < 0.1):
            if short_liq < 1.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Exhaustion dump: price up {change_5m:.1f}%, RSI5m {rsi6_5m:.1f}, "
                    f"volume {volume_ratio:.2f}x, energy collapsed → dump imminent"
                ),
                "priority": -130
            }
        return {"override": False}


class UltraCloseSqueezeOverride:
    """
    🔥 Memaksa LONG ketika short liq sangat dekat (<0.5%), OFI SHORT kuat, down_energy=0.
    Priority -155
    """
    @staticmethod
    def detect(short_liq: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, volume_ratio: float, change_5m: float) -> Dict:
        if (short_liq < 0.5
                and ofi_bias == "SHORT"
                and ofi_strength > 0.7
                and down_energy < 0.01
                and volume_ratio < 0.7
                and change_5m > -1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Ultra close squeeze: short liq {short_liq:.2f}%, "
                    f"strong OFI SHORT {ofi_strength:.2f}, no sellers → forced LONG"
                ),
                "priority": -155
            }
        return {"override": False}


class AbsorptionReversalOverride:
    """
    🔥 Mendeteksi bear trap: OFI SHORT kuat tapi harga tidak turun (down_energy=0).
    Priority -135
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               change_5m: float, short_liq: float, volume_ratio: float) -> Dict:
        if (ofi_bias == "SHORT"
                and ofi_strength > 0.7
                and down_energy < 0.01
                and volume_ratio < 0.7
                and short_liq < 3.0
                and change_5m > -1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Absorption reversal: strong OFI SHORT {ofi_strength:.2f} but no sellers "
                    f"(down_energy=0) and short liq {short_liq:.2f}% close → bear trap, squeeze up"
                ),
                "priority": -135
            }
        return {"override": False}


class OversoldLiquidityBounce:
    """
    🔥 Memaksa LONG pada oversold (RSI6_5m < 30), volume rendah (<0.6), long liq dekat (<5%).
    Priority -138
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, long_liq: float, down_energy: float,
               algo_bias: str = None, hft_bias: str = None, change_5m: float = None) -> Dict:
        if (algo_bias is not None
                and hft_bias is not None
                and change_5m is not None):
            if algo_bias == "SHORT" and hft_bias == "SHORT" and change_5m < 0:
                return {"override": False}

        if (rsi6_5m < 30
                and volume_ratio < 0.6
                and long_liq < 5.0
                and down_energy < 0.01):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Oversold liquidity bounce (5m): RSI5m {rsi6_5m:.1f}, "
                    f"volume {volume_ratio:.2f}x, long liq {long_liq:.2f}%, no sellers → bounce likely"
                ),
                "priority": -138
            }
        return {"override": False}


class LiquidityAbsorptionReversal:
    """
    🔥 Memaksa LONG ketika long liq tidak terlalu dekat (>2%), RSI netral (>=30), OFI SHORT kuat.
    Priority -136
    """
    @staticmethod
    def detect(long_liq: float, rsi6: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, volume_ratio: float, change_5m: float) -> Dict:
        if (long_liq > 2.0
                and rsi6 >= 30
                and ofi_bias == "SHORT"
                and ofi_strength > 0.6
                and down_energy < 0.01
                and volume_ratio < 0.8
                and change_5m < 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity absorption reversal: long liq {long_liq:.2f}%, RSI {rsi6:.1f} netral, "
                    f"strong OFI SHORT {ofi_strength:.2f}, no sellers → bear trap, bounce up"
                ),
                "priority": -136
            }
        return {"override": False}


class OversoldLiquidityContinuation:
    """
    🔥 Memaksa SHORT pada oversold dengan long liq sangat dekat (<1.5%), OFI SHORT kuat.
    Priority -139
    """
    @staticmethod
    def detect(volume_ratio: float, long_liq: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, change_5m: float, rsi6: float,
               up_energy: float = 0.0, agg: float = 0.5,
               latest_volume: float = 0.0, volume_ma10: float = 1.0) -> Dict:
        if up_energy > 1.0:
            return {"override": False}

        if agg > 0.55 and volume_ma10 > 0 and (latest_volume / volume_ma10) > 1.5:
            return {"override": False}

        if (volume_ratio < 0.7
                and long_liq < 1.5
                and down_energy < 0.01
                and (ofi_bias == "SHORT" or ofi_bias == "NEUTRAL")
                and change_5m < -1.0
                and rsi6 < 35):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Oversold liquidity continuation: long liq {long_liq:.2f}%, "
                    f"RSI6 {rsi6:.1f}, down_energy=0 → dump continues"
                ),
                "priority": -139
            }
        return {"override": False}


class FallingKnifeOverride:
    """
    🔥 Mencegah LONG trap pada oversold dengan long liq dekat.
    Priority -139
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, long_liq: float,
               volume_ratio: float, up_energy: float, down_energy: float,
               algo_bias: str, hft_bias: str, change_5m: float) -> Dict:
        if (rsi6 < 25
                and rsi6_5m < 35
                and long_liq < 3.0
                and volume_ratio < 0.7
                and up_energy > 0
                and down_energy == 0
                and algo_bias == "SHORT"
                and hft_bias == "SHORT"
                and change_5m < 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Falling knife: oversold (RSI6 {rsi6:.1f}) with long liq {long_liq:.2f}%, "
                    f"low volume, but HFT+Algo SHORT, down_energy=0 → no support, continuing down"
                ),
                "priority": -139
            }
        return {"override": False}


class FallingKnifeOBVConfirm:
    """
    🔥 RLSUSDT EXACT PATTERN:
    
    Kombinasi mematikan:
    - OBV value sangat negatif (< -20 juta) = distribusi lama
    - ask_slope >> bid_slope (>3x) = sell wall tebal
    - RSI_1m DAN RSI_5m keduanya oversold
    - harga terus turun (change_5m < 0)
    - volume rendah (tidak ada rescue buyer)
    
    Ini adalah FALLING KNIFE — oversold bukan berarti bounce.
    Smart money masih distribusi. Jangan masuk LONG.
    
    Priority: -205 (lebih tinggi dari OversoldFalseBounceTrap -201)
    """
    @staticmethod
    def detect(obv_value: float, ask_slope: float, bid_slope: float,
               rsi6: float, rsi6_5m: float, change_5m: float,
               volume_ratio: float, down_energy: float) -> Dict:
        
        if bid_slope <= 0:
            return {"override": False}
        
        ask_bid_ratio = ask_slope / bid_slope if bid_slope > 0 else 1.0
        
        # Semua sinyal bearish confluence
        obv_bearish = obv_value < -20_000_000
        slope_bearish = ask_bid_ratio > 2.5
        rsi_double_oversold = rsi6 < 30 and rsi6_5m < 25  # keduanya oversold
        price_falling = change_5m < -1.0
        low_volume = volume_ratio < 0.85
        
        bearish_count = sum([obv_bearish, slope_bearish, rsi_double_oversold,
                             price_falling, low_volume])
        
        if bearish_count >= 4:  # minimal 4 dari 5 kondisi terpenuhi
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Falling knife OBV confirm: OBV={obv_value:,.0f} (distribusi lama), ask/bid ratio={ask_bid_ratio:.1f}x, RSI_1m={rsi6:.1f} RSI_5m={rsi6_5m:.1f} keduanya oversold, price down {change_5m:.1f}% → NOT a bounce, dump continues",
                "priority": -205
            }
        
        return {"override": False}


class DataSnapshotConsistencyCheck:
    """
    🔥 Fix bug display ≠ JSON.
    
    Masalah: Data diambil 2x — sekali untuk display, sekali untuk keputusan.
    Race condition menghasilkan nilai berbeda.
    
    Solusi: Cek konsistensi agg vs ofi_bias.
    Jika agg > 0.7 tapi ofi_bias = SHORT, atau sebaliknya,
    gunakan agg sebagai tiebreaker (lebih real-time).
    """
    @staticmethod
    def resolve(agg: float, ofi_bias: str, ofi_strength: float,
                up_energy: float, down_energy: float) -> Dict:
        
        # Deteksi inkonsistensi: agg tinggi tapi OFI SHORT
        if agg > 0.7 and ofi_bias == "SHORT":
            # agg lebih real-time → percaya agg
            # Upgrade OFI ke NEUTRAL atau LONG tergantung energy
            if up_energy > down_energy:
                return {"bias": "LONG", "strength": (agg - 0.5) * 2,
                        "resolved": True, "reason": "agg>0.7 overrides OFI SHORT"}
            else:
                return {"bias": "NEUTRAL", "strength": 0,
                        "resolved": True, "reason": "agg>0.7 neutralizes OFI SHORT"}
        
        # agg rendah tapi OFI LONG
        if agg < 0.3 and ofi_bias == "LONG":
            if down_energy > up_energy:
                return {"bias": "SHORT", "strength": (0.5 - agg) * 2,
                        "resolved": True, "reason": "agg<0.3 overrides OFI LONG"}
            else:
                return {"bias": "NEUTRAL", "strength": 0,
                        "resolved": True, "reason": "agg<0.3 neutralizes OFI LONG"}
        
        # Konsisten → kembalikan apa adanya
        return {"bias": ofi_bias, "strength": ofi_strength, "resolved": False}


class ExtremeOversoldCloseLiquidityBounce:
    """
    🔥 Memaksa LONG ketika long liq sangat dekat (<0.5%) dan oversold (RSI < 25).
    Priority -141
    """
    @staticmethod
    def detect(rsi6: float, long_liq: float, up_energy: float, change_5m: float) -> Dict:
        if long_liq < 0.5 and rsi6 < 25 and up_energy > 0 and change_5m < 0:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme oversold with very close long liq ({long_liq:.2f}%), "
                    f"RSI {rsi6:.1f}, up_energy={up_energy:.2f} → bounce imminent"
                ),
                "priority": -141
            }
        return {"override": False}


class ExhaustionDropReversal:
    """
    🔥 Mendeteksi exhaustion drop: harga turun tajam dengan volume rendah.
    Priority -141
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, down_energy: float,
               short_liq: float, rsi6_5m: float) -> Dict:
        if (change_5m < -3.5
                and volume_ratio < 0.6
                and down_energy < 0.01
                and short_liq > 2.5
                and rsi6_5m < 85):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Exhaustion drop reversal: dropped {change_5m:.1f}% with low volume "
                    f"({volume_ratio:.2f}x), no sellers, short liq {short_liq:.2f}% → bounce likely"
                ),
                "priority": -141
            }
        return {"override": False}


class EnergyAggConsensus:
    """
    🔥 Ketika energy dan agg bertentangan dengan sinyal final, batalkan sinyal tersebut.
    Priority -143
    """
    @staticmethod
    def detect(up_energy: float, down_energy: float, agg: float,
               current_bias: str, rsi6: float, change_5m: float) -> Dict:
        if (current_bias == "SHORT"
                and up_energy > 1.0
                and down_energy < 0.1
                and agg > 0.55
                and rsi6 < 40):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Energy-Agg consensus overrides SHORT: up_energy={up_energy:.2f}, "
                    f"down_energy={down_energy:.2f}, agg={agg:.2f} all pointing LONG "
                    f"with RSI {rsi6:.1f} oversold"
                ),
                "priority": -143
            }

        if (current_bias == "LONG"
                and down_energy > 1.0
                and up_energy < 0.1
                and agg < 0.45
                and rsi6 > 60):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Energy-Agg consensus overrides LONG: down_energy={down_energy:.2f}, "
                    f"up_energy={up_energy:.2f}, agg={agg:.2f} all pointing SHORT "
                    f"with RSI {rsi6:.1f} overbought"
                ),
                "priority": -143
            }

        return {"override": False}


class ExtremeOverboughtDistribution:
    """
    🔥 DETECTS EXTREME OVERBOUGHT DISTRIBUTION
    Priority -270
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, volume_ratio: float,
               ofi_bias: str, ofi_strength: float, up_energy: float,
               short_liq: float, change_5m: float) -> Dict:
        if short_liq < 1.5:
            return {"override": False}

        if (rsi6_5m > 90
                and rsi6 > 70
                and volume_ratio < 0.9
                and ofi_bias == "LONG"
                and ofi_strength > 0.7
                and up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme overbought distribution: RSI5m {rsi6_5m:.1f} > 90, "
                    f"volume {volume_ratio:.2f}x, OFI LONG {ofi_strength:.2f}, "
                    f"up_energy={up_energy:.2f} → smart money distributing, dump incoming"
                ),
                "priority": -270
            }
        return {"override": False}


class TrappedShortSqueeze:
    """
    🔥 Mendeteksi short squeeze ketika OFI SHORT kuat tapi tidak ada sell wall.
    Priority -160
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               up_energy: float, volume_ratio: float, short_liq: float,
               long_liq: float, change_5m: float, rsi6: float = None) -> Dict:
        if rsi6 is not None and rsi6 > 75:
            return {"override": False}

        if (ofi_bias == "SHORT"
                and ofi_strength > 0.6
                and volume_ratio < 0.7
                and down_energy < 0.01
                and up_energy > 0.1
                and short_liq < long_liq
                and change_5m > 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Trapped short squeeze: OFI SHORT {ofi_strength:.2f} with low volume "
                    f"({volume_ratio:.2f}x), no sellers (down_energy=0), "
                    f"short liq {short_liq:.2f}% < long liq {long_liq:.2f}% "
                    f"→ short sellers trapped, squeeze up"
                ),
                "priority": -160
            }
        return {"override": False}


class TrappedLongSqueeze:
    """
    🔥 Mirror: OFI LONG kuat tapi tidak ada buy wall, long liq lebih dekat.
    Priority -160
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, up_energy: float,
               down_energy: float, volume_ratio: float, short_liq: float,
               long_liq: float, change_5m: float) -> Dict:
        if (ofi_bias == "LONG"
                and ofi_strength > 0.6
                and volume_ratio < 0.7
                and up_energy < 0.01
                and down_energy > 0.1
                and long_liq < short_liq
                and change_5m < -1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Trapped long squeeze: OFI LONG {ofi_strength:.2f} with low volume, "
                    f"no buyers, long liq closer → long sellers trapped, dump down"
                ),
                "priority": -160
            }
        return {"override": False}


class SqueezeContinuationDetector:
    @staticmethod
    def detect(rsi6_5m: float, change_5m: float, volume_ratio: float,
               short_dist: float, up_energy: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, bid_slope: float,
               ask_slope: float) -> Dict:
        """
        Mendeteksi squeeze continuation yang membatalkan sinyal SHORT palsu.
        Priority -265
        """
        if (rsi6_5m > 70
                and change_5m > 2.0
                and volume_ratio < 0.8
                and ofi_bias == "SHORT"
                and ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Squeeze continuation: RSI 5m {rsi6_5m:.1f} overbought, "
                    f"price up {change_5m:.1f}% low vol, but OFI SHORT {ofi_strength:.2f} "
                    f"→ selling being absorbed, squeeze ongoing"
                ),
                "priority": -265
            }

        if (ask_slope > bid_slope * 3
                and change_5m > 3.0
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Squeeze continuation: large ask wall ({ask_slope:.0f}) but price rising "
                    f"{change_5m:.1f}% → wall absorbed, upside continuation"
                ),
                "priority": -265
            }

        if (short_dist < 5.0
                and up_energy > 0.1
                and change_5m > 2.0
                and volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Squeeze continuation: short liq {short_dist}% still close, "
                    f"price up {change_5m:.1f}% low vol → short squeeze ongoing"
                ),
                "priority": -265
            }

        return {"override": False}


# ================= EXISTING DETECTOR MODULES =================

class RetailSentimentTracker:
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, retail_order_flow: float) -> Dict:
        if change_5m < -2.0 and volume_ratio > 2.0 and retail_order_flow > 1.5:
            return {
                "signal": "LONG",
                "confidence": "SUPREME",
                "reason": "恐慌割肉盘出现，量化机构在低位接盘",
                "action": "BUY_PANIC",
                "priority": -100
            }
        if change_5m > 2.0 and volume_ratio > 2.0 and retail_order_flow < 0.5:
            return {
                "signal": "SHORT",
                "confidence": "SUPREME",
                "reason": "贪婪追高盘出现，量化机构在高位派发",
                "action": "SELL_FOMO",
                "priority": -100
            }
        return {"signal": "NEUTRAL", "priority": 0}


class RSIVolumeParadoxDetector:
    @staticmethod
    def detect(rsi: float, volume_ratio: float, price_change: float,
               obv_trend: str, stoch_k: float, stoch_d: float) -> Dict:
        if rsi < 30 and volume_ratio > 1.5:
            return {
                "is_trap": True,
                "correct_bias": "SHORT",
                "reason": f"Oversold trap: RSI {rsi:.1f} + Volume tinggi {volume_ratio:.2f}x → masih panic selling",
                "priority": -120
            }
        if rsi > 70 and volume_ratio > 1.5:
            return {
                "is_trap": True,
                "correct_bias": "LONG",
                "reason": f"Overbought trap: RSI {rsi:.1f} + Volume tinggi {volume_ratio:.2f}x → momentum masih kuat",
                "priority": -120
            }
        if obv_trend == "NEGATIVE_EXTREME" and 40 < rsi < 60 and stoch_k > stoch_d:
            return {
                "is_trap": True,
                "correct_bias": "LONG",
                "reason": f"OBV bait: OBV negatif ekstrim tapi Stoch bullish → akan pump",
                "priority": -120
            }
        if 30 < rsi < 45 and volume_ratio < 0.8 and stoch_k < stoch_d:
            return {
                "is_trap": True,
                "correct_bias": "SHORT",
                "reason": f"Bounce trap: RSI {rsi:.1f} + Volume rendah {volume_ratio:.2f}x + Stoch bearish → dead cat bounce",
                "priority": -120
            }
        if 65 < rsi < 75 and volume_ratio < 0.8 and price_change > 0:
            return {
                "is_trap": True,
                "correct_bias": "LONG",
                "reason": f"Volume exhaustion: RSI {rsi:.1f} + Volume turun {volume_ratio:.2f}x tapi harga naik → seller habis",
                "priority": -120
            }
        return {"is_trap": False, "correct_bias": "NEUTRAL", "priority": 0}


class EnergySupremacyOverride:
    @staticmethod
    def detect(up_energy: float, down_energy: float) -> Dict:
        if up_energy <= 0 or down_energy <= 0:
            return {"override": False}
        ratio = down_energy / up_energy
        if ratio > ENERGY_RATIO_THRESHOLD:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Energy supremacy: down_energy {down_energy:.2f}x up_energy → force LONG",
                "priority": -250
            }
        elif up_energy / down_energy > ENERGY_RATIO_THRESHOLD:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Energy supremacy: up_energy {up_energy:.2f}x down_energy → force SHORT",
                "priority": -250
            }
        return {"override": False, "priority": 0}


class VacuumDirectionRule:
    @staticmethod
    def detect(bid_volume: float, ask_volume: float, up_energy: float,
               down_energy: float) -> Dict:
        if bid_volume < VACUUM_VOLUME_THRESHOLD and ask_volume < VACUUM_VOLUME_THRESHOLD:
            if up_energy < down_energy:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": "Vacuum: kedua sisi kosong, energy up lebih murah → LONG",
                    "priority": -245
                }
            else:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": "Vacuum: kedua sisi kosong, energy down lebih murah → SHORT",
                    "priority": -245
                }
        if (bid_volume < VACUUM_VOLUME_THRESHOLD
                and ask_volume > 0
                and up_energy < down_energy * 3):
            return {
                "override": True,
                "bias": "LONG",
                "reason": "Vacuum: bid kosong, energi up murah → LONG",
                "priority": -245
            }
        if (ask_volume < VACUUM_VOLUME_THRESHOLD
                and bid_volume > 0
                and down_energy < up_energy * 3):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": "Vacuum: ask kosong, energi down murah → SHORT",
                "priority": -245
            }
        return {"override": False, "priority": 0}


class DeadMarketProximityRule:
    @staticmethod
    def detect(agg: float, flow: float, short_dist: float, long_dist: float,
               up_energy: float, down_energy: float) -> Dict:
        if agg < DEAD_AGG_THRESHOLD and flow < DEAD_FLOW_THRESHOLD:
            if short_dist < LIQ_PROXIMITY_THRESHOLD:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Dead market + short liq sangat dekat (+{short_dist}%) → LONG",
                    "priority": -235
                }
            if long_dist < LIQ_PROXIMITY_THRESHOLD:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Dead market + long liq sangat dekat (-{long_dist}%) → SHORT",
                    "priority": -235
                }
            if short_dist < long_dist and up_energy < down_energy * 3:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Dead market: short liq lebih dekat, energi up murah → LONG",
                    "priority": -235
                }
            if long_dist < short_dist and down_energy < up_energy * 3:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Dead market: long liq lebih dekat, energi down murah → SHORT",
                    "priority": -235
                }
        return {"override": False, "priority": 0}


class OverboughtDistributionTrapFilter:
    @staticmethod
    def detect(rsi: float, oi_delta: float, up_energy: float, down_energy: float) -> Dict:
        if rsi > OVERBOUGHT_RSI and oi_delta > OI_DELTA_THRESHOLD and up_energy < down_energy * 5:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Overbought trap: RSI {rsi:.1f} + OI naik {oi_delta:.2f}% "
                    f"tapi energi up murah → pump lanjut"
                ),
                "priority": -190
            }
        return {"override": False, "priority": 0}


class LiquidityFlushConfirmation:
    @staticmethod
    def detect(short_dist: float, long_dist: float, agg: float) -> Dict:
        if short_dist < FLUSH_ZONE_THRESHOLD and long_dist < FLUSH_ZONE_THRESHOLD:
            return {
                "wait": True,
                "reason": (
                    f"Double sweep zone: short liq +{short_dist}%, "
                    f"long liq -{long_dist}% → tunggu sweep"
                ),
                "priority": -255
            }
        if (agg < FLUSH_AGG_THRESHOLD
                and (short_dist < FLUSH_ZONE_THRESHOLD or long_dist < FLUSH_ZONE_THRESHOLD)):
            return {
                "wait": True,
                "reason": f"Low aggression + close liquidity → kemungkinan flush, tunggu",
                "priority": -255
            }
        return {"wait": False, "priority": 0}


class EnergyGapTrapDetector:
    @staticmethod
    def detect(rsi14: float, up_energy: float, down_energy: float) -> Dict:
        if rsi14 > 75 and down_energy < up_energy * 0.1:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Energy Gap Trap: RSI {rsi14:.1f} overbought + "
                    f"down_energy {down_energy:.2f} << up_energy {up_energy:.2f} → HFT akan dump"
                ),
                "priority": -215
            }
        if rsi14 < 25 and up_energy < down_energy * 0.1:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Energy Gap Trap: RSI {rsi14:.1f} oversold + "
                    f"up_energy {up_energy:.2f} << down_energy {down_energy:.2f} → HFT akan pump"
                ),
                "priority": -215
            }
        return {"override": False, "priority": 0}


class ExtremeOversoldReversalFilter:
    @staticmethod
    def detect(rsi6: float, rsi14: float, stoch_k: float, obv_value: float, obv_trend: str,
               long_dist: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float) -> Dict:
        obv_extreme_negative = obv_value < -30_000_000

        if (rsi6 < EXTREME_OVERSOLD_RSI
                and rsi14 < EXTREME_OVERSOLD_RSI
                and stoch_k < EXTREME_OVERSOLD_STOCH
                and obv_trend == "NEGATIVE_EXTREME"
                and obv_extreme_negative
                and change_5m < PANIC_DROP_THRESHOLD
                and down_energy < 0.01):
            if ofi_bias == "LONG" and ofi_strength > 0.3:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"Extreme oversold reversal: OBV {obv_value:,.0f} "
                        f"(strong selling exhaustion) + panic drop → bounce imminent"
                    ),
                    "priority": -225
                }
        return {"override": False, "priority": 0}


class PanicDropExhaustionDetector:
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, rsi6: float,
               down_energy: float, obv_trend: str) -> Dict:
        if (change_5m < -10.0
                and volume_ratio < 1.0
                and rsi6 < 15
                and down_energy < 0.01
                and obv_trend == "NEGATIVE_EXTREME"):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Panic exhaustion: drop {change_5m:.1f}% + volume drying + "
                    f"RSI {rsi6:.1f} + no sellers → reversal likely"
                ),
                "priority": -224
            }
        return {"override": False, "priority": 0}


class ShortSqueezeTrapDetector:
    @staticmethod
    def detect(long_dist: float, rsi6: float, ofi_bias: str,
               ofi_strength: float, down_energy: float,
               agg: float, flow: float) -> Dict:
        if (long_dist < 1.0
                and rsi6 < 20
                and ofi_bias == "LONG"
                and ofi_strength > 0.3
                and down_energy < 0.05
                and agg < 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Short squeeze trap: long liq {long_dist}% + oversold RSI {rsi6:.1f} "
                    f"+ OFI LONG + no sellers → HFT will pump to trigger liq then bounce"
                ),
                "priority": -223
            }
        return {"override": False, "priority": 0}


class OFIExtremeOversoldConfirm:
    @staticmethod
    def detect(rsi6: float, ofi_bias: str, ofi_strength: float,
               long_dist: float, down_energy: float, up_energy: float,
               volume_ratio: float) -> Dict:
        min_ofi_strength = 0.60 if volume_ratio < 0.8 else 0.35

        if (rsi6 < 20
                and ofi_bias == "LONG"
                and ofi_strength > min_ofi_strength
                and down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OFI confirms oversold bounce: RSI {rsi6:.1f} + "
                    f"OFI LONG ({ofi_strength:.2f} > {min_ofi_strength:.2f}) + "
                    f"no sellers → smart money accumulating"
                ),
                "priority": -222
            }
        if (rsi6 > 80
                and ofi_bias == "SHORT"
                and ofi_strength > min_ofi_strength
                and up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OFI confirms overbought dump: RSI {rsi6:.1f} + "
                    f"OFI SHORT ({ofi_strength:.2f} > {min_ofi_strength:.2f}) + "
                    f"no buyers → smart money distributing"
                ),
                "priority": -222
            }
        return {"override": False, "priority": 0}


class OversoldContinuation:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, price: float, ma25: float, ma99: float,
               volume_ratio: float, down_energy: float, ofi_bias: str, ofi_strength: float,
               long_dist: float) -> Dict:
        if long_dist < 1.5 and volume_ratio < 0.7:
            return {"override": False}
        if (rsi6 < 25
                and obv_trend == "NEGATIVE_EXTREME"
                and price < ma25
                and price < ma99
                and volume_ratio < 0.8):
            if ofi_bias == "LONG" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Oversold continuation: RSI {rsi6:.1f} deep oversold, "
                    f"price below MAs, low volume → falling knife likely"
                ),
                "priority": -223
            }
        return {"override": False, "priority": 0}


class OversoldBounce:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, down_energy: float, long_dist: float,
               price: float, recent_low: float, up_energy: float, ma25: float, ma99: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float) -> Dict:
        if rsi6 < 25 and obv_trend == "NEGATIVE_EXTREME" and down_energy < 0.01:
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            if price < ma25 and price < ma99:
                return {"override": False}
            if (long_dist < 3.0
                    or (price - recent_low) / recent_low < 0.02
                    or up_energy > 0.1):
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"Oversold bounce: RSI {rsi6:.1f} deep oversold, "
                        f"OBV extreme negative, no sellers → potential bounce"
                    ),
                    "priority": -223
                }
        return {"override": False, "priority": 0}


class StrongBearishOverride:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, price: float, ma25: float, ma99: float,
               volume_ratio: float, down_energy: float) -> Dict:
        if (price < ma25
                and price < ma99
                and obv_trend == "NEGATIVE_EXTREME"
                and volume_ratio < 0.8
                and rsi6 < 40
                and down_energy < ENERGY_ZERO_THRESHOLD):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Strong bearish override: price below MAs, OBV extreme, "
                    f"low volume, RSI {rsi6:.1f} < 40 → force SHORT"
                ),
                "priority": -222
            }
        return {"override": False, "priority": 0}


class OFIConflictFilter:
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, short_dist: float, long_dist: float,
               up_energy: float, down_energy: float, rsi6: float, change_5m: float) -> Dict:
        if ofi_bias != "NEUTRAL" and ofi_strength > 0.7:
            if not (abs(change_5m) > 8.0 or rsi6 < 10 or rsi6 > 90):
                return {"override": False, "priority": 0}
        if ofi_bias == "NEUTRAL" or ofi_strength < 0.7:
            return {"override": False}

        if ofi_bias == "LONG" and down_energy < up_energy * 0.2 and long_dist < 1.5:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OFI conflict: OFI says LONG (strength {ofi_strength:.2f}) "
                    f"but down_energy is cheap and long liq close ({long_dist}%) → override to SHORT"
                ),
                "priority": -222
            }
        if ofi_bias == "SHORT" and up_energy < down_energy * 0.2 and short_dist < 1.5:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OFI conflict: OFI says SHORT (strength {ofi_strength:.2f}) "
                    f"but up_energy is cheap and short liq close ({short_dist}%) → override to LONG"
                ),
                "priority": -222
            }
        if ofi_bias == "LONG" and rsi6 < 30 and long_dist < 2.0 and down_energy < 0.1:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OFI conflict: OFI says LONG in oversold (RSI {rsi6:.1f}) "
                    f"but long liq close and no sellers → likely trap, override to SHORT"
                ),
                "priority": -222
            }
        return {"override": False, "priority": 0}


class LiquidityPriorityEnergyCheck:
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               up_energy: float, down_energy: float,
               price_change_5m: float) -> Dict:
        CLOSE_LIQ = 1.5
        if (long_dist < CLOSE_LIQ
                and long_dist < short_dist
                and down_energy < 0.01
                and price_change_5m < -5.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity priority blocked: long liq dekat ({long_dist}%) tapi "
                    f"down_energy {down_energy:.2f} + harga sudah drop {price_change_5m:.1f}% "
                    f"→ tidak ada fuel untuk dump, justru bounce"
                ),
                "priority": -221
            }
        if (short_dist < CLOSE_LIQ
                and short_dist < long_dist
                and up_energy < 0.01
                and price_change_5m > 5.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity priority blocked: short liq dekat ({short_dist}%) tapi "
                    f"up_energy {up_energy:.2f} + harga sudah pump {price_change_5m:.1f}% "
                    f"→ tidak ada fuel untuk pump, justru dump"
                ),
                "priority": -221
            }
        return {"override": False, "priority": 0}


class LiquidityPriorityOverride:
    @staticmethod
    def detect(short_dist: float, long_dist: float, volume_ratio: float, rsi6_5m: float,
               rsi6: float, ofi_bias: str, ofi_strength: float) -> Dict:
        if volume_ratio < 0.5:
            return {
                "override": False,
                "priority": 0,
                "reason": "Volume too low (<0.5x) → Liquidity target unreliable (HFT Trap Risk)"
            }
        if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
            return {"override": False}

        CLOSE_LIQ_THRESHOLD = 1.5
        if short_dist < CLOSE_LIQ_THRESHOLD and short_dist < long_dist:
            if rsi6 > 65 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity priority: short liq sangat dekat (+{short_dist}%) "
                    f"→ ambil likuidasi short (LONG)"
                ),
                "priority": -220
            }
        if long_dist < CLOSE_LIQ_THRESHOLD and long_dist < short_dist:
            if rsi6 < 35 and ofi_bias == "LONG" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            if rsi6 < 25 and volume_ratio < 0.6:
                return {"override": False}
            if long_dist < 2.5 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            if rsi6 < 35 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity priority: long liq sangat dekat (-{long_dist}%) "
                    f"→ ambil likuidasi long (SHORT)"
                ),
                "priority": -220
            }
        return {"override": False, "priority": 0}


class LiquidityEnergyCheck:
    @staticmethod
    def detect(short_dist: float, long_dist: float, up_energy: float, down_energy: float,
               volume_ratio: float, ofi_bias: str, ofi_strength: float, rsi6_5m: float,
               obv_magnitude: str) -> Dict:
        CLOSE_LIQ = 1.5

        if volume_ratio < 0.6:
            return {"override": False, "priority": 0}
        if ofi_bias == "NEUTRAL" or ofi_strength < 0.4:
            return {"override": False, "priority": 0}
        if short_dist < CLOSE_LIQ and short_dist < long_dist and down_energy < up_energy * 0.2:
            if rsi6_5m > 70:
                return {"override": False, "priority": 0}
        if obv_magnitude == "LOW" and volume_ratio < 0.8:
            return {"override": False, "priority": 0}

        if short_dist < CLOSE_LIQ and short_dist < long_dist and down_energy < up_energy * 0.2:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity energy trap: short liq dekat (+{short_dist}%) tapi "
                    f"down_energy {down_energy:.2f} jauh lebih murah dari up_energy {up_energy:.2f} "
                    f"→ HFT akan dump dulu"
                ),
                "priority": -220
            }
        if long_dist < CLOSE_LIQ and long_dist < short_dist and up_energy < down_energy * 0.2:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity energy trap: long liq dekat (-{long_dist}%) tapi "
                    f"up_energy {up_energy:.2f} jauh lebih murah dari down_energy {down_energy:.2f} "
                    f"→ HFT akan pump dulu"
                ),
                "priority": -220
            }
        return {"override": False, "priority": 0}


class OverboughtLiquidityTrap:
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, up_energy: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float, funding_rate: float) -> Dict:
        CLOSE_LIQ = 1.5

        # GUARD: Jika kondisi ekstrem overbought + volume kering, biarkan BlowOffTopShortLiqTrap yang handle
        if rsi6 > 85 and volume_ratio < 0.7:
            return {"override": False}  # Jangan paksa LONG, biarkan blow-off detector handle
        
        if volume_ratio < 0.6:
            return {"override": False, "priority": 0}
        if rsi6 > 90 and volume_ratio < 0.9:
            return {"override": False, "priority": 0}
        if rsi6 > 85 and funding_rate < -0.005 and volume_ratio < 1.0:
            return {"override": False}

        if (short_dist < CLOSE_LIQ
                and short_dist < long_dist
                and rsi6 > 70
                and down_energy < ENERGY_ZERO_THRESHOLD):
            if ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False, "priority": 0}

        if (short_dist < CLOSE_LIQ
                and short_dist < long_dist
                and rsi6 > 70
                and down_energy < ENERGY_ZERO_THRESHOLD):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Overbought liquidity trap: short liq sangat dekat (+{short_dist}%) "
                    f"tetapi RSI overbought ({rsi6:.1f}) dan tidak ada tekanan jual "
                    f"(down_energy {down_energy:.2f}) → HFT akan pump dulu untuk ambil short liq"
                ),
                "priority": -221
            }
        return {"override": False, "priority": 0}


class LiquidityBaitDetector:
    @staticmethod
    def detect(short_dist: float, long_dist: float, up_energy: float, down_energy: float,
               agg: float, flow: float, volume_ratio: float) -> Dict:
        CLOSE_LIQ = 2.0
        if long_dist < 1.0 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "LONG" if up_energy < down_energy else "SHORT",
                "reason": (
                    f"Liquidity bait: long liq dekat (-{long_dist}%) + "
                    f"volume rendah {volume_ratio:.2f}x → HFT akan reverse"
                ),
                "priority": -216
            }
        if short_dist < 1.0 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "SHORT" if down_energy < up_energy else "LONG",
                "reason": (
                    f"Liquidity bait: short liq dekat (+{short_dist}%) + "
                    f"volume rendah {volume_ratio:.2f}x → HFT akan reverse"
                ),
                "priority": -216
            }
        if short_dist < CLOSE_LIQ and long_dist < CLOSE_LIQ:
            return {"override": False, "priority": 0}
        if short_dist < CLOSE_LIQ and down_energy < up_energy * 0.3 and agg < 0.3:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Liquidity bait: short liq dekat (+{short_dist}%) tetapi "
                    f"down_energy {down_energy:.2f} lebih murah → HFT akan dump dulu"
                ),
                "priority": -216
            }
        if long_dist < CLOSE_LIQ and up_energy < down_energy * 0.3 and agg < 0.3:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Liquidity bait: long liq dekat (-{long_dist}%) tetapi "
                    f"up_energy {up_energy:.2f} lebih murah → HFT akan pump dulu"
                ),
                "priority": -216
            }
        return {"override": False, "priority": 0}


class ExtremeEnergyImbalance:
    @staticmethod
    def detect(up_energy: float, down_energy: float, volume_ratio: float, rsi14: float,
               price_change_5m: float, ofi_bias: str, ofi_strength: float,
               rsi6: float, rsi6_5m: float) -> Dict:
        if volume_ratio < 0.7:
            return {"override": False, "priority": 0}
        if ofi_bias == "NEUTRAL" or ofi_strength < 0.4:
            return {"override": False, "priority": 0}
        if down_energy < ENERGY_ZERO_THRESHOLD and price_change_5m < -1.0:
            return {"override": False, "priority": 0}
        if up_energy < down_energy and rsi6_5m > 30:
            return {"override": False, "priority": 0}

        if down_energy < ENERGY_ZERO_THRESHOLD and up_energy > MIN_ENERGY_TO_MOVE:
            if price_change_5m > 1.5 and ofi_bias == "LONG" and ofi_strength > 0.3:
                return {"override": False, "priority": 0}
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            if rsi6 < 35 and volume_ratio < 0.8 and price_change_5m < 0:
                return {"override": False, "priority": 0}
            if rsi6_5m < 40 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Extreme energy imbalance: down_energy {down_energy:.2f} << "
                    f"up_energy {up_energy:.2f} → tidak ada buyer support, bearish"
                ),
                "priority": -218
            }

        if up_energy < ENERGY_ZERO_THRESHOLD and down_energy > MIN_ENERGY_TO_MOVE:
            if ofi_bias == "LONG" and ofi_strength > 0.6 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Extreme energy imbalance: up_energy {up_energy:.2f} << "
                    f"down_energy {down_energy:.2f} → tidak ada seller pressure, bullish"
                ),
                "priority": -218
            }
        return {"override": False, "priority": 0}


class EnergyTrapFilter:
    @staticmethod
    def detect(up_energy: float, down_energy: float, change_5m: float, volume_ratio: float,
               rsi14: float, short_liq: float, rsi6_5m: float) -> Dict:
        if (down_energy < ENERGY_ZERO_THRESHOLD
                and up_energy < 1.0
                and change_5m > 2.0
                and volume_ratio < 1.0
                and rsi14 > 60):
            if short_liq < 1.5 and volume_ratio < 0.6 and rsi6_5m > 70:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Energy Trap: down_energy {down_energy:.2f} seolah habis, tapi harga naik "
                    f"{change_5m:.1f}% dengan volume turun {volume_ratio:.2f}x → HFT akan dump"
                ),
                "priority": -217
            }
        return {"override": False, "priority": 0}


class ThinOrderBookPump:
    @staticmethod
    def detect(up_energy: float, down_energy: float, change_5m: float,
               volume_ratio: float, ofi_bias: str, ofi_strength: float,
               short_liq: float) -> Dict:
        if (down_energy < ENERGY_ZERO_THRESHOLD
                and up_energy > 0
                and change_5m > 1.0
                and volume_ratio < 1.0
                and ofi_bias == "LONG"
                and ofi_strength > 0
                and short_liq < 5.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Thin order book pump: down_energy {down_energy:.2f} but price rising "
                    f"and OFI bullish → no sellers to stop the pump, short liq {short_liq}% close"
                ),
                "priority": -217
            }
        return {"override": False, "priority": 0}


class PumpExhaustionTrap:
    """
    🔥 Detects thin pumps that are likely reversal traps.
    Priority -216
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, down_energy: float,
               long_liq: float, short_liq: float, rsi6: float) -> Dict:
        if (change_5m > 1.0
                and volume_ratio < 0.7
                and down_energy < 0.01
                and long_liq < short_liq
                and rsi6 < 75):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Pump exhaustion trap: naik {change_5m:.1f}% dengan volume {volume_ratio:.2f}x, "
                    f"down_energy=0 tapi long liq {long_liq}% < short liq {short_liq}% "
                    f"→ reversal untuk ambil long liq"
                ),
                "priority": -216
            }
        return {"override": False}


class HFTTrapDetector:
    @staticmethod
    def detect_fake_energy(down_energy: float, up_energy: float, change_5m: float,
                           volume_ratio: float, rsi14: float, short_liq: float, long_liq: float,
                           rsi6_5m: float, rsi6: float) -> Dict:
        if (down_energy < ENERGY_ZERO_THRESHOLD
                and change_5m > 3.0
                and volume_ratio < 0.7
                and rsi14 > 60):
            if short_liq < 1.5 and volume_ratio < 0.6 and (rsi6_5m > 70 or rsi6 > 80):
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"Fake Energy: down_energy=0 tetapi harga naik {change_5m:.1f}% "
                    f"dengan volume turun {volume_ratio:.2f}x → HFT trap, akan dump"
                ),
                "priority": -230
            }
        if (up_energy < ENERGY_ZERO_THRESHOLD
                and change_5m < -3.0
                and volume_ratio < 0.7
                and rsi14 < 40):
            if long_liq < 1.5 and volume_ratio < 0.6 and (rsi6_5m < 30 or rsi6 < 20):
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"Fake Energy: up_energy=0 tetapi harga turun {change_5m:.1f}% "
                    f"dengan volume turun {volume_ratio:.2f}x → HFT trap, akan pump"
                ),
                "priority": -230
            }
        return {"override": False, "priority": 0}


class VolumeConfidenceFilter:
    @staticmethod
    def apply(volume_ratio: float, current_confidence: str,
              current_reason: str) -> Tuple[str, str]:
        if volume_ratio < 0.3:
            if current_confidence == "ABSOLUTE":
                new_conf = "MEDIUM"
            elif current_confidence == "MEDIUM":
                new_conf = "MEDIUM"
            else:
                new_conf = current_confidence
            reason_suffix = f" | Low volume warning ({volume_ratio:.1%} of normal)"
            return new_conf, current_reason + reason_suffix
        return current_confidence, current_reason


class MultiTimeframeConfirmation:
    @staticmethod
    def check(rsi6_1m: float, rsi6_5m: float, current_confidence: str,
              current_reason: str) -> Tuple[str, str]:
        if (rsi6_1m > 50 and rsi6_5m < 40) or (rsi6_1m < 50 and rsi6_5m > 60):
            if current_confidence == "ABSOLUTE":
                new_conf = "MEDIUM"
            else:
                new_conf = current_confidence
            reason_suffix = (
                f" | Multi-TF divergence: 1m RSI {rsi6_1m:.1f} vs 5m RSI {rsi6_5m:.1f}"
            )
            return new_conf, current_reason + reason_suffix
        return current_confidence, current_reason


class MomentumVolumeSpikeProtection:
    """
    🔥 PIPPINUSDT KILLER: Saat harga pump >8% dalam 5m dengan volume spike >4x,
    JANGAN PERNAH output SHORT sampai short_liq tersapu.
    
    Ini adalah squeeze yang sedang berjalan — semua sinyal reversal harus diabaikan.
    
    Rule:
    - change_5m > 8% AND volume spike > 4x AND short_liq < 2% → LOCK LONG
    - change_5m < -8% AND volume spike > 4x AND long_liq < 2% → LOCK SHORT
    
    Priority: -1102 (TERTINGGI ABSOLUT — di atas funding ban!)
    Karena momentum volume spike aktif = squeeze sedang berlangsung,
    tidak ada yang bisa stop ini.
    """
    @staticmethod
    def detect(change_5m: float, latest_volume: float, volume_ma10: float,
               short_dist: float, long_dist: float,
               obv_trend: str, up_energy: float, down_energy: float) -> Dict:
        
        if volume_ma10 <= 0:
            return {"override": False}
        
        vol_spike = latest_volume / volume_ma10
        
        # LONG LOCK: pump besar + volume spike raksasa + short liq dekat
        if (change_5m > 8.0 and
            vol_spike > 4.0 and
            short_dist < 2.5 and
            short_dist < long_dist and
            down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"MOMENTUM VOLUME SPIKE LOCK: price up {change_5m:.1f}%, volume {vol_spike:.1f}x MA10, short liq {short_dist:.2f}% < {long_dist:.2f}%, no sellers → active short squeeze, LOCK LONG until short liq swept",
                "priority": -1102
            }
        
        # SHORT LOCK: dump besar + volume spike + long liq dekat
        if (change_5m < -8.0 and
            vol_spike > 4.0 and
            long_dist < 2.5 and
            long_dist < short_dist and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"MOMENTUM VOLUME SPIKE LOCK: price down {change_5m:.1f}%, volume {vol_spike:.1f}x MA10, long liq {long_dist:.2f}% < {short_dist:.2f}%, no buyers → active long squeeze, LOCK SHORT until long liq swept",
                "priority": -1102
            }
        
        return {"override": False}


class OBVStochasticReversal:
    @staticmethod
    def apply(obv_trend: str, obv_value: float, stoch_k: float, stoch_d: float,
              current_bias: str, current_reason: str, volume_ratio: float,
              rsi6: float, rsi6_5m: float,
              change_5m: float = 0.0, short_dist: float = 99.0, long_dist: float = 99.0,
              latest_volume: float = 0.0, volume_ma10: float = 1.0) -> Tuple[str, str]:
        
        # 🔥 PATCH PIPPINUSDT: Jangan reverse saat squeeze aktif
        # Kondisi: momentum besar + short/long liq dekat + volume spike
        vol_spike = latest_volume / volume_ma10 if volume_ma10 > 0 else 1.0
        
        if abs(change_5m) > 5.0 and vol_spike > 3.0:
            if change_5m > 0 and short_dist < 3.0:
                return current_bias, f"{current_reason} | OBV reversal BLOCKED (active squeeze: {change_5m:.1f}% pump, vol {vol_spike:.1f}x, short liq {short_dist:.2f}%)"
            if change_5m < 0 and long_dist < 3.0:
                return current_bias, f"{current_reason} | OBV reversal BLOCKED (active squeeze: {change_5m:.1f}% dump, vol {vol_spike:.1f}x, long liq {long_dist:.2f}%)"
        
        # 🔥 PATCH 2: Jangan reverse saat change_5m sangat ekstrem (>8%)
        if abs(change_5m) > 8.0:
            return current_bias, f"{current_reason} | OBV reversal BLOCKED (extreme momentum {change_5m:.1f}% override)"
        
        stoch_j = 3 * stoch_k - 2 * stoch_d
        obv_magnitude_strong = abs(obv_value) > 10_000_000

        if volume_ratio < 0.8:
            return current_bias, f"{current_reason} | Volume low ({volume_ratio:.2f}x), OBV reversal skipped"

        if obv_trend in ["NEGATIVE", "NEGATIVE_EXTREME"] and obv_magnitude_strong:
            if stoch_j < stoch_k:
                if rsi6 > 30 or stoch_k > 30:
                    return current_bias, f"{current_reason} | OBV reversal to LONG skipped (1m not oversold)"
                if rsi6_5m > 30:
                    return current_bias, f"{current_reason} | OBV reversal to LONG skipped (5m not oversold)"
                new_bias = "LONG" if current_bias == "SHORT" else "SHORT"
                return new_bias, f"{current_reason} | OBV- (val={obv_value:,.0f}) & J<K → reversal to {new_bias}"

        if obv_trend in ["POSITIVE", "POSITIVE_EXTREME"] and obv_magnitude_strong:
            if stoch_k < stoch_j:
                if rsi6 < 70 or stoch_k < 70:
                    return current_bias, f"{current_reason} | OBV reversal to SHORT skipped (1m not overbought)"
                if rsi6_5m < 70:
                    return current_bias, f"{current_reason} | OBV reversal to SHORT skipped (5m not overbought)"
                new_bias = "LONG" if current_bias == "SHORT" else "SHORT"
                return new_bias, f"{current_reason} | OBV+ (val={obv_value:,.0f}) & K<J → reversal to {new_bias}"

        return current_bias, f"{current_reason} | OBV magnitude {abs(obv_value):,.0f} (not strong enough for reversal)"


class LiquidityDirectionAbsolutePriority:
    """
    🔥 JCTUSDT FIX: Jika long_liq LEBIH DEKAT dari short_liq,
    HFT PASTI akan dump dulu untuk sweep long stop.
    Ini harus override AbsoluteAggOverride (-1098).
    
    Logika:
    - long_liq < short_liq → HFT path: dump → sweep long stop → pump (mungkin)
    - short_liq < long_liq → HFT path: pump → sweep short stop → dump (mungkin)
    
    Jika selisih > 0.5% dan salah satu liq < 3%, ini bukan kebetulan.
    
    Priority: -1099.5 (antara BlowOffTop -1099 dan AbsoluteAgg -1098)
    Karena liquidity proximity adalah fakta fisik market, 
    lebih fundamental dari agg yang bisa di-spoof.
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               agg: float, ofi_bias: str, ofi_strength: float,
               obv_trend: str, change_5m: float,
               down_energy: float, up_energy: float,
               funding_rate: float = 0.0,      # ← TAMBAH
               obv_value: float = 0.0,          # ← TAMBAH
               rsi6: float = 50.0,              # ← TAMBAH
               rsi6_5m: float = 50.0) -> Dict:  # ← TAMBAH
        
        diff = abs(short_dist - long_dist)

        # ============================================================
        # 🔥 PATCH SKYAIUSDT: Guard OBV + Funding sebelum memaksa arah
        # ============================================================

        # Guard 1: OBV NEGATIVE_EXTREME + funding crowded long
        # = distribusi aktif, long_liq akan ditembus bukan di-bounce
        if (obv_trend == "NEGATIVE_EXTREME" and
            funding_rate > 0.0005 and       # crowded long
            obv_value < -30_000_000):       # distribusi besar
            # Dalam kondisi ini, long_liq bukan target bounce
            # tapi rintangan yang akan ditembus HFT saat dump
            # → paksa SHORT jika long_liq lebih dekat
            if long_dist < short_dist and long_dist < 5.0:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": (
                        f"OBV distribution + crowded long override: "
                        f"OBV={obv_value:,.0f} (NEGATIVE_EXTREME), "
                        f"funding={funding_rate:.4f} (crowded long), "
                        f"long liq {long_dist:.2f}% → HFT akan DUMP melewati long liq, bukan bounce"
                    ),
                    "priority": -1099
                }
            return {"override": False}  # biarkan logik lain tangkap

        # Guard 2: OBV NEGATIVE_EXTREME + RSI netral + harga turun
        # = falling distribution, tidak ada bounce
        if (obv_trend == "NEGATIVE_EXTREME" and
            obv_value < -50_000_000 and
            rsi6 < 45 and           # tidak oversold = tidak ada bounce catalyst
            change_5m < 0):         # harga sudah turun
            if long_dist < short_dist:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": (
                        f"OBV falling distribution: OBV={obv_value:,.0f}, "
                        f"RSI {rsi6:.1f} netral, price down {change_5m:.1f}% → "
                        f"tidak ada catalyst bounce, long liq akan ditembus"
                    ),
                    "priority": -1099
                }
            return {"override": False}

        # ============================================================
        # Logic asli (hanya jalan jika guard di atas tidak trigger)
        # ============================================================
        
        # Long liq jauh lebih dekat → HFT dump dulu
        if (long_dist < short_dist and
            long_dist < 3.0 and
            diff > 0.3):
            
            # Konfirmasi: ada sell pressure atau momentum turun
            sell_confirmed = (
                ofi_bias == "SHORT" or
                down_energy > up_energy or
                change_5m < 0 or
                (obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"] and agg < 0.7)
            )
            
            if sell_confirmed:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Liquidity direction priority: long liq {long_dist:.2f}% << short liq {short_dist:.2f}% (diff {diff:.2f}%), HFT akan dump ke long stop dulu",
                    "priority": -1099
                }
        
        # Short liq jauh lebih dekat → HFT pump dulu
        if (short_dist < long_dist and
            short_dist < 3.0 and
            diff > 0.3):
            
            buy_confirmed = (
                ofi_bias == "LONG" or
                up_energy > down_energy or
                change_5m > 0 or
                (obv_trend in ["POSITIVE_EXTREME", "POSITIVE"] and agg > 0.3)
            )
            
            if buy_confirmed:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Liquidity direction priority: short liq {short_dist:.2f}% << long liq {long_dist:.2f}% (diff {diff:.2f}%), HFT akan pump ke short stop dulu",
                    "priority": -1099
                }
        
        return {"override": False}


class OBVDistributionFundingTrap:
    """
    🔥 SKYAIUSDT EXACT PATTERN:
    
    Kombinasi paling berbahaya yang diabaikan sistem:
    OBV NEGATIVE_EXTREME + Funding positif (crowded long) + RSI netral
    
    Ini adalah "Distribution Trap":
    - OBV negatif = smart money sudah distribusi (jual perlahan)
    - Funding positif = retail masih optimis, banyak long terbuka  
    - RSI netral = tidak ada sinyal ekstrem yang visible → retail tidak curiga
    - long_liq dekat = HFT akan dump untuk sweep long stop
    
    Berbeda dengan kondisi bounce normal:
    - Bounce: OBV positif/netral + RSI oversold + down_energy tinggi
    - Distribution trap: OBV negatif + RSI netral + funding positif
    
    HFT menunggu kondisi ini: retail beli karena "long_liq dekat = bounce",
    padahal itu justru target dump.
    
    Priority: -1100 (sama dengan MasterSqueezeRule)
    """
    @staticmethod
    def detect(obv_trend: str, obv_value: float,
               funding_rate: float, rsi6: float, rsi6_5m: float,
               long_dist: float, short_dist: float,
               agg: float, change_5m: float,
               down_energy: float, volume_ratio: float) -> Dict:

        if funding_rate is None:
            return {"override": False}

        # Core pattern: OBV distribusi + crowded long + RSI netral
        obv_distributing = (
            obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"] and
            obv_value < -30_000_000
        )
        crowded_long = funding_rate > 0.0005
        rsi_neutral = 30 < rsi6 < 60  # tidak oversold, tidak overbought
        rsi5m_neutral = 30 < rsi6_5m < 65

        if (obv_distributing and
            crowded_long and
            rsi_neutral and
            rsi5m_neutral and
            long_dist < 5.0):         # ada long liq yang bisa disweep
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OBV Distribution + Funding Trap: "
                    f"OBV={obv_value:,.0f} ({obv_trend}), "
                    f"funding={funding_rate:.4f} (crowded long), "
                    f"RSI={rsi6:.1f} netral (retail tidak curiga), "
                    f"long liq {long_dist:.2f}% → HFT dump untuk sweep long stop"
                ),
                "priority": -1100
            }

        # Mirror: OBV akumulasi + crowded short + RSI netral = pump trap
        obv_accumulating = (
            obv_trend in ["POSITIVE_EXTREME", "POSITIVE"] and
            obv_value > 30_000_000
        )
        crowded_short = funding_rate < -0.0005
        rsi_neutral_high = 40 < rsi6 < 70

        if (obv_accumulating and
            crowded_short and
            rsi_neutral_high and
            short_dist < 5.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OBV Accumulation + Funding Trap: "
                    f"OBV={obv_value:,.0f} ({obv_trend}), "
                    f"funding={funding_rate:.4f} (crowded short), "
                    f"RSI={rsi6:.1f} netral, "
                    f"short liq {short_dist:.2f}% → HFT pump untuk sweep short stop"
                ),
                "priority": -1100
            }

        return {"override": False}


# ================================================================
# 🏦 EXCHANGE RISK ENGINE - Binance Survival Perspective
# ================================================================
# Layer baru (dari priority tertinggi ke terendah):
#   -1104 : GlobalPositionImbalance      ← "exchange akan biarkan market drop jika 80% user LONG"
#   -1103 : FundingNegativeShortLiqSqueeze — JANGAN UBAH
#   -1102 : MarkPriceGapDetector         ← "HFT main di gap mark vs last price"
#   -1101 : ExtremeFundingRateLongBan — JANGAN UBAH
#   -105  : InsuranceFundProtection      ← "exchange smooth/force cascade"
#   -104  : ADLRiskScoring               ← "risiko posisi profit dipotong"
#   -103  : ExchangeVolatilityControl    ← "exchange tidak mau chaotic, tidak mau sepi"
#   0     : ExchangeRiskScore            ← composite score, dipakai sebagai bobot ke prob_engine
# ================================================================

class GlobalPositionImbalance:
    """
    🏦 BINANCE SURVIVAL LAYER #1: Global Crowd Positioning
    
    Binance lihat: berapa % user LONG vs SHORT secara total.
    Jika 80%+ user LONG → sistem rawan jika harga naik
    → exchange "biarkan" dump terjadi untuk likuidasi massal
    
    Data proxy: funding_rate adalah indikator crowd positioning
    yang paling accessible tanpa akses internal Binance.
    
    Skala:
    - funding > +0.002  → crowded LONG  (>60% user long)
    - funding > +0.004  → very crowded  (>75% user long)
    - funding < -0.002  → crowded SHORT (>60% user short)
    - funding < -0.004  → very crowded  (>75% user short)
    
    Ditambah: open interest growth sebagai konfirmasi
    (OI naik + funding positif = semakin banyak user buka LONG)
    
    Priority: -1104 (TERTINGGI ABSOLUT — di atas semua signal lain)
    Karena ini perspektif exchange survival, bukan trading signal.
    """
    @staticmethod
    def detect(funding_rate: float, oi_delta: float,
               volume_ratio: float, change_5m: float,
               short_dist: float, long_dist: float) -> dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # ============================================================
        # CROWDED LONG → Exchange akan biarkan dump
        # ============================================================
        # Very crowded long + OI naik + harga belum dump
        if (funding_rate > 0.004 and      # >75% user long
            oi_delta > 1.0 and            # OI naik = makin banyak yang buka posisi
            change_5m > -2.0 and          # belum dump
            volume_ratio < 1.5):          # tidak ada kepanikan beli besar
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"GLOBAL POSITION IMBALANCE (EXCHANGE RISK): "
                    f"funding={funding_rate:.4f} (>75% user LONG), "
                    f"OI delta={oi_delta:.2f}% (posisi terus dibuka) → "
                    f"Binance akan biarkan dump untuk likuidasi massa"
                ),
                "priority": -1104,
                "exchange_risk_type": "CROWDED_LONG_SYSTEMIC"
            }
        
        # Crowded long (moderat) + long liq sangat dekat
        if (funding_rate > 0.003 and
            long_dist < 2.0 and
            oi_delta > 0.5):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"GLOBAL POSITION IMBALANCE: funding={funding_rate:.4f} crowded long, "
                    f"long liq {long_dist:.2f}% sangat dekat, OI tumbuh → "
                    f"exchange pathway: dump untuk sweep long stop"
                ),
                "priority": -1104,
                "exchange_risk_type": "CROWDED_LONG_LIQ_CLOSE"
            }
        
        # ============================================================
        # CROWDED SHORT → Exchange akan biarkan pump
        # ============================================================
        if (funding_rate < -0.004 and     # >75% user short
            oi_delta > 1.0 and
            change_5m < 2.0 and
            volume_ratio < 1.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"GLOBAL POSITION IMBALANCE (EXCHANGE RISK): "
                    f"funding={funding_rate:.4f} (>75% user SHORT), "
                    f"OI delta={oi_delta:.2f}% → "
                    f"Binance akan biarkan pump untuk likuidasi short massa"
                ),
                "priority": -1104,
                "exchange_risk_type": "CROWDED_SHORT_SYSTEMIC"
            }
        
        if (funding_rate < -0.003 and
            short_dist < 2.0 and
            oi_delta > 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"GLOBAL POSITION IMBALANCE: funding={funding_rate:.4f} crowded short, "
                    f"short liq {short_dist:.2f}% sangat dekat → "
                    f"exchange pathway: pump untuk sweep short stop"
                ),
                "priority": -1104,
                "exchange_risk_type": "CROWDED_SHORT_LIQ_CLOSE"
            }
        
        # ============================================================
        # 🔥 FIX DUSDT: funding hampir netral TAPI liq SANGAT asimetris
        # funding = -0.000056 (tidak trigger threshold di atas)
        # tapi long_liq = 34.71% >> short_liq = 5.97% (ratio 5.81x)
        # Di kondisi ini exchange tetap akan pilih dump (max pain)
        # ============================================================
        if funding_rate is not None and short_dist > 0 and long_dist > 0:
            liq_ratio_val = long_dist / short_dist if long_dist > short_dist else short_dist / long_dist
            
            # Liq sangat asimetris (>5x) + volume sangat rendah
            if liq_ratio_val >= 5.0 and volume_ratio < 0.5:
                if long_dist > short_dist:
                    # Dump lebih profitable walaupun funding netral
                    return {
                        "override": True,
                        "bias": "SHORT",
                        "reason": (
                            f"GLOBAL POSITION IMBALANCE (LIQ ASYMMETRY): "
                            f"long_liq={long_dist:.2f}% vs short_liq={short_dist:.2f}% "
                            f"(ratio {liq_ratio_val:.1f}x), volume={volume_ratio:.2f}x → "
                            f"exchange akan dump karena total liquidation value jauh lebih besar"
                        ),
                        "priority": -1104,
                        "exchange_risk_type": "LIQ_VALUE_ASYMMETRY_SHORT"
                    }
                else:
                    return {
                        "override": True,
                        "bias": "LONG",
                        "reason": (
                            f"GLOBAL POSITION IMBALANCE (LIQ ASYMMETRY): "
                            f"short_liq={short_dist:.2f}% vs long_liq={long_dist:.2f}% "
                            f"(ratio {liq_ratio_val:.1f}x), volume={volume_ratio:.2f}x → "
                            f"exchange akan pump karena total liquidation value jauh lebih besar"
                        ),
                        "priority": -1104,
                        "exchange_risk_type": "LIQ_VALUE_ASYMMETRY_LONG"
                    }
        
        return {"override": False}


class MarkPriceGapDetector:
    """
    🏦 BINANCE SURVIVAL LAYER #2: Mark Price vs Last Price Gap
    
    Di Binance Futures:
    - Liquidation menggunakan MARK PRICE (average dari multiple exchanges)
    - Candle/harga yang kita lihat = LAST PRICE (harga transaksi terakhir)
    
    HFT tahu ini dan BERMAIN DI GAP ini:
    - Jika last price > mark price (gap positif):
      → last price "terlalu tinggi" vs mark
      → mark price akan menarik last price turun
      → bias: SHORT (reversion ke mark)
    
    - Jika last price < mark price (gap negatif):
      → last price "terlalu rendah" vs mark
      → mark price akan menarik last price naik
      → bias: LONG (reversion ke mark)
    
    Priority: -1102 (di bawah GlobalPositionImbalance -1104)
    """
    @staticmethod
    def detect(mark_price: float, last_price: float,
               funding_rate: float, change_5m: float) -> dict:
        
        if mark_price is None or mark_price == 0 or last_price == 0:
            return {"override": False}
        
        # Hitung gap: positif = last > mark, negatif = last < mark
        gap_pct = ((last_price - mark_price) / mark_price) * 100
        
        # Gap signifikan: > 0.3%
        if abs(gap_pct) < 0.3:
            return {"override": False}
        
        # Last price terlalu tinggi dari mark → akan turun ke mark
        if gap_pct > 0.5:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"MARK PRICE GAP: last={last_price:.4f} > mark={mark_price:.4f} "
                    f"(gap +{gap_pct:.2f}%) → last price akan revert ke mark, "
                    f"liquidation threshold lebih rendah dari yang terlihat → SHORT"
                ),
                "priority": -1102,
                "gap_pct": gap_pct,
                "exchange_risk_type": "MARK_PRICE_REVERSION_DOWN"
            }
        
        # Last price terlalu rendah dari mark → akan naik ke mark
        if gap_pct < -0.5:
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"MARK PRICE GAP: last={last_price:.4f} < mark={mark_price:.4f} "
                    f"(gap {gap_pct:.2f}%) → last price akan revert ke mark, "
                    f"liquidation threshold lebih tinggi dari yang terlihat → LONG"
                ),
                "priority": -1102,
                "gap_pct": gap_pct,
                "exchange_risk_type": "MARK_PRICE_REVERSION_UP"
            }
        
        return {"override": False, "gap_pct": gap_pct}
    
    @staticmethod
    def estimate_gap_from_proxy(funding_rate: float, change_5m: float,
                                 volume_ratio: float) -> dict:
        """
        Proxy estimasi gap ketika mark price tidak tersedia.
        Berdasarkan divergence antara funding rate dan price action.
        """
        if funding_rate is None:
            return {"gap_estimated": 0, "direction": "NEUTRAL"}
        
        # Divergence: funding positif (crowded long) tapi harga turun
        if funding_rate > 0.002 and change_5m < -2.0:
            return {
                "gap_estimated": funding_rate * 100,
                "direction": "LAST_PREMIUM",
                "bias_implication": "SHORT"
            }
        
        # Divergence: funding negatif (crowded short) tapi harga naik
        if funding_rate < -0.002 and change_5m > 2.0:
            return {
                "gap_estimated": abs(funding_rate) * 100,
                "direction": "LAST_DISCOUNT",
                "bias_implication": "LONG"
            }
        
        return {"gap_estimated": 0, "direction": "NEUTRAL", "bias_implication": "NEUTRAL"}


class InsuranceFundProtection:
    """
    🏦 BINANCE SURVIVAL LAYER #3: Insurance Fund Protection Logic
    
    Binance punya insurance fund untuk menutupi kerugian saat
    bankrupt order tidak bisa diisi. Jika fund menipis:
    → Exchange akan "smooth" gerakan (delay cascade)
    → Atau "force cascade" untuk reset imbalance
    
    PROXY:
    - Volatility (change_5m sangat besar)
    - OI delta (tiba-tiba turun = mass liquidation terjadi)
    - Volume spike (panic = cascade sedang berlangsung)
    
    Priority: -105 (lower priority, gunakan sebagai modifier)
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float,
               oi_delta: float, funding_rate: float,
               short_dist: float, long_dist: float) -> dict:
        
        # ============================================================
        # CASCADE TERJADI: OI drop besar + volume spike
        # → Insurance fund terpakai → exchange akan smooth bounce
        # ============================================================
        if (oi_delta < -3.0 and         # OI turun drastis = mass liq
            volume_ratio > 3.0 and       # volume spike = panik
            abs(change_5m) > 5.0):       # harga bergerak ekstrem
            
            # Jika harga baru saja dump keras
            if change_5m < -5.0:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"INSURANCE FUND PROTECTION: OI drop {oi_delta:.2f}% "
                        f"(mass liquidation), volume {volume_ratio:.2f}x, "
                        f"price -{abs(change_5m):.1f}% → cascade terjadi, "
                        f"exchange akan smooth bounce (insurance fund dipakai)"
                    ),
                    "priority": -105,
                    "exchange_risk_type": "CASCADE_SMOOTHING_BOUNCE"
                }
            
            # Jika harga baru saja pump keras
            if change_5m > 5.0:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": (
                        f"INSURANCE FUND PROTECTION: OI drop {oi_delta:.2f}% "
                        f"(mass short liq), volume {volume_ratio:.2f}x, "
                        f"price +{change_5m:.1f}% → short cascade, "
                        f"exchange akan smooth setelah sweep"
                    ),
                    "priority": -105,
                    "exchange_risk_type": "CASCADE_SMOOTHING_DUMP"
                }
        
        # ============================================================
        # VOLATILITY EKSTREM + OI MASIH TINGGI
        # → Exchange belum selesai, mungkin akan force lebih lanjut
        # ============================================================
        if (abs(change_5m) > 8.0 and
            oi_delta > 2.0 and      # OI masih naik = belum ada mass liq
            volume_ratio > 2.0):    # volume tinggi = tekanan masih ada
            
            if change_5m > 8.0:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"INSURANCE FUND: extreme pump {change_5m:.1f}% + OI tumbuh "
                        f"{oi_delta:.2f}% → shorts keep opening, squeeze belum selesai"
                    ),
                    "priority": -105,
                    "exchange_risk_type": "ONGOING_SQUEEZE"
                }
        
        return {"override": False}


class ADLRiskScoring:
    """
    🏦 BINANCE SURVIVAL LAYER #4: Auto-Deleveraging (ADL) Risk
    
    ADL terjadi ketika:
    1. Posisi bankrupt (modal habis)
    2. Insurance fund tidak cukup untuk menutupi
    3. → Binance POTONG posisi PROFIT trader lain untuk kompensasi
    
    Priority: -104
    """
    @staticmethod
    def score(funding_rate: float, volume_ratio: float,
              rsi6: float, change_5m: float,
              short_dist: float, long_dist: float) -> dict:
        
        if funding_rate is None:
            return {"adl_risk": 0, "override": False}
        
        adl_risk = 0
        risk_factors = []
        
        # Factor 1: Funding sangat ekstrem
        if abs(funding_rate) > 0.005:
            adl_risk += 3
            risk_factors.append(f"extreme_funding={funding_rate:.4f}")
        elif abs(funding_rate) > 0.003:
            adl_risk += 2
            risk_factors.append(f"high_funding={funding_rate:.4f}")
        
        # Factor 2: Volume sangat rendah (tidak ada counterparty)
        if volume_ratio < 0.3:
            adl_risk += 2
            risk_factors.append(f"very_low_volume={volume_ratio:.2f}x")
        elif volume_ratio < 0.5:
            adl_risk += 1
            risk_factors.append(f"low_volume={volume_ratio:.2f}x")
        
        # Factor 3: RSI di zona ekstrem
        if rsi6 > 90 or rsi6 < 10:
            adl_risk += 2
            risk_factors.append(f"extreme_rsi={rsi6:.1f}")
        elif rsi6 > 80 or rsi6 < 20:
            adl_risk += 1
            risk_factors.append(f"high_rsi={rsi6:.1f}")
        
        # Factor 4: Likuiditas terlalu dekat (posisi banyak approaching margin call)
        if min(short_dist, long_dist) < 1.0:
            adl_risk += 2
            risk_factors.append(f"ultra_close_liq={min(short_dist, long_dist):.2f}%")
        
        # Factor 5: Pergerakan ekstrem
        if abs(change_5m) > 8.0:
            adl_risk += 2
            risk_factors.append(f"extreme_move={change_5m:.1f}%")
        elif abs(change_5m) > 5.0:
            adl_risk += 1
            risk_factors.append(f"large_move={change_5m:.1f}%")
        
        # ADL Risk >= 6: Exchange akan prefer reversi ke zona aman
        if adl_risk >= 6:
            if funding_rate > 0:
                safe_direction = "SHORT"
            else:
                safe_direction = "LONG"
            
            return {
                "adl_risk": adl_risk,
                "override": True,
                "bias": safe_direction,
                "reason": (
                    f"ADL RISK HIGH ({adl_risk}/10): {', '.join(risk_factors)} → "
                    f"exchange prefer {safe_direction} untuk mengurangi ADL exposure"
                ),
                "priority": -104,
                "exchange_risk_type": "ADL_RISK_REVERSION"
            }
        
        return {
            "adl_risk": adl_risk,
            "override": False,
            "risk_factors": risk_factors
        }


class ExchangeVolatilityControl:
    """
    🏦 BINANCE SURVIVAL LAYER #5: Volatility Control Layer
    
    Exchange ingin:
    ✅ Cukup volatil (volume, fee revenue)
    ❌ Tidak terlalu chaotic (system crash, reputasi)
    ❌ Tidak terlalu sepi (tidak ada fee)
    
    Priority: -103
    """
    @staticmethod
    def detect(volume_ratio: float, change_5m: float,
               short_dist: float, long_dist: float,
               funding_rate: float, rsi6: float) -> dict:
        
        # ============================================================
        # TERLALU CHAOTIC: Volume spike + gerakan besar
        # → Exchange akan smooth (bias reversi)
        # ============================================================
        if volume_ratio > 5.0 and abs(change_5m) > 10.0:
            reversal_bias = "SHORT" if change_5m > 0 else "LONG"
            return {
                "override": True,
                "bias": reversal_bias,
                "reason": (
                    f"EXCHANGE VOLATILITY CONTROL (CHAOTIC): "
                    f"volume {volume_ratio:.1f}x dengan move {change_5m:.1f}% → "
                    f"market terlalu chaotic, exchange akan smooth → reversi ke {reversal_bias}"
                ),
                "priority": -103,
                "exchange_risk_type": "VOLATILITY_SMOOTHING"
            }
        
        # ============================================================
        # TERLALU SEPI: Volume sangat rendah, range sempit
        # → Likuiditas akan dirangsang ke arah terdekat
        # ============================================================
        if (volume_ratio < 0.2 and
            abs(change_5m) < 0.5 and
            (short_dist < 5.0 or long_dist < 5.0)):
            
            if short_dist < long_dist:
                target_bias = "LONG"
                target_liq = short_dist
            else:
                target_bias = "SHORT"
                target_liq = long_dist
            
            return {
                "override": True,
                "bias": target_bias,
                "reason": (
                    f"EXCHANGE VOLATILITY CONTROL (TOO QUIET): "
                    f"volume {volume_ratio:.2f}x, move hanya {change_5m:.2f}% → "
                    f"market terlalu sepi, liq terdekat {target_liq:.2f}% akan di-rangsang → {target_bias}"
                ),
                "priority": -103,
                "exchange_risk_type": "VOLATILITY_STIMULATION"
            }
        
        return {"override": False}


class ExchangeRiskScore:
    """
    🏦 COMPOSITE: Exchange Risk Score
    
    Ini adalah "composite score" yang menggabungkan semua faktor
    exchange risk menjadi satu angka 0-10.
    
    Score ini digunakan sebagai:
    1. Bobot tambahan ke prob_engine
    2. Warning di output
    3. Confidence modifier
    """
    @staticmethod
    def calculate(funding_rate: float, short_dist: float, long_dist: float,
                  volume_ratio: float, change_5m: float,
                  oi_delta: float = 0.0) -> dict:
        
        risk_score = 0
        risk_breakdown = {}
        
        if funding_rate is not None:
            if abs(funding_rate) > 0.003:
                risk_score += 2
                risk_breakdown["funding"] = f"EXTREME ({funding_rate:.4f})"
            elif abs(funding_rate) > 0.001:
                risk_score += 1
                risk_breakdown["funding"] = f"ELEVATED ({funding_rate:.4f})"
            
            if funding_rate > 0 and long_dist < short_dist:
                risk_score += 2
                risk_breakdown["position_bias"] = "DANGEROUS: crowded long + long liq close"
            elif funding_rate < 0 and short_dist < long_dist:
                risk_score += 2
                risk_breakdown["position_bias"] = "DANGEROUS: crowded short + short liq close"
        
        if volume_ratio < 0.3:
            risk_score += 2
            risk_breakdown["volume"] = f"VERY LOW ({volume_ratio:.2f}x)"
        elif volume_ratio < 0.5:
            risk_score += 1
            risk_breakdown["volume"] = f"LOW ({volume_ratio:.2f}x)"
        
        if abs(change_5m) > 5.0:
            risk_score += 1
            risk_breakdown["volatility"] = f"HIGH ({change_5m:.1f}%)"
        
        if short_dist < 2.0 and long_dist < 2.0:
            risk_score += 3
            risk_breakdown["imbalance"] = "EXTREME: both liq < 2%"
        elif min(short_dist, long_dist) < 1.0:
            risk_score += 2
            risk_breakdown["imbalance"] = f"CRITICAL: liq {min(short_dist,long_dist):.2f}%"
        
        if oi_delta > 2.0 and abs(change_5m) > 3.0:
            risk_score += 1
            risk_breakdown["oi_growth"] = f"OI naik {oi_delta:.2f}% saat volatile"
        
        risk_score = min(risk_score, 10)
        
        # ============================================================
        # 🔥 FIX DUSDT BUG: Asymmetric liquidity HARUS masuk risk score
        # Ratio liq ekstrem = exchange risk tinggi walaupun funding netral
        # ============================================================
        liq_ratio = 1.0
        if short_dist > 0 and long_dist > 0:
            liq_ratio = max(long_dist / short_dist, short_dist / long_dist)
        
        if liq_ratio >= 8.0:
            risk_score += 3
            risk_breakdown["liq_asymmetry"] = f"EXTREME ratio {liq_ratio:.1f}x (max pain dominant)"
        elif liq_ratio >= 5.0:
            risk_score += 2
            risk_breakdown["liq_asymmetry"] = f"STRONG ratio {liq_ratio:.1f}x"
        elif liq_ratio >= 3.0:
            risk_score += 1
            risk_breakdown["liq_asymmetry"] = f"MODERATE ratio {liq_ratio:.1f}x"
        
        if oi_delta > 2.0 and abs(change_5m) > 3.0:
            risk_score += 1
            risk_breakdown["oi_growth"] = f"OI naik {oi_delta:.2f}% saat volatile"
        
        risk_score = min(risk_score, 10)
        
        # ============================================================
        # 🔥 FIX: safe_direction HARUS mempertimbangkan liq asymmetry
        # Bukan hanya funding rate!
        # DUSDT: funding -0.000056 (hampir netral) → lama: NEUTRAL → salah
        #        tapi long_liq 34% >> short_liq 6% → harusnya: SHORT
        # ============================================================
        safe_direction = "NEUTRAL"
        
        # Priority 1: Asymmetric liquidity menentukan safe direction
        if long_dist > 0 and short_dist > 0:
            if long_dist > short_dist * 3.0 and volume_ratio < 0.7:
                # Long liq jauh lebih besar → dump lebih profitable → SHORT safer
                safe_direction = "SHORT"
                risk_breakdown["safe_dir_reason"] = f"long_liq {long_dist:.1f}% >> short_liq {short_dist:.1f}% (ratio {long_dist/short_dist:.1f}x)"
            elif short_dist > long_dist * 3.0 and volume_ratio < 0.7:
                # Short liq jauh lebih besar → pump lebih profitable → LONG safer
                safe_direction = "LONG"
                risk_breakdown["safe_dir_reason"] = f"short_liq {short_dist:.1f}% >> long_liq {long_dist:.1f}% (ratio {short_dist/long_dist:.1f}x)"
        
        # Priority 2: Funding rate (hanya jika tidak ada asymmetry override)
        if safe_direction == "NEUTRAL" and risk_score >= 5:
            if funding_rate is not None and funding_rate > 0.001:
                safe_direction = "SHORT"
            elif funding_rate is not None and funding_rate < -0.001:
                safe_direction = "LONG"
        
        return {
            "risk_score": risk_score,
            "risk_level": "CRITICAL" if risk_score >= 8 else "HIGH" if risk_score >= 5 else "MEDIUM" if risk_score >= 3 else "LOW",
            "risk_breakdown": risk_breakdown,
            "safe_direction": safe_direction,
            "liq_ratio": liq_ratio,
            "exchange_perspective": (
                f"Exchange Risk Score: {risk_score}/10 "
                f"(safe={safe_direction}, liq_ratio={liq_ratio:.1f}x, "
                f"{', '.join(f'{k}={v}' for k, v in risk_breakdown.items() if k != 'safe_dir_reason')})"
            )
        }


class AsymmetricLiquidityMaxPain:
    """
    🏦 EXCHANGE MAX PAIN ROUTE — Asymmetric Liquidity Logic
    
    DUSDT CASE:
      Short liq: +5.97%  → reward naik = kecil
      Long liq:  -34.71% → reward turun = BESAR
      Volume: 0.34x      → market tipis = mudah digerakkan
    
    INSIGHT:
      Market tidak selalu menuju liquidity TERDEKAT.
      Market menuju liquidity TERBESAR saat kondisi memungkinkan.
    
    RULE:
      if long_liq > short_liq * RATIO_THRESHOLD and volume_ratio < 0.6:
          bias = SHORT  (max pain ke bawah lebih profitable)
      
      if short_liq > long_liq * RATIO_THRESHOLD and volume_ratio < 0.6:
          bias = LONG   (max pain ke atas lebih profitable)
    
    RATIO_THRESHOLD:
      - 3x = moderate asymmetry → override jika sinyal lain lemah
      - 5x = strong asymmetry   → override hampir selalu
      - 8x = extreme asymmetry  → override selalu (termasuk liq proximity)
    
    DUSDT ratio: 34.71 / 5.97 = 5.81x → STRONG ASYMMETRY → harusnya SHORT
    
    Priority:
      - ratio >= 8x: -1104 (setara GlobalPositionImbalance)
      - ratio >= 5x: -1100 (setara MasterSqueezeRule)
      - ratio >= 3x: -1075 (setara LiquidityMagnetOverride)
    
    GUARDS (jangan override jika):
      1. short_dist < 1.0 dan ada momentum kuat → squeeze override
      2. RSI sangat ekstrem (< 10 atau > 90) → momentum lebih dominan
      3. OFI sangat kuat searah dengan liq terdekat
      4. Volume spike ekstrem (> 3x) → momentum nyata
    """
    
    # Ratio threshold untuk asymmetric detection
    RATIO_EXTREME  = 8.0   # 8x lipat → override tertinggi
    RATIO_STRONG   = 5.0   # 5x lipat → override kuat
    RATIO_MODERATE = 3.0   # 3x lipat → override moderat
    
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               volume_ratio: float, change_5m: float,
               rsi6: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float,
               funding_rate: float,
               up_energy: float, down_energy: float,
               obv_trend: str = "NEUTRAL",
               agg: float = 0.5) -> dict:
        
        # Guard: kedua liq terlalu jauh → tidak relevan
        if short_dist > 20.0 and long_dist > 20.0:
            return {"override": False}
        
        # Guard: RSI sangat ekstrem → momentum lebih dominan dari max pain
        if rsi6 < 10 or rsi6 > 90:
            return {"override": False}
        
        # Guard: volume spike nyata → momentum override max pain
        if volume_ratio > 3.0:
            return {"override": False}
        
        # Hitung ratio asimetri
        # Case A: long_liq >> short_liq → dump lebih profitable
        if long_dist > 0 and short_dist > 0:
            long_to_short_ratio = long_dist / short_dist  # e.g. 34.71/5.97 = 5.81
            short_to_long_ratio = short_dist / long_dist
        else:
            return {"override": False}
        
        # ============================================================
        # CASE A: Long liq JAUH lebih besar → SHORT (dump lebih profitable)
        # DUSDT exact case: ratio 5.81x
        # ============================================================
        if long_dist > short_dist * AsymmetricLiquidityMaxPain.RATIO_MODERATE:
            
            # Guard: jika short liq sangat dekat (<1%) dan ada buy momentum → jangan override
            if short_dist < 1.0 and up_energy > 1.0 and change_5m > 3.0:
                return {"override": False}
            
            # Guard: OFI LONG sangat kuat + no seller → squeeze lebih dominan
            if (ofi_bias == "LONG" and ofi_strength > 0.8 and 
                down_energy < 0.01 and short_dist < 2.0):
                return {"override": False}
            
            # Guard: RSI sangat oversold → bouncing dari bottom, bukan max pain
            if rsi6 < 20 and rsi6_5m < 25:
                return {"override": False}
            
            # Tentukan priority berdasarkan ratio
            if long_to_short_ratio >= AsymmetricLiquidityMaxPain.RATIO_EXTREME:
                # 8x+ → override tertinggi, hampir tidak ada kondisi yang menghalangi
                priority = -1104
                strength_label = "EXTREME"
                weight = 10.05
            elif long_to_short_ratio >= AsymmetricLiquidityMaxPain.RATIO_STRONG:
                # 5x-8x → override kuat
                priority = -1100
                strength_label = "STRONG"
                weight = 10.0
            else:
                # 3x-5x → override moderat (hanya jika volume rendah)
                if volume_ratio > 0.7:
                    return {"override": False}  # butuh volume rendah untuk moderate
                priority = -1075
                strength_label = "MODERATE"
                weight = 3.5
            
            # Konfirmasi tambahan (tidak wajib, tapi memperkuat)
            confirmations = []
            if volume_ratio < 0.5:
                confirmations.append(f"vol={volume_ratio:.2f}x (sangat rendah→mudah dump)")
            if funding_rate is not None and funding_rate > 0.0001:
                confirmations.append(f"funding={funding_rate:.5f} (crowded long)")
            if obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"]:
                confirmations.append("OBV negatif (distribusi)")
            if change_5m < 0:
                confirmations.append(f"price down {change_5m:.1f}%")
            if down_energy > up_energy:
                confirmations.append(f"down_energy={down_energy:.2f}>up_energy={up_energy:.2f}")
            if agg < 0.4:
                confirmations.append(f"agg={agg:.2f} (majority sell)")
            
            conf_str = (", ".join(confirmations)) if confirmations else "no extra confirmation"
            
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"ASYMMETRIC LIQUIDITY MAX PAIN ({strength_label}): "
                    f"long_liq={long_dist:.2f}% vs short_liq={short_dist:.2f}% "
                    f"(ratio {long_to_short_ratio:.1f}x) → "
                    f"dump {long_dist:.1f}% jauh lebih profitable dari squeeze {short_dist:.1f}%, "
                    f"exchange pilih SHORT | {conf_str}"
                ),
                "priority": priority,
                "liq_ratio": long_to_short_ratio,
                "weight": weight,
                "exchange_risk_type": "MAX_PAIN_SHORT"
            }
        
        # ============================================================
        # CASE B: Short liq JAUH lebih besar → LONG (pump lebih profitable)
        # Mirror case
        # ============================================================
        if short_dist > long_dist * AsymmetricLiquidityMaxPain.RATIO_MODERATE:
            
            # Guard: jika long liq sangat dekat (<1%) dan ada sell momentum → jangan override
            if long_dist < 1.0 and down_energy > 1.0 and change_5m < -3.0:
                return {"override": False}
            
            # Guard: OFI SHORT sangat kuat + no buyer
            if (ofi_bias == "SHORT" and ofi_strength > 0.8 and 
                up_energy < 0.01 and long_dist < 2.0):
                return {"override": False}
            
            # Guard: RSI sangat overbought
            if rsi6 > 80 and rsi6_5m > 75:
                return {"override": False}
            
            if short_to_long_ratio >= AsymmetricLiquidityMaxPain.RATIO_EXTREME:
                priority = -1104
                strength_label = "EXTREME"
                weight = 10.05
            elif short_to_long_ratio >= AsymmetricLiquidityMaxPain.RATIO_STRONG:
                priority = -1100
                strength_label = "STRONG"
                weight = 10.0
            else:
                if volume_ratio > 0.7:
                    return {"override": False}
                priority = -1075
                strength_label = "MODERATE"
                weight = 3.5
            
            confirmations = []
            if volume_ratio < 0.5:
                confirmations.append(f"vol={volume_ratio:.2f}x")
            if funding_rate is not None and funding_rate < -0.0001:
                confirmations.append(f"funding={funding_rate:.5f} (crowded short)")
            if obv_trend in ["POSITIVE_EXTREME", "POSITIVE"]:
                confirmations.append("OBV positif (akumulasi)")
            if change_5m > 0:
                confirmations.append(f"price up {change_5m:.1f}%")
            if up_energy > down_energy:
                confirmations.append(f"up_energy={up_energy:.2f}>down_energy={down_energy:.2f}")
            if agg > 0.6:
                confirmations.append(f"agg={agg:.2f} (majority buy)")
            
            conf_str = ", ".join(confirmations) if confirmations else "no extra confirmation"
            
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"ASYMMETRIC LIQUIDITY MAX PAIN ({strength_label}): "
                    f"short_liq={short_dist:.2f}% vs long_liq={long_dist:.2f}% "
                    f"(ratio {short_to_long_ratio:.1f}x) → "
                    f"pump {short_dist:.1f}% jauh lebih profitable dari dump {long_dist:.1f}%, "
                    f"exchange pilih LONG | {conf_str}"
                ),
                "priority": priority,
                "liq_ratio": short_to_long_ratio,
                "weight": weight,
                "exchange_risk_type": "MAX_PAIN_LONG"
            }
        
        return {"override": False}


class OBVTrendFundingConsensus:
    """
    🔥 Ketika OBV trend dan funding rate menunjuk arah yang SAMA,
    ini adalah sinyal consensus yang sangat kuat.
    
    Rule:
    - OBV NEGATIVE + funding positif (crowded long) = SHORT kuat
      (Smart money jual, retail masih beli = distribusi)
    - OBV POSITIVE + funding negatif (crowded short) = LONG kuat  
      (Smart money beli, retail masih jual = akumulasi)
    
    Ini harus override sebagian besar sinyal lain karena mencerminkan
    posisi smart money yang sebenarnya.
    
    Priority: -213 (di atas AskBidSlopeImbalance -210)
    """
    @staticmethod
    def detect(obv_trend: str, obv_value: float,
               funding_rate: float, rsi6: float,
               long_dist: float, short_dist: float,
               volume_ratio: float, change_5m: float) -> Dict:

        if funding_rate is None:
            return {"override": False}

        # OBV negatif + funding positif = distribusi aktif
        if (obv_trend in ["NEGATIVE_EXTREME", "NEGATIVE"] and
            obv_value < -20_000_000 and
            funding_rate > 0.0003 and
            rsi6 < 55 and              # tidak oversold (ada ruang turun)
            volume_ratio < 0.7):       # volume rendah = distribusi diam-diam
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"OBV-Funding consensus SHORT: OBV={obv_value:,.0f} distributing, "
                    f"funding={funding_rate:.4f} crowded long → "
                    f"smart money selling while retail holds long"
                ),
                "priority": -213
            }

        # OBV positif + funding negatif = akumulasi aktif
        if (obv_trend in ["POSITIVE_EXTREME", "POSITIVE"] and
            obv_value > 20_000_000 and
            funding_rate < -0.0003 and
            rsi6 > 45 and
            volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"OBV-Funding consensus LONG: OBV={obv_value:,.0f} accumulating, "
                    f"funding={funding_rate:.4f} crowded short → "
                    f"smart money buying while retail holds short"
                ),
                "priority": -213
            }

        return {"override": False}


class VolumeTrapDetector:
    @staticmethod
    def detect(volume_ratio: float, change_5m: float, bias: str) -> Dict:
        if volume_ratio < 0.4 and abs(change_5m) > 2.0:
            return {
                "warning": True,
                "reason": (
                    f"Low Volume Trap: Volume {volume_ratio:.2f}x but price moved {change_5m:.1f}%. "
                    f"Possible HFT spoof."
                ),
                "action": "DOWNGRADE_CONFIDENCE"
            }
        return {"warning": False}


class OrderFlowImbalance:
    @staticmethod
    def calculate(trades: List[Dict], window_ms: int = 1000) -> Dict:
        if not trades:
            return {"bias": "NEUTRAL", "strength": 0}
        now = time.time() * 1000
        window_start = now - window_ms
        buy_vol = 0.0
        sell_vol = 0.0
        for t in trades:
            ts = t.get('E') or t.get('T') or t.get('time', 0)
            if ts < window_start:
                continue
            qty = t.get('q') or t.get('qty')
            if qty is None:
                continue
            qty = float(qty)
            is_sell = t.get('m', False) or t.get('isBuyerMaker', False)
            if not is_sell:
                buy_vol += qty
            else:
                sell_vol += qty
        total = buy_vol + sell_vol
        if total == 0:
            return {"bias": "NEUTRAL", "strength": 0}
        ofi = (buy_vol - sell_vol) / total
        if ofi > 0.3:
            return {"bias": "LONG", "strength": ofi}
        elif ofi < -0.3:
            return {"bias": "SHORT", "strength": abs(ofi)}
        return {"bias": "NEUTRAL", "strength": 0}


class IcebergDetector:
    @staticmethod
    def detect(trades: List[Dict], price_level: float, tolerance: float = 0.001) -> Dict:
        same_price_trades = []
        for t in trades:
            price = t.get('p') or t.get('price')
            if price is None:
                continue
            price = float(price)
            if abs(price - price_level) < tolerance:
                qty = t.get('q') or t.get('qty')
                if qty is not None:
                    same_price_trades.append(t)
        if len(same_price_trades) > 20:
            total_qty = sum(float(t.get('q', t.get('qty', 0))) for t in same_price_trades)
            if total_qty > 100000:
                first = same_price_trades[0]
                is_sell = first.get('m', False) or first.get('isBuyerMaker', False)
                side = "SELL" if is_sell else "BUY"
                return {"detected": True, "side": side, "total_qty": total_qty}
        return {"detected": False}


class CrossExchangeLeader:
    @staticmethod
    def check_leader(symbol: str) -> Dict:
        return {"leader": "NEUTRAL", "confidence": 0}


class FundingRateTrap:
    @staticmethod
    def detect(funding_rate: float, open_interest: float) -> Dict:
        if funding_rate > 0.01 and open_interest > 1000000:
            return {"bias": "SHORT", "reason": "Long squeeze imminent"}
        elif funding_rate < -0.01 and open_interest > 1000000:
            return {"bias": "LONG", "reason": "Short squeeze imminent"}
        return {"bias": "NEUTRAL"}


class LiquidationHeatMap:
    @staticmethod
    def fetch_real_liq(symbol: str) -> Dict:
        return {"bias": "NEUTRAL"}


class OBVPriceVolumeDivergence:
    """
    🔥 JCTUSDT PATTERN: OBV sangat besar tapi harga tidak naik proporsional.
    
    OBV 1.1 miliar = volume transaksi akumulatif sangat besar.
    Jika harga hanya +1.3% sementara OBV sudah besar,
    ini artinya: smart money beli → harga naik sedikit → distribusi diam-diam.
    
    Logika:
    - Jika obv_value > 500 juta DAN change_5m < 3% DAN long_liq < short_liq
    → distribusi → SHORT
    
    Priority: -212 (di atas EnergyGapTrap -215)
    """
    @staticmethod
    def detect(obv_value: float, change_5m: float,
               long_dist: float, short_dist: float,
               agg: float, volume_ratio: float) -> Dict:
        
        # OBV sangat besar tapi harga tidak naik proporsional = distribusi
        if (obv_value > 500_000_000 and      # OBV raksasa
            0 < change_5m < 3.0 and          # harga naik sedikit
            long_dist < short_dist and        # long liq lebih dekat
            long_dist < 3.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OBV-Price divergence: OBV={obv_value:,.0f} raksasa tapi price hanya +{change_5m:.1f}%, long liq {long_dist:.2f}% lebih dekat → smart money distribusi diam-diam, dump imminent",
                "priority": -212
            }
        
        # Mirror: OBV sangat negatif tapi harga tidak turun proporsional = akumulasi
        if (obv_value < -500_000_000 and
            -3.0 < change_5m < 0 and
            short_dist < long_dist and
            short_dist < 3.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OBV-Price divergence: OBV={obv_value:,.0f} sangat negatif tapi price hanya {change_5m:.1f}%, short liq {short_dist:.2f}% lebih dekat → smart money akumulasi diam-diam, pump imminent",
                "priority": -212
            }
        
        return {"override": False}


class QuantCrowdednessDetector:
    @staticmethod
    def detect(volume_ratio: float, volatility: float, open_interest_growth: float) -> Dict:
        crowded_score = 0
        if volume_ratio > 3.0:
            crowded_score += 2
        if volatility > 0.05:
            crowded_score += 1
        if open_interest_growth > 10:
            crowded_score += 2
        if crowded_score >= 4:
            return {
                "crowded": True,
                "action": "REDUCE_POSITION",
                "position_multiplier": 0.3,
                "reason": f"Quant crowdedness high ({crowded_score}/5)",
                "priority": 0
            }
        return {"crowded": False, "position_multiplier": 1.0, "priority": 0}


class FundingNegativeOBVPositiveSqueezeFirst:
    """
    🔥 PUFFERUSDT PATTERN: Funding negatif + OBV POSITIVE EXTREME + short_liq dekat
    
    Ini adalah setup "squeeze dulu, dump belakangan":
    1. Funding negatif = crowded short (banyak yang short)
    2. OBV positif ekstrem = ada akumulasi nyata  
    3. Short_liq dekat = target sweep ada di dekat
    
    HFT akan PUMP dulu untuk liquidasi semua short (sweep short stop),
    BARU KEMUDIAN dump. Jadi bias jangka pendek = LONG.
    
    Bedanya dengan CUSDT:
    - CUSDT: OBV NEUTRAL + RSI_5m=97 → sudah terlalu tinggi, langsung dump
    - PUFFERUSDT: OBV POSITIVE EXTREME + RSI_5m=62 → masih ada ruang pump
    
    Priority: -1088 (antara ExtremeFundingBan -1085 dan ExtremeOversoldIgnore -1080)
    """
    @staticmethod
    def detect(funding_rate: float, obv_trend: str, obv_value: float,
               short_dist: float, long_dist: float,
               rsi6_5m: float, down_energy: float,
               change_5m: float, up_energy: float = 0.0) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # Funding negatif + OBV positif + short_liq dekat = sweep short dulu
        if (funding_rate < -0.003 and          # crowded short
            obv_trend == "POSITIVE_EXTREME" and # ada akumulasi
            obv_value > 50_000_000 and
            short_dist < 3.0 and               # target sweep dekat
            short_dist < long_dist and
            rsi6_5m < 80 and                   # belum blow-off top
            down_energy < 0.1):                # tidak ada seller aktif
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Funding negative + OBV positive + short liq close: funding={funding_rate:.4f} (crowded short), OBV={obv_value:,.0f} positive extreme, short liq {short_dist:.2f}% → HFT sweep short stops FIRST before dump",
                "priority": -1088
            }
        
        # Mirror: Funding positif + OBV negatif + long liq dekat = sweep long dulu
        if (funding_rate > 0.003 and
            obv_trend == "NEGATIVE_EXTREME" and
            obv_value < -50_000_000 and
            long_dist < 3.0 and
            long_dist < short_dist and
            rsi6_5m > 20 and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Funding positive + OBV negative + long liq close: funding={funding_rate:.4f} (crowded long), OBV={obv_value:,.0f} negative extreme, long liq {long_dist:.2f}% → HFT sweep long stops FIRST before pump",
                "priority": -1088
            }
        
        return {"override": False}


class FundingNegativeShortLiqSqueeze:
    """
    🔥 SIRENUSDT FIX: Funding sangat negatif tapi short liq SANGAT DEKAT
    
    Funding negatif ekstrem = semua orang LONG (crowded).
    Biasanya HFT akan dump untuk likuidasi long.
    TAPI jika short liq lebih dekat (<1.5%), HFT akan PUMP dulu
    untuk menyapu short stop sebelum dump.
    
    Priority: -1103 (di atas ExtremeFundingRateLongBan -1101)
    """
    @staticmethod
    def detect(funding_rate: float, short_dist: float, long_dist: float,
               up_energy: float, down_energy: float, rsi6: float,
               volume_ratio: float) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # Funding negatif ekstrem + short liq sangat dekat = pump dulu
        if (funding_rate < -0.005 and
            short_dist < 1.5 and
            short_dist < long_dist and
            up_energy > 0.1 and
            down_energy < 0.01 and
            rsi6 < 80 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"FUNDING NEGATIVE + SHORT LIQ SUPER CLOSE: funding={funding_rate:.4f} (crowded long), short liq {short_dist:.2f}% sangat dekat, up_energy={up_energy:.2f} → HFT sweep short stops FIRST before dump",
                "priority": -1103
            }
        
        # Mirror: funding positif ekstrem + long liq sangat dekat = dump dulu
        if (funding_rate > 0.005 and
            long_dist < 1.5 and
            long_dist < short_dist and
            down_energy > 0.1 and
            up_energy < 0.01 and
            rsi6 > 20 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"FUNDING POSITIVE + LONG LIQ SUPER CLOSE: funding={funding_rate:.4f} (crowded short), long liq {long_dist:.2f}% sangat dekat, down_energy={down_energy:.2f} → HFT sweep long stops FIRST before pump",
                "priority": -1103
            }
        
        return {"override": False}


class FundingNegativeShortSqueezeOverride:
    """
    🔥 SIRENUSDT FIX: Funding sangat negatif + long_liq DEKAT = SHORT SQUEEZE
    
    Funding negatif ekstrem berarti semua orang sudah SHORT.
    Jika long_liq juga dekat (< 2%), HFT akan PUMP untuk:
    1. Sweep long liq (likuidasi long yang ada)
    2. Squeeze semua short yang terperangkap
    
    Ini BERBEDA dari CUSDT (funding negatif + RSI_5m 97 + long_liq jauh = dump).
    
    Rule: funding < -0.005 AND long_liq < 2.5% AND RSI6 < 40 AND down_energy = 0
    → LONG (short squeeze)
    
    Priority: -1102 (TERTINGGI — override funding ban!)
    Karena liquidity target yang sangat dekat override funding signal.
    """
    @staticmethod
    def detect(funding_rate: float, long_dist: float, short_dist: float,
               rsi6: float, down_energy: float, up_energy: float,
               rsi6_5m: float, obv_trend: str) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # Funding negatif ekstrem + long_liq sangat dekat + oversold = short squeeze
        if (funding_rate < -0.005 and
            long_dist < 2.5 and           # target dekat
            long_dist < short_dist and    # long lebih dekat dari short
            rsi6 < 45 and                 # tidak overbought
            rsi6_5m < 65 and              # 5m tidak overbought
            down_energy < 0.1):           # tidak ada seller aktif
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Funding negative short squeeze: funding={funding_rate:.4f} (crowded short), long liq {long_dist:.2f}% SANGAT dekat, RSI {rsi6:.1f} oversold, no sellers → HFT sweep long liq DULU sebelum dump lebih lanjut",
                "priority": -1102
            }
        
        # Mirror: funding positif + short_liq dekat + overbought = long squeeze
        if (funding_rate > 0.005 and
            short_dist < 2.5 and
            short_dist < long_dist and
            rsi6 > 55 and
            rsi6_5m > 35 and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Funding positive long squeeze: funding={funding_rate:.4f} (crowded long), short liq {short_dist:.2f}% SANGAT dekat → HFT sweep short liq DULU",
                "priority": -1102
            }
        
        return {"override": False}


class ProfitImbalanceReversal:
    """
    🔥 EXCHANGE NEUTRALIZATION LOGIC (Dosen's Insight)
    
    Binance tidak peduli arah, tapi peduli agar tidak ada side yang terlalu menang.
    Jika satu sisi (misal long) sudah hampir mati (long liq kecil, harga turun, RSI oversold),
    maka exchange akan reverse untuk membunuh short yang sedang profit.
    
    Kondisi LONG (setelah dump):
    - change_5m < -2.5%   (sudah turun signifikan)
    - rsi6 < 25           (oversold)
    - long_liq < 2.0%     (long tinggal sedikit → hampir habis)
    - volume_ratio < 0.8  (tidak ada panic volume, market tipis → mudah dibalik)
    
    Kondisi SHORT (setelah pump):
    - change_5m > 2.5%    (sudah naik signifikan)
    - rsi6 > 75           (overbought)
    - short_liq < 2.0%    (short tinggal sedikit)
    - volume_ratio < 0.8
    
    Priority: -1105 (TERTINGGI, di atas semua detector lain)
    Karena ini adalah kebijakan exchange level tertinggi.
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float,
               long_liq: float, short_liq: float,
               volume_ratio: float) -> Dict:
        
        # Kasus A: Market sudah dump, long hampir mati → exchange akan pump (LONG)
        if (change_5m < -2.5 and
            rsi6 < 25 and
            long_liq < 2.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"PROFIT IMBALANCE REVERSAL (Exchange Neutralization): "
                    f"price dumped {change_5m:.1f}%, RSI {rsi6:.1f} oversold, "
                    f"long liq {long_liq:.2f}% hampir habis → long side sudah mati, "
                    f"exchange akan reverse untuk bunuh short yang profit → LONG"
                ),
                "priority": -1105
            }
        
        # Kasus B: Market sudah pump, short hampir mati → exchange akan dump (SHORT)
        if (change_5m > 2.5 and
            rsi6 > 75 and
            short_liq < 2.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"PROFIT IMBALANCE REVERSAL (Exchange Neutralization): "
                    f"price pumped {change_5m:.1f}%, RSI {rsi6:.1f} overbought, "
                    f"short liq {short_liq:.2f}% hampir habis → short side sudah mati, "
                    f"exchange akan reverse untuk bunuh long yang profit → SHORT"
                ),
                "priority": -1105
            }
        
        return {"override": False}


class ProximityContinuationOverride:
    """
    🔥 DUSDT PATTERN: Harga sudah bergerak searah dengan target likuiditas dekat,
    volume kering, tidak ada resistance → HFT lanjutkan arah.
    
    Kondisi LONG:
    - short_liq < 2.5% (target dekat)
    - change_5m > 1.0% (harga naik)
    - volume_ratio < 0.8 (volume kering)
    - down_energy < 0.1 (tidak ada seller)
    - agg > 0.6 atau ofi_bias == "LONG" (konfirmasi order flow)
    
    Kondisi SHORT:
    - long_liq < 2.5% (target dekat)
    - change_5m < -1.0% (harga turun)
    - volume_ratio < 0.8 (volume kering)
    - up_energy < 0.1 (tidak ada buyer)
    - agg < 0.4 atau ofi_bias == "SHORT" (konfirmasi order flow)
    
    Priority: -1105 (sama dengan ProfitImbalanceReversal)
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, change_5m: float,
               volume_ratio: float, down_energy: float, up_energy: float,
               agg: float, ofi_bias: str) -> Dict:
        # CASE LONG: harga naik, short liq dekat, no sellers
        if (short_liq < 2.5 and change_5m > 1.0 and volume_ratio < 0.8 and down_energy < 0.1):
            if agg > 0.6 or ofi_bias == "LONG":
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"PROXIMITY CONTINUATION: price up {change_5m:.1f}%, short liq {short_liq:.2f}% close, volume dry, no sellers → HFT sweep short stops, continue LONG",
                    "priority": -1105
                }
        # CASE SHORT: harga turun, long liq dekat, no buyers
        if (long_liq < 2.5 and change_5m < -1.0 and volume_ratio < 0.8 and up_energy < 0.1):
            if agg < 0.4 or ofi_bias == "SHORT":
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"PROXIMITY CONTINUATION: price down {change_5m:.1f}%, long liq {long_liq:.2f}% close, volume dry, no buyers → HFT sweep long stops, continue SHORT",
                    "priority": -1105
                }
        return {"override": False}


class OversoldLongLiqBounceReversal:
    """
    🔥 PLAYUSDT PATTERN: Oversold + long liq sangat dekat + harga sudah turun
    → HFT akan pump untuk reversal (bukan lanjut dump)
    
    Kondisi:
    - long_liq < 2.0% (sangat dekat)
    - rsi6 < 30 (oversold)
    - change_5m < -2.0% (sudah turun signifikan)
    - volume_ratio < 1.0 (exhaustion, tidak ada panic volume)
    - down_energy < 0.1 (tidak ada seller aktif)
    
    Priority: -1104 (di atas VEGA-KILL CONFLICT -9991.5)
    Bias: LONG
    """
    @staticmethod
    def detect(long_liq: float, rsi6: float, change_5m: float,
               volume_ratio: float, down_energy: float) -> Dict:
        if (long_liq < 2.0 and
            rsi6 < 30 and
            change_5m < -2.0 and
            volume_ratio < 1.0 and
            down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OVERSOLD LONG LIQ BOUNCE REVERSAL: long liq {long_liq:.2f}% super close, RSI {rsi6:.1f} oversold, price dropped {change_5m:.1f}%, volume dry ({volume_ratio:.2f}x), no sellers → HFT will pump for reversal",
                "priority": -1104
            }
        return {"override": False}


class OverboughtShortLiqDumpReversal:
    """
    Mirror: Overbought + short liq sangat dekat + harga sudah naik
    → HFT akan dump untuk reversal
    
    Kondisi:
    - short_liq < 2.0% (sangat dekat)
    - rsi6 > 70 (overbought)
    - change_5m > 2.0% (sudah naik signifikan)
    - volume_ratio < 1.0 (exhaustion, tidak ada panic volume)
    - up_energy < 0.1 (tidak ada buyer aktif)
    
    Priority: -1104 (di atas VEGA-KILL CONFLICT -9991.5)
    Bias: SHORT
    """
    @staticmethod
    def detect(short_liq: float, rsi6: float, change_5m: float,
               volume_ratio: float, up_energy: float) -> Dict:
        if (short_liq < 2.0 and
            rsi6 > 70 and
            change_5m > 2.0 and
            volume_ratio < 1.0 and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OVERBOUGHT SHORT LIQ DUMP REVERSAL: short liq {short_liq:.2f}% super close, RSI {rsi6:.1f} overbought, price pumped {change_5m:.1f}%, volume dry → HFT will dump for reversal",
                "priority": -1104
            }
        return {"override": False}


class AskWallLongTrap:
    """
    🔥 PLAYUSDT EXACT PATTERN:
    
    HFT pasang ask wall raksasa (ask_slope >> bid_slope) tapi bikin
    semua trades BUY (agg=1.00) untuk memancing long entry.
    
    Tanda:
    - ask_slope >> bid_slope (rasio > 5x) = barrier naik sangat tebal
    - agg > 0.85 (hampir semua trades buy = spoofing buy pressure)  
    - RSI 1m overbought tapi RSI 5m sangat oversold = divergence palsu
    - change_5m kecil meski agg=1.00 = harga tidak bisa nembus ask wall
    
    Logika: jika 100% trades BUY tapi harga hanya naik 1-2% = ask wall
    menyerap semua buy → setelah retail terisi → HFT tarik ask wall → dump
    
    Priority: -1099 (sangat tinggi)
    """
    @staticmethod
    def detect(ask_slope: float, bid_slope: float,
               agg: float, change_5m: float,
               rsi6: float, rsi6_5m: float,
               volume_ratio: float,
               down_energy: float) -> Dict:
        
        if bid_slope <= 0:
            return {"override": False}
        
        ask_bid_ratio = ask_slope / bid_slope
        
        # Core pattern: ask wall raksasa + agg tinggi palsu + price tidak naik proporsional
        if (ask_bid_ratio > 5.0 and           # sell wall 5x lebih tebal
            agg > 0.80 and                     # hampir semua trades BUY
            0 < change_5m < 3.0 and           # harga naik tipis (tidak proporsional)
            volume_ratio < 0.6 and            # volume rendah (tidak ada momentum nyata)
            down_energy < 0.1):               # tidak ada seller di book (tapi ask wall ada)
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"ASK WALL LONG TRAP: ask_slope={ask_slope:.0f} vs bid={bid_slope:.0f} "
                    f"(ratio {ask_bid_ratio:.1f}x), agg={agg:.2f} (spoof buy), "
                    f"price only +{change_5m:.1f}% → ask wall absorbing all buys, "
                    f"retail trapped LONG, dump incoming"
                ),
                "priority": -1099
            }
        
        return {"override": False}


class RSIMultiTFDivergenceTrap:
    """
    🔥 PLAYUSDT: RSI 1m overbought + RSI 5m oversold ekstrem
    
    Sistem sekarang interpret ini sebagai "bounce akan terjadi karena 5m oversold".
    Padahal ini adalah JEBAKAN:
    - RSI 1m = 77 (overbought = retail sudah beli di top)
    - RSI 5m = 18 (oversold = trend turun jangka menengah masih kuat)
    
    Ketika RSI 1m overbought DAN RSI 5m masih sangat oversold:
    → trend 5m lebih dominan
    → bounce 1m hanya fake recovery
    → arah sebenarnya = lanjut turun sesuai trend 5m
    
    Priority: -1098
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float,
               ask_slope: float, bid_slope: float,
               change_5m: float, volume_ratio: float,
               obv_trend: str) -> Dict:
        
        # RSI 1m overbought tapi RSI 5m masih oversold ekstrem
        # = bounce palsu, trend 5m lebih kuat
        rsi_divergence = rsi6 > 70 and rsi6_5m < 30
        
        if not rsi_divergence:
            return {"override": False}
        
        # Konfirmasi: ask wall lebih tebal (supply lebih besar)
        ask_dominant = (bid_slope > 0 and ask_slope / bid_slope > 3.0)
        
        # Konfirmasi: harga sudah naik tipis dengan volume rendah
        # = momentum palsu, bukan breakout nyata
        fake_pump = (0 < change_5m < 3.0 and volume_ratio < 0.6)
        
        if rsi_divergence and (ask_dominant or fake_pump):
            bias = "SHORT"  # ikuti trend 5m yang lebih kuat
            return {
                "override": True,
                "bias": bias,
                "reason": (
                    f"RSI MULTI-TF DIVERGENCE TRAP: RSI_1m={rsi6:.1f} overbought "
                    f"tapi RSI_5m={rsi6_5m:.1f} oversold ekstrem → "
                    f"bounce 1m adalah fake, trend 5m lebih dominan, "
                    f"ask/bid ratio={ask_slope/bid_slope:.1f}x → SHORT"
                ),
                "priority": -1098
            }
        
        return {"override": False}


class OFISpoofingDetector:
    """
    🔥 RLSUSDT PATTERN: OFI SHORT 1.00 tapi sebenarnya LONG
    
    HFT teknik spoofing:
    - Pasang order sell besar di order book → OFI bias SHORT
    - Tapi 90%+ trades sebenarnya BUY (agg tinggi)
    - Ini artinya: wall sell besar = inducement untuk short retail
    - Setelah short terisi → HFT tarik order → PUMP
    
    Deteksi: OFI SHORT kuat TAPI agg > 0.6 (mayoritas trades BUY)
    = OFI sedang di-spoof, percaya agg bukan OFI
    
    Priority: -1098.5 (antara BlowOffTop dan AbsoluteAgg)
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, agg: float,
               up_energy: float, down_energy: float,
               short_dist: float, long_dist: float,
               volume_ratio: float) -> Dict:
        
        # OFI SHORT tapi agg tinggi (trades mayoritas BUY) = spoof
        if (ofi_bias == "SHORT" and
            ofi_strength > 0.7 and
            agg > 0.6 and                    # mayoritas trades BUY
            up_energy > 0 and
            down_energy < 0.01 and           # tidak ada seller nyata
            volume_ratio < 0.7):             # volume rendah = lebih mudah dispoof
            
            # Tentukan arah dari liquidity
            if short_dist < long_dist and short_dist < 5.0:
                target = "LONG"
                reason_liq = f"short liq {short_dist:.2f}% lebih dekat"
            else:
                # Default LONG karena agg menunjukkan buy pressure nyata
                target = "LONG"
                reason_liq = "agg menunjukkan buy pressure nyata"
            
            return {
                "override": True,
                "bias": target,
                "reason": f"OFI SPOOFING detected: OFI SHORT {ofi_strength:.2f} tapi agg={agg:.2f} (trades mayoritas BUY), down_energy=0 → HFT induced short trap, {reason_liq} → forced {target}",
                "priority": -1099
            }
        
        # Mirror: OFI LONG tapi agg rendah = spoof untuk induce LONG
        if (ofi_bias == "LONG" and
            ofi_strength > 0.7 and
            agg < 0.4 and                    # mayoritas trades SELL
            down_energy > 0 and
            up_energy < 0.01 and
            volume_ratio < 0.7):
            
            if long_dist < short_dist and long_dist < 5.0:
                target = "SHORT"
                reason_liq = f"long liq {long_dist:.2f}% lebih dekat"
            else:
                target = "SHORT"
                reason_liq = "agg menunjukkan sell pressure nyata"
            
            return {
                "override": True,
                "bias": target,
                "reason": f"OFI SPOOFING detected: OFI LONG {ofi_strength:.2f} tapi agg={agg:.2f} (trades mayoritas SELL), up_energy=0 → HFT induced long trap, {reason_liq} → forced {target}",
                "priority": -1099
            }
        
        return {"override": False}


# ================================================================
# 🔥 LECTURER'S FINAL RESOLVERS (PRIORITY TERTINGGI)
# ================================================================

class ExtremeFundingObvConsensus:
    """
    Detector: Extreme Funding + OBV Consensus (Priority -10002)
    
    Kondisi:
    - funding_rate < -0.005 (funding negatif ekstrem = crowded short)
    - obv_trend in ("NEGATIVE_EXTREME", "NEGATIVE") (OBV konfirmasi bearish)
    - change_5m > 2.0 (harga sudah pump signifikan)
    - volume_ratio < 0.8 (volume rendah = distribusi diam-diam)
    
    Priority: -10002 (lebih tinggi dari CrowdedDirectionLiquidityResolver)
    Bias: SHORT
    
    Logika: Funding negatif ekstrem + OBV negatif + harga pump = distribution trap.
    Smart money sedang distribute ke retail yang FOMO long, dump incoming.
    """
    @staticmethod
    def detect(funding_rate: float, obv_trend: str, change_5m: float,
               volume_ratio: float) -> Dict:
        if funding_rate is None:
            return {"override": False}
        if (funding_rate < -0.005 and 
            obv_trend in ("NEGATIVE_EXTREME", "NEGATIVE") and
            change_5m > 2.0 and 
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"EXTREME FUNDING + OBV NEGATIVE: funding={funding_rate:.5f} (crowded short), OBV={obv_trend}, price up {change_5m:.1f}% low vol → distribution trap, dump incoming",
                "priority": -10002
            }
        return {"override": False}


class AskWallSpoofedOFI:
    """
    🔥 SPOOFED OFI + AGG DENGAN ASK WALL RAKSASA (Pola PLAYUSDT & RLSUSDT)
    
    Apa yang terjadi:
    - OFI = 1.00 (100% buy order flow) dan agg = 1.00 (100% trades buy) → sistem pikir bullish kuat.
    - Tapi ask_slope >> bid_slope (ask wall 5-10x lebih tebal) → harga tidak bisa naik.
    - HFT memancing entry LONG dengan micro-trades, lalu menjebak dengan sell wall.
    
    Data bukti (dari file Anda):
    "agg": 1.0, "ofi_bias": "LONG", "ofi_strength": 1.0,
    "ask_slope": 239416867460.3, "bid_slope": 188807008333.32,  // ask/bid > 1.2x
    "change_5m": -1.74  // harga TURUN meski 100% buy trades
    
    Priority: -9999.5
    Bias: SHORT
    """
    @staticmethod
    def detect(ofi_bias: str, agg: float, ask_slope: float, bid_slope: float,
               change_5m: float, volume_ratio: float) -> Dict:
        if bid_slope <= 0:
            return {"override": False}
        
        if (ofi_bias == "LONG" and agg > 0.85 and 
            ask_slope > bid_slope * 1.5 and 
            change_5m < 0 and volume_ratio < 0.7):
            ratio = ask_slope / bid_slope
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"SPOOFED OFI: OFI LONG {ofi_bias}, agg={agg:.2f} tapi ask_slope={ask_slope:.0f} > bid_slope={bid_slope:.0f} (ratio {ratio:.1f}x), price down {change_5m:.1f}% → HFT jebak LONG",
                "priority": -9999.5
            }
        return {"override": False}


class BlowOffTopNoOBV:
    """
    🔥 POST-PUMP DISTRIBUTION DENGAN VOLUME KERING (Pola CUSDT & BULLAUSDT)
    
    Apa yang terjadi:
    - Harga sudah pump >5% dalam 5 menit.
    - Volume turun drastis (<0.6x) → tidak ada pembeli baru.
    - RSI 5m > 85, RSI 1m > 90 → blow-off top.
    - Sistem sering salah menginterpretasi sebagai "squeeze continuation".
    
    Data bukti (dari file BULLAUSDT):
    "change_5m": 6.67, "volume_ratio": 0.45, "rsi6": 89.0, "rsi6_5m": 47.2
    // RSI 1m 89 (overbought), tapi RSI 5m 47 (netral) → divergence
    "bias": "LONG", "greeks_kill_direction": "LONG"  // sistem bilang LONG
    // TAPI user bilang "ini dia malah short 8% kebawah" → error
    
    Priority: -10004 (LEBIH TINGGI dari PostPumpDistribution)
    Bias: SHORT
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, rsi6_5m: float, short_dist: float) -> Dict:
        if change_5m > 5.0 and volume_ratio < 0.6 and rsi6_5m > 80:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"BLOW-OFF TOP (no OBV): pump {change_5m:.1f}%, volume {volume_ratio:.2f}x, RSI5m={rsi6_5m:.1f} overbought → force SHORT",
                "priority": -10004
            }
        return {"override": False}


class PostPumpDistribution:
    """
    Detector: Post-Pump Distribution (Priority -10003)
    
    Kondisi:
    - change_5m > 2.5 (pump signifikan)
    - volume_ratio < 0.7 (volume rendah = tidak ada conviction)
    - obv_trend in ("NEGATIVE_EXTREME", "NEGATIVE") (OBV divergensi negatif)
    - funding_rate < -0.003 (funding negatif = shorts masih dominan)
    
    Priority: -10003 (PALING TINGGI di priority ladder baru)
    Bias: SHORT
    
    Logika: Pump dengan volume rendah + OBV negatif + funding negatif = 
    smart money distribute positions ke retail FOMO. Dump imminent.
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, obv_trend: str,
               funding_rate: float) -> Dict:
        if (change_5m > 2.5 and volume_ratio < 0.7 and 
            obv_trend in ("NEGATIVE_EXTREME", "NEGATIVE") and
            funding_rate is not None and funding_rate < -0.003):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"POST-PUMP DISTRIBUTION: pumped {change_5m:.1f}% with low vol {volume_ratio:.2f}x, OBV {obv_trend}, funding {funding_rate:.5f} → dump imminent",
                "priority": -10003
            }
        return {"override": False}


class VegaActiveShortOverride:
    """
    🔥 PRIORITY -10002: Ketika Vega aktif (BAIT phase), volume rendah,
    harga sudah pump >2%, short liq dekat sebagai umpan → paksa SHORT
    (karena HFT akan fade the move)
    """
    @staticmethod
    def detect(vega_active: bool, volume_ratio: float, change_5m: float,
               short_liq: float, obv_trend: str, agg: float) -> Dict:
        if (vega_active and
            volume_ratio < 0.7 and
            change_5m > 2.0 and
            short_liq < 2.0 and
            obv_trend != "POSITIVE_EXTREME"):  # OBV positif ekstrem kadang juga palsu, tapi kita biarkan dulu
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"VEGA ACTIVE SHORT OVERRIDE: Vega aktif (BAIT phase), volume {volume_ratio:.2f}x rendah, price up {change_5m:.1f}%, short liq {short_liq:.2f}% → HFT fade to SHORT",
                "priority": -10002
            }
        return {"override": False}


class RSI100AbsoluteReversal:
    """
    🔥 PRIORITY -1104.5: RSI6 = 100 atau RSI5m = 100
    Tidak peduli sinyal squeeze apapun, ini adalah blow-off top absolut.
    Harga tidak bisa naik lagi dalam jangka pendek, pasti reversal.
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, change_5m: float, 
               short_liq: float, long_liq: float, volume_ratio: float) -> Dict:
        if (rsi6 >= 99.5 or rsi6_5m >= 99.5) and change_5m > 2.0:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"RSI100 ABSOLUTE REVERSAL: RSI6={rsi6:.1f} RSI5m={rsi6_5m:.1f} max overbought, price pumped {change_5m:.1f}% → no room for squeeze, immediate dump",
                "priority": -1104.5
            }
        return {"override": False}


class AggOFIBearishOverride:
    """
    🔥 PRIORITY -1104.5: Ketika agg < 0.3 (majoritas sell trades), OFI SHORT kuat,
    short_liq dekat (<2%) sebagai umpan, dan harga naik (pump tipis)
    → HFT sedang distribusi, paksa SHORT
    """
    @staticmethod
    def detect(agg: float, ofi_bias: str, ofi_strength: float,
               short_liq: float, change_5m: float) -> Dict:
        if (agg < 0.3 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.7 and
            short_liq < 2.0 and
            change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"AGG-OFI BEARISH OVERRIDE: agg={agg:.2f} (87%+ sell trades), OFI SHORT {ofi_strength:.2f}, short_liq={short_liq:.2f}% umpan → HFT distribusi, dump incoming",
                "priority": -1104.5
            }
        return {"override": False}


class OverboughtSqueezeGuard:
    """
    Guard untuk ExtremeShortLiqSqueeze: jika RSI terlalu tinggi (>95),
    short squeeze tidak akan terjadi karena tidak ada buyer baru.
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, short_liq: float, change_5m: float) -> Dict:
        if short_liq < 1.0 and (rsi6 > 95 or rsi6_5m > 90) and change_5m > 2.0:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OVERBOUGHT SQUEEZE GUARD: short_liq={short_liq:.2f}% close tapi RSI6={rsi6:.1f} RSI5m={rsi6_5m:.1f} extreme overbought → squeeze fake, reversal imminent",
                "priority": -1103.5
            }
        return {"override": False}


class FundingCrowdedOverride:
    """
    🔥 PRIORITY -1104.6: Koreksi delta_crowded berdasarkan funding rate
    Funding positif = crowded LONG (banyak posisi long)
    Funding negatif = crowded SHORT (banyak posisi short)
    Jika funding positif dan short_liq dekat, HFT akan dump untuk likuidasi LONG, bukan pump.
    """
    @staticmethod
    def detect(funding_rate, short_liq, long_liq, change_5m, rsi6):
        if funding_rate is None:
            return {"override": False}
        # Funding positif (crowded long) + short liq dekat = HFT akan dump
        if funding_rate > 0.0002 and short_liq < 2.0 and short_liq < long_liq:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"FUNDING CROWDED OVERRIDE: funding={funding_rate:.5f} (crowded LONG), short_liq={short_liq:.2f}% dekat → HFT akan DUMP untuk likuidasi LONG, bukan pump",
                "priority": -1104.6
            }
        # Funding negatif (crowded short) + long liq dekat = HFT akan pump
        if funding_rate < -0.0002 and long_liq < 2.0 and long_liq < short_liq:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"FUNDING CROWDED OVERRIDE: funding={funding_rate:.5f} (crowded SHORT), long_liq={long_liq:.2f}% dekat → HFT akan PUMP",
                "priority": -1104.6
            }
        return {"override": False}


class PumpFakeShortLiqTrap:
    """
    🔥 PRIORITY -1104.8: Deteksi pump palsu dengan short liq dekat sebagai umpan
    Kondisi: short_liq < 1.5%, change_5m > 2%, rsi6 > 70, volume_ratio < 0.7, agg < 0.45
    HFT membuat short liq dekat untuk memancing LONG, lalu dump.
    """
    @staticmethod
    def detect(short_liq: float, change_5m: float, rsi6: float, volume_ratio: float, agg: float) -> Dict:
        if (short_liq < 1.5 and change_5m > 2.0 and rsi6 > 70 and volume_ratio < 0.7 and agg < 0.45):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"PUMP FAKE SHORT LIQ TRAP: short_liq={short_liq:.2f}% dekat, price up {change_5m:.1f}%, RSI={rsi6:.1f} overbought, volume {volume_ratio:.2f}x rendah, agg={agg:.2f} sell dominant → HFT jebak LONG, dump imminent",
                "priority": -1104.8
            }
        return {"override": False}


class BaitPhaseShortLiqTrap:
    """
    🔥 PRIORITY -1103.8: Deteksi jebakan LONG di BAIT phase
    Kondisi: BAIT phase + short_liq super dekat (<1%) + RSI overbought (>70) + funding positif
    = HFT memancing LONG dengan ilusi squeeze, lalu dump
    """
    @staticmethod
    def detect(market_phase, short_liq, rsi6, funding_rate, change_5m, down_energy):
        if funding_rate is None:
            return {"override": False}
        if (market_phase == "BAIT" and 
            short_liq < 1.0 and 
            rsi6 > 70 and 
            funding_rate > 0.0002 and
            change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"BAIT PHASE SHORT LIQ TRAP: short_liq={short_liq:.2f}% super dekat, RSI={rsi6:.1f} overbought, funding={funding_rate:.5f} (crowded long) → HFT jebak LONG, dump imminent",
                "priority": -1103.8
            }
        return {"override": False}


class PresweepMisinterpretationGuard:
    """
    🔥 Deteksi: long_liq sangat dekat TAPI OFI/agg sangat bullish
    = HFT bukan mau dump, tapi mau pump setelah sweep long liq tipis

    Kondisi: long_liq < 0.3% + agg > 0.75 + ofi_bias == "LONG" 
    → BLOCK semua SHORT signal, paksa ke LONG
    Priority: -10001.5 (antara CrowdedResolver dan DualLiqFirstMove)
    """
    @staticmethod
    def detect(long_liq: float, short_liq: float, agg: float,
               ofi_bias: str, ofi_strength: float,
               funding_rate: float, rsi6: float, change_5m: float) -> dict:

        # Guard 1: long_liq sangat tipis tapi order flow bullish
        # Ini bukan setup dump, tapi "micro-dip sweep lalu pump"
        if (long_liq < 0.3 and
            long_liq < short_liq * 5 and  # short liq jauh lebih besar
            agg > 0.75 and
            ofi_bias == "LONG" and
            ofi_strength > 0.6):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"PRESWEEP MISINTERPRETATION GUARD: long_liq={long_liq:.2f}% "
                    f"sangat dekat tapi agg={agg:.2f} + OFI LONG {ofi_strength:.2f} "
                    f"→ HFT akan micro-dip sweep long liq LALU pump ke short_liq={short_liq:.2f}%"
                ),
                "priority": -10001.5
            }

        # Guard 2: Dual liq trap tapi funding sangat negatif
        # Funding negatif = semua orang short → HFT akan squeeze short, bukan dump
        if (funding_rate is not None and
            funding_rate < -0.003 and
            short_liq < long_liq * 2 and  # short liq lebih dekat atau sebanding
            agg > 0.7):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"PRESWEEP FUNDING SQUEEZE GUARD: funding={funding_rate:.5f} "
                    f"(crowded SHORT), agg={agg:.2f} bullish "
                    f"→ HFT squeeze shorts, BUKAN dump"
                ),
                "priority": -10001.5
            }

        return {"override": False}


class FundingExtremeDualTrapOverride:
    """
    🔥 PRIORITY -10001.5: Ketika funding rate ekstrem dan dual_liq_trap aktif,
    ikuti greeks_kill_direction (bukan first_move_direction dari liquidity proximity)
    
    Kondisi:
    - dual_liq_trap aktif (trap_score >= 3)
    - abs(funding_rate) > 0.003 (funding ekstrem, crowded)
    - greeks_kill_direction in ("LONG", "SHORT")
    - volume_ratio < 0.8 (market tipis)
    
    Karena funding ekstrem menunjukkan crowding yang sudah pasti, HFT akan eksekusi
    kill_direction, bukan first_move.
    """
    @staticmethod
    def detect(dual_liq_trap: bool, trap_score: int,
               funding_rate: float, greeks_kill_direction: str,
               volume_ratio: float) -> Dict:
        if (dual_liq_trap and trap_score >= 3 and
            funding_rate is not None and abs(funding_rate) > 0.003 and
            greeks_kill_direction in ("LONG", "SHORT") and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": greeks_kill_direction,
                "reason": f"FUNDING EXTREME DUAL TRAP: funding={funding_rate:.5f} (crowded), dual trap score={trap_score}, kill_direction={greeks_kill_direction} → ikuti kill direction, abaikan first_move",
                "priority": -10001.5
            }
        return {"override": False}


class CapitulationTrapGuard:
    """
    🔥 PRIORITY -1104.95: Deteksi jebakan capitulation bottom palsu
    Kondisi: RSI6 < 15, long_liq < 2.0%, change_5m < -2.5%, OBV NEGATIVE_EXTREME, volume_ratio < 0.7
    HFT membuat long liq dekat untuk memancing LONG, tapi distribusi masih aktif → dump lanjut
    """
    @staticmethod
    def detect(rsi6: float, long_liq: float, change_5m: float,
               obv_trend: str, volume_ratio: float) -> Dict:
        if (rsi6 < 15 and long_liq < 2.0 and change_5m < -2.5 and
            obv_trend == "NEGATIVE_EXTREME" and volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"CAPITULATION TRAP GUARD: RSI6={rsi6:.1f} capitulation, long_liq={long_liq:.2f}% dekat, price down {change_5m:.1f}%, OBV {obv_trend}, volume {volume_ratio:.2f}x kering → distribusi masih aktif, dump lanjut, force SHORT",
                "priority": -1104.95
            }
        return {"override": False}


class GreeksShortTrapOverride:
    """
    🔥 PRIORITY -9996: Deteksi jebakan Greeks DELTA: SHORT_TRADERS_DIE tapi kondisi bearish
    Kondisi: greeks_liq_7pct == "SHORT_TRADERS_DIE", agg < 0.4, ofi_bias == "SHORT",
    volume_ratio < 0.8, rsi6 > 70, change_5m > 3% → HFT distribusi, paksa SHORT
    """
    @staticmethod
    def detect(greeks_liq_7pct: str, agg: float, ofi_bias: str,
               volume_ratio: float, rsi6: float, change_5m: float) -> Dict:
        if (greeks_liq_7pct == "SHORT_TRADERS_DIE" and
            agg < 0.4 and
            ofi_bias == "SHORT" and
            volume_ratio < 0.8 and
            rsi6 > 70 and
            change_5m > 3.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"GREEKS SHORT TRAP: greeks_liq_7pct={greeks_liq_7pct}, agg={agg:.2f} (sell dominant), OFI={ofi_bias}, volume {volume_ratio:.2f}x, RSI {rsi6:.1f} overbought, price up {change_5m:.1f}% → distribution trap, HFT akan dump, force SHORT",
                "priority": -9996
            }
        return {"override": False}


class CrowdedDirectionLiquidityResolver:
    """
    KUNCI: delta_crowded + liquidity proximity harus selalu dibaca bersama.
    
    delta_crowded = LONG artinya:
    - CASE A: long_liq < short_liq → longs yang crowded akan dieksekusi → SHORT
    - CASE B: short_liq < long_liq → longs masuk sebagai counter-trend,
              shorts yang sebenarnya crowded (mereka short karena RSI tinggi)
              → LONG (squeeze shorts)
    
    Priority: -10001 (TERTINGGI ABSOLUT)
    """
    @staticmethod
    def detect(delta_crowded: str, short_liq: float, long_liq: float,
               agg: float, ofi_bias: str, ofi_strength: float, volume_ratio: float,
               rsi6: float, change_5m: float,
               who_dies_first: str, greeks_kill_direction: str,
               funding_rate: float = 0.0, obv_trend: str = "NEUTRAL",
               vega_active: bool = False) -> Dict:
        
        # 🔥 FIX 2: GUARD - Jangan override jika Vega aktif (BAIT phase) dan volume rendah
        # Karena di BAIT phase, arah sebenarnya adalah fade (kebalikan dari move)
        if vega_active and volume_ratio < 0.7:
            return {"override": False}
        
        # GUARD: funding ekstrem negatif + OBV negatif → jangan override (biarkan detector lain)
        if funding_rate is not None and funding_rate < -0.005 and obv_trend in ("NEGATIVE_EXTREME", "NEGATIVE"):
            return {"override": False}
        
        if delta_crowded == "NEUTRAL":
            return {"override": False}
        
        liq_diff = abs(short_liq - long_liq)
        if liq_diff < 0.3:
            return {"override": False}  # terlalu simetris
        
        # CASE A: delta_crowded LONG + long_liq lebih dekat
        # = longs terlalu banyak, akan dieksekusi → SHORT
        if (delta_crowded == "LONG" and 
            long_liq < short_liq and 
            long_liq < 2.0):
            
            # 🔥 PRESWEEP EXCEPTION: Jika long_liq sangat tipis (<0.3%) DAN 
            # order flow bullish kuat → ini bukan setup dump
            # HFT akan micro-sweep long liq lalu pump ke short liq yang lebih besar
            if (long_liq < 0.3 and 
                agg > 0.75 and 
                ofi_bias == "LONG"):
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": (
                        f"CROWDED RESOLVER EXCEPTION: long_liq={long_liq:.2f}% sangat tipis "
                        f"+ agg={agg:.2f} + OFI LONG → bukan dump setup, HFT micro-sweep lalu pump"
                    ),
                    "priority": -10001
                }
            
            is_bait = (agg > 0.8 and ofi_bias == "LONG" and volume_ratio < 0.9)
            
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"CROWDED DIRECTION RESOLVER: delta_crowded=LONG + long_liq={long_liq:.2f}% < short_liq={short_liq:.2f}% "
                    f"→ longs akan dieksekusi, bait_active={is_bait} "
                    f"(agg={agg:.2f}, OFI={ofi_bias}) → SHORT"
                ),
                "priority": -10001
            }
        
        # CASE B: delta_crowded LONG + short_liq lebih dekat
        # = shorts crowded (retail short karena RSI tinggi/overbought)
        # longs masuk sebagai counter → HFT akan squeeze shorts → LONG
        if (delta_crowded == "LONG" and
            short_liq < long_liq and
            short_liq < 2.0):
            
            # 🔥 BEARISH CONFLUENCE GUARD
            if agg < 0.3 and ofi_bias == "SHORT" and ofi_strength > 0.6:
                # Jangan paksa LONG, biarkan sinyal lain (misal Vega fade) yang menentukan
                return {"override": False}
            
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"CROWDED DIRECTION RESOLVER: delta_crowded=LONG + short_liq={short_liq:.2f}% < long_liq={long_liq:.2f}% "
                    f"→ shorts crowded (overbought trap), HFT akan squeeze → LONG"
                ),
                "priority": -10001
            }
        
        # Mirror untuk delta_crowded SHORT
        if (delta_crowded == "SHORT" and
            short_liq < long_liq and
            short_liq < 2.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"CROWDED DIRECTION RESOLVER: delta_crowded=SHORT + short_liq={short_liq:.2f}% closer "
                    f"→ shorts akan dieksekusi → LONG"
                ),
                "priority": -10001
            }
        
        if (delta_crowded == "SHORT" and
            long_liq < short_liq and
            long_liq < 2.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"CROWDED DIRECTION RESOLVER: delta_crowded=SHORT + long_liq={long_liq:.2f}% closer "
                    f"→ longs crowded (oversold trap), HFT akan dump → SHORT"
                ),
                "priority": -10001
            }
        
        return {"override": False}


class AggSpoofingWithLiquidityContext:
    """
    agg=1.00 bisa berarti DUA hal yang berlawanan:
    
    BULLISH (squeeze): agg=1.00 + short_liq dekat + down_energy=0
    BEARISH (bait):    agg=1.00 + long_liq dekat + OBV negatif/netral
    
    Sistem sekarang selalu interpret agg=1.00 sebagai LONG.
    Ini yang membunuh SYSUSDT prediction.
    
    Priority: -10000.5
    """
    @staticmethod
    def detect(agg: float, short_liq: float, long_liq: float,
               obv_trend: str, obv_value: float,
               volume_ratio: float, change_5m: float,
               down_energy: float, up_energy: float,
               funding_rate: float) -> Dict:
        
        if agg < 0.85:
            return {"override": False}
        
        # agg=1.00 BEARISH: long_liq dekat + OBV tidak confirm + volume rendah
        # = HFT inject microtrades BUY untuk tarik LONG masuk, sebelum dump
        if (long_liq < short_liq and
            long_liq < 2.0 and
            volume_ratio < 0.9 and
            obv_trend not in ["POSITIVE_EXTREME", "POSITIVE"] and
            change_5m < 0):
            
            return {
                "override": True,
                "bias": "SHORT",
                "reason": (
                    f"AGG SPOOFING WITH LIQ CONTEXT: agg={agg:.2f} (100% buy microtrades) "
                    f"TAPI long_liq={long_liq:.2f}% < short_liq={short_liq:.2f}%, "
                    f"OBV={obv_trend}, change={change_5m:.1f}% → "
                    f"HFT inject buy trades untuk jebak LONG sebelum dump"
                ),
                "priority": -10000.5
            }
        
        # agg=1.00 BULLISH: short_liq dekat + down_energy=0 + harga tidak turun
        if (short_liq < long_liq and
            short_liq < 2.0 and
            down_energy < 0.01 and
            change_5m >= -1.0):
            
            return {
                "override": True,
                "bias": "LONG",
                "reason": (
                    f"AGG REAL SQUEEZE: agg={agg:.2f} genuine buy pressure, "
                    f"short_liq={short_liq:.2f}% dekat, no sellers → LONG"
                ),
                "priority": -10000.5
            }
        
        return {"override": False}


class DualLiqFirstMoveFollower:
    """
    Ketika dual_liq_trap aktif, sistem sudah tahu first_move_direction.
    Tapi sekarang sistem mengabaikannya karena block entry.
    
    Fix: jika dual_liq_trap aktif DAN first_move_direction clear,
    gunakan itu sebagai bias (bukan NEUTRAL/BLOCK).
    
    SYSUSDT: first_move=DOWN → SHORT (benar!)
    AIOTUSDT: first_move=UP → LONG (benar!)
    
    Priority: -10000
    """
    @staticmethod
    def detect(dual_liq_trap: bool, trap_score: int,
               first_move_direction: str,
               short_liq: float, long_liq: float,
               who_dies_first: str,
               agg: float, volume_ratio: float,
               change_5m: float,
               funding_rate: float = 0.0,
               ofi_bias: str = "NEUTRAL") -> Dict:
        
        if not dual_liq_trap or trap_score < 3:
            return {"override": False}
        
        # 🔥 Jika funding sangat negatif (crowded short) dan agg bullish,
        # maka first_move seharusnya UP (squeeze short), bukan DOWN
        if (funding_rate is not None and 
            funding_rate < -0.003 and 
            agg > 0.65):
            # Override first_move_direction ke UP
            first_move_direction = "UP"
        
        if first_move_direction == "UNKNOWN":
            return {"override": False}
        
        # Validasi first_move dengan liq proximity
        liq_consistent = (
            (first_move_direction == "UP" and short_liq < long_liq) or
            (first_move_direction == "DOWN" and long_liq < short_liq)
        )
        
        if not liq_consistent:
            return {"override": False}
        
        closer_liq = min(short_liq, long_liq)
        if closer_liq > 2.0:
            return {"override": False}
        
        bias = "LONG" if first_move_direction == "UP" else "SHORT"
        
        return {
            "override": True,
            "bias": bias,
            "reason": (
                f"DUAL LIQ FIRST MOVE FOLLOWER: trap_score={trap_score}/5, "
                f"first_move={first_move_direction}, "
                f"short_liq={short_liq:.2f}% vs long_liq={long_liq:.2f}% "
                f"→ HFT akan gerak {first_move_direction} dulu, bias={bias}"
            ),
            "priority": -10000,
            "is_first_move": True
        }


class VegaKillConflictGuard:
    """
    AIOTUSDT bug: VEGA-KILL CONFLICT memaksa SHORT karena kill_direction=SHORT.
    Tapi who_dies_first = BOTH_POSSIBLE = direction belum committed.
    
    Rule: jika who_dies = BOTH_POSSIBLE, kill_direction tidak reliable.
    Ikuti first_move_direction dari dual_liq_trap sebagai gantinya.
    
    (Method ini akan dipanggil dari dalam _greeks_absolute_score)
    """
    @staticmethod
    def should_block_vega_kill_conflict(who_dies_first: str,
                                         gamma_executing: bool,
                                         kill_speed: float) -> bool:
        if who_dies_first == "BOTH_POSSIBLE" and not gamma_executing:
            return True
        if kill_speed < 1.0 and not gamma_executing:
            return True
        return False


class LiquidityAmbiguityResolver:
    """
    🔥 POLA LICIK HFT: Liquidity Ambiguity / Inducement Phase
    
    HFT sengaja membuat sinyal ambiguous:
    - OFI SHORT tapi agg tinggi (spoof sell wall)
    - Funding negatif tapi long_liq dekat (bikin orang takut long)
    - RSI overbought tapi short_liq dekat (bikin orang masuk short)
    
    Resolver: ketika ada konflik sinyal yang jelas,
    percaya LIQUIDITY PROXIMITY sebagai ground truth.
    
    Ground truth hierarchy:
    1. Liquidity yang LEBIH DEKAT = arah HFT (tidak bisa dispoof)
    2. down_energy/up_energy = tekanan nyata di book
    3. agg = bukti transaksi nyata (lebih sulit dispoof dari OFI)
    4. OFI = bisa dispoof oleh order besar yang ditarik
    5. Funding = lagging indicator, bukan real-time
    
    Priority: -1097.5 (antara OFISpoofing dan AbsoluteAgg)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               ofi_bias: str, agg: float,
               down_energy: float, up_energy: float,
               funding_rate: float,
               rsi6: float, rsi6_5m: float) -> Dict:
        
        if funding_rate is None:
            funding_rate = 0.0
        
        # Hitung skor konflik
        liq_direction = "LONG" if short_dist < long_dist else "SHORT"
        ofi_direction = ofi_bias if ofi_bias != "NEUTRAL" else liq_direction
        agg_direction = "LONG" if agg > 0.5 else "SHORT"
        energy_direction = "LONG" if up_energy >= down_energy else "SHORT"
        
        # Hitung consensus dari sinyal yang tidak bisa dispoof
        trusted_signals = [liq_direction, agg_direction, energy_direction]
        long_count = trusted_signals.count("LONG")
        short_count = trusted_signals.count("SHORT")
        
        # Jika OFI berlawanan dengan consensus trusted signals = inducement
        ofi_conflicts = (ofi_direction != liq_direction and 
                        long_count >= 2 and ofi_direction == "SHORT")
        ofi_conflicts_short = (ofi_direction != liq_direction and
                              short_count >= 2 and ofi_direction == "LONG")
        
        # Ambiguity: sinyal utama saling bertentangan
        is_ambiguous = (
            (short_dist < 5.0 or long_dist < 5.0) and  # ada liq target dekat
            abs(short_dist - long_dist) < 3.0 and        # kedua liq relatif dekat
            LiquidityAmbiguityResolver._volume_ratio_implied_low(agg, up_energy, down_energy)  # volume rendah
        )
        
        # Jika OFI spoof terdeteksi, ikuti trusted consensus
        if ofi_conflicts and long_count >= 2:
            closer_liq = min(short_dist, long_dist)
            if short_dist <= long_dist:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Liquidity ambiguity resolved: OFI SHORT tapi liq={short_dist:.2f}% closer, agg={agg:.2f}, energy={energy_direction} → trusted signals say LONG",
                    "priority": -1097
                }
        
        if ofi_conflicts_short and short_count >= 2:
            if long_dist <= short_dist:
                return {
                    "override": True,
                    "bias": "SHORT",
                    "reason": f"Liquidity ambiguity resolved: OFI LONG tapi liq={long_dist:.2f}% closer, agg={agg:.2f}, energy={energy_direction} → trusted signals say SHORT",
                    "priority": -1097
                }
        
        return {"override": False}
    
    @staticmethod
    def _volume_ratio_implied_low(agg, up_energy, down_energy):
        """Helper: apakah kondisi menunjukkan volume efektif rendah"""
        return down_energy < 0.1 or up_energy < 0.1


class AlgoTypeAnalyzer:
    @staticmethod
    def analyze(order_book: Dict, trades: List[Dict], price: float,
                short_dist: float, long_dist: float,
                up_energy: float, down_energy: float) -> Dict:
        if trades and len(trades) > 0:
            recent_buys = 0
            recent_sells = 0
            for t in trades[-100:]:
                is_sell = t.get('m', False) or t.get('isBuyerMaker', False)
                if is_sell:
                    recent_sells += 1
                else:
                    recent_buys += 1
            buy_ratio = safe_div(recent_buys, recent_buys + recent_sells, 0.5)
            impact_bias = (
                "LONG" if buy_ratio > 0.55
                else "SHORT" if buy_ratio < 0.45
                else "NEUTRAL"
            )
        else:
            impact_bias = "NEUTRAL"

        if order_book and order_book.get("asks") and order_book.get("bids"):
            best_bid = order_book["bids"][0][0]
            best_ask = order_book["asks"][0][0]
            spread = (best_ask - best_bid) / price * 100 if price > 0 else 0
            if spread < 0.02:
                bid_depth = sum(q for _, q in order_book["bids"][:10])
                ask_depth = sum(q for _, q in order_book["asks"][:10])
                if bid_depth > ask_depth:
                    cost_bias = "SHORT"
                elif ask_depth > bid_depth:
                    cost_bias = "LONG"
                else:
                    cost_bias = "NEUTRAL"
            else:
                cost_bias = "NEUTRAL"
        else:
            cost_bias = "NEUTRAL"

        if short_dist < long_dist and short_dist < 2.0:
            opp_bias = "LONG"
        elif long_dist < short_dist and long_dist < 2.0:
            opp_bias = "SHORT"
        else:
            target_price_up = price * (1 + TARGET_MOVE_PCT / 100)
            target_price_down = price * (1 - TARGET_MOVE_PCT / 100)
            cost_to_up = 0.0
            cost_to_down = 0.0
            if order_book:
                for ask_price, ask_qty in order_book.get("asks", []):
                    if ask_price >= target_price_up:
                        break
                    cost_to_up += ask_qty * (ask_price - price)
                for bid_price, bid_qty in reversed(order_book.get("bids", [])):
                    if bid_price <= target_price_down:
                        break
                    cost_to_down += bid_qty * (price - bid_price)
            opp_bias = (
                "LONG" if cost_to_up < cost_to_down
                else "SHORT" if cost_to_down < cost_to_up
                else "NEUTRAL"
            )

        scores = {"LONG": 0, "SHORT": 0}
        for bias in [impact_bias, cost_bias, opp_bias]:
            if bias == "LONG":
                scores["LONG"] += 1
            elif bias == "SHORT":
                scores["SHORT"] += 1

        if scores["LONG"] > scores["SHORT"]:
            final_bias = "LONG"
            confidence = "HIGH"
        elif scores["SHORT"] > scores["LONG"]:
            final_bias = "SHORT"
            confidence = "HIGH"
        else:
            final_bias = "LONG" if up_energy < down_energy else "SHORT"
            confidence = "MEDIUM"

        return {"bias": final_bias, "confidence": confidence, "reason": "Algo Type Analysis"}


class HFT6PercentDirection:
    @staticmethod
    def determine(price: float, short_dist: float, long_dist: float,
                  up_energy: float, down_energy: float, oi_delta: float,
                  agg: float, flow: float) -> Dict:
        if short_dist > 5.0 and long_dist > 5.0:
            if up_energy > 0 and down_energy < 0.01 and agg > 0.6:
                return {
                    "bias": "LONG",
                    "reason": (
                        f"Both liq too far (short={short_dist:.1f}%, long={long_dist:.1f}%), "
                        f"energy+agg: up_energy={up_energy:.2f}, agg={agg:.2f} → LONG"
                    ),
                    "confidence": "MEDIUM"
                }
            if down_energy > 0 and up_energy < 0.01 and agg < 0.4:
                return {
                    "bias": "SHORT",
                    "reason": (
                        f"Both liq too far (short={short_dist:.1f}%, long={long_dist:.1f}%), "
                        f"energy+agg: down_energy={down_energy:.2f}, agg={agg:.2f} → SHORT"
                    ),
                    "confidence": "MEDIUM"
                }
            return {
                "bias": "NEUTRAL",
                "reason": f"Both liq too far ({short_dist:.1f}%/{long_dist:.1f}%), no strong signal",
                "confidence": "LOW"
            }

        if short_dist < 1.0 and short_dist < long_dist:
            primary = "LONG"
            reason = f"Short liq sangat dekat (+{short_dist}%) → Priority Squeeze"
            if down_energy > up_energy * 5:
                primary = "SHORT"
                reason += " (Blocked by massive sell wall)"
        elif long_dist < 1.0 and long_dist < short_dist:
            primary = "SHORT"
            reason = f"Long liq sangat dekat (-{long_dist}%) → Priority Squeeze"
            if up_energy > down_energy * 5:
                primary = "LONG"
                reason += " (Blocked by massive buy wall)"
        else:
            if short_dist < long_dist:
                primary = "LONG"
                reason = f"Short liq lebih dekat (+{short_dist}%)"
            else:
                primary = "SHORT"
                reason = f"Long liq lebih dekat (-{long_dist}%)"

            if up_energy < down_energy * 0.5 and primary == "SHORT":
                primary = "LONG"
                reason = "Energi up sangat murah → HFT akan pump terlebih dahulu"
            elif down_energy < up_energy * 0.5 and primary == "LONG":
                primary = "SHORT"
                reason = "Energi down sangat murah → HFT akan dump terlebih dahulu"

        if oi_delta > 2.0:
            reason += ", OI naik → posisi terperangkap memperkuat arah"
        if agg < DEAD_AGG_THRESHOLD and flow < DEAD_FLOW_THRESHOLD:
            primary = "LONG" if short_dist < long_dist else "SHORT"
            reason = "Dead market, target likuidasi terdekat"

        return {"bias": primary, "reason": reason, "confidence": "HIGH"}


class MultiStrategyVoting:
    def __init__(self):
        self.strategies = {}
        self.dynamic_weights = {}

    def register_strategy(self, name: str, base_weight: float):
        self.strategies[name] = base_weight

    def update_weights(self, market_conditions: Dict):
        agg = market_conditions.get("agg", 1.0)
        flow = market_conditions.get("flow", 1.0)
        is_dead = agg < DEAD_AGG_THRESHOLD and flow < DEAD_FLOW_THRESHOLD

        for name in self.strategies:
            if is_dead:
                if "energy" in name or "vacuum" in name:
                    self.dynamic_weights[name] = self.strategies[name] * 5
                elif "distribution" in name:
                    self.dynamic_weights[name] = self.strategies[name] * 0.2
                else:
                    self.dynamic_weights[name] = self.strategies[name]
            else:
                self.dynamic_weights[name] = self.strategies[name]

    def vote(self, signals: Dict[str, str]) -> Dict:
        score_long = 0.0
        score_short = 0.0
        total_weight = 0.0

        for strategy, bias in signals.items():
            weight = self.dynamic_weights.get(
                strategy, self.strategies.get(strategy, 1.0)
            )
            total_weight += weight
            if bias == "LONG":
                score_long += weight
            elif bias == "SHORT":
                score_short += weight

        if total_weight == 0:
            return {"bias": "NEUTRAL", "confidence": 0}

        long_prob = score_long / total_weight
        short_prob = score_short / total_weight

        if long_prob > VOTE_THRESHOLD:
            return {"bias": "LONG", "confidence": long_prob}
        elif short_prob > VOTE_THRESHOLD:
            return {"bias": "SHORT", "confidence": short_prob}
        else:
            return {"bias": "NEUTRAL", "confidence": max(long_prob, short_prob)}

# ================= INDICATOR CALCULATOR =================

class IndicatorCalculator:
    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, period + 1):
            change = closes[-i] - closes[-i - 1]
            if change >= 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_stoch(highs: List[float], lows: List[float], closes: List[float],
                        period: int = 14, smooth: int = 3) -> Tuple[float, float]:
        if len(closes) < period + smooth:
            return 50.0, 50.0

        k_values = []
        for i in range(smooth):
            idx = -1 - i
            start = -period - i
            end = None if i == 0 else -i
            low_min = min(lows[start:end]) if start < 0 else min(lows[start:])
            high_max = max(highs[start:end]) if start < 0 else max(highs[start:])
            if high_max == low_min:
                k = 50.0
            else:
                close = closes[idx]
                k = (close - low_min) / (high_max - low_min) * 100
            k_values.append(k)

        k_current = k_values[0]
        d = sum(k_values) / len(k_values)
        return k_current, d

    @staticmethod
    def calculate_obv(closes: List[float], volumes: List[float]) -> Tuple[List[float], str, float]:
        if len(closes) < 2:
            return [], "NEUTRAL", 0.0

        obv = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])

        current_obv = obv[-1] if obv else 0.0

        if len(obv) < 20:
            return obv, "NEUTRAL", current_obv

        recent_obv = obv[-20:]
        if all(x < y for x, y in zip(recent_obv, recent_obv[1:])):
            trend = "POSITIVE"
        elif all(x > y for x, y in zip(recent_obv, recent_obv[1:])):
            trend = "NEGATIVE"
        else:
            trend = "NEUTRAL"

        if current_obv > 0 and current_obv > max(obv) * 1.1:
            trend = "POSITIVE_EXTREME"
        if current_obv < 0 and current_obv < min(obv) * 0.9:
            trend = "NEGATIVE_EXTREME"

        # 🔥 PATCH AIOTUSDT/RLSUSDT: Override trend berdasarkan absolute value
        # Jika OBV value sangat negatif tapi tidak monoton → NEGATIVE_EXTREME
        # Jika OBV value sangat positif tapi tidak monoton → POSITIVE_EXTREME
        EXTREME_THRESHOLD = 30_000_000  # 30 juta

        if trend == "NEUTRAL":
            if current_obv < -EXTREME_THRESHOLD:
                trend = "NEGATIVE_EXTREME"  # OBV negatif besar = distribusi panjang
            elif current_obv > EXTREME_THRESHOLD:
                trend = "POSITIVE_EXTREME"  # OBV positif besar = akumulasi panjang

        return obv, trend, current_obv

    @staticmethod
    def get_liquidation_zones(highs: List[float], lows: List[float], price: float) -> Dict:
        if not highs or not lows or price == 0:
            return {"long_dist": 99.0, "short_dist": 99.0}
        recent_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        recent_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        long_dist = ((price - recent_low) / recent_low) * 100 if recent_low != 0 else 0
        short_dist = ((recent_high - price) / price) * 100 if price != 0 else 0
        return {
            "long_dist": round(long_dist, 2),
            "short_dist": round(short_dist, 2),
            "recent_low": recent_low,
            "recent_high": recent_high
        }

    @staticmethod
    def calculate_energy(order_book: Dict) -> Tuple[float, float]:
        if not order_book or not order_book.get("asks") or not order_book.get("bids"):
            return 1.0, 1.0
        bids = order_book["bids"]
        asks = order_book["asks"]
        if not bids or not asks:
            return 1.0, 1.0
        mid_price = (bids[0][0] + asks[0][0]) / 2
        target_up = mid_price * 1.001
        target_down = mid_price * 0.999
        up_energy = 0.0
        down_energy = 0.0
        for price, qty in asks:
            if price >= target_up:
                break
            up_energy += qty * (price - mid_price)
        for price, qty in reversed(bids):
            if price <= target_down:
                break
            down_energy += qty * (mid_price - price)
        return up_energy, down_energy

    @staticmethod
    def calculate_retail_order_flow(trades: List[Dict]) -> float:
        if not trades:
            return 1.0
        sizes = []
        for t in trades:
            qty = t.get('q') or t.get('qty')
            if qty is not None:
                sizes.append(abs(float(qty)))
        if not sizes:
            return 1.0
        median_size = np.median(sizes)
        small_trades = [
            t for t in trades
            if abs(float(t.get('q', t.get('qty', 0)))) < median_size
        ]
        if not small_trades:
            return 1.0
        buys = 0
        sells = 0
        for t in small_trades:
            is_sell = t.get('m', False) or t.get('isBuyerMaker', False)
            if not is_sell:
                buys += 1
            else:
                sells += 1
        if sells == 0:
            return 10.0
        return buys / sells

    @staticmethod
    def calculate_ma(closes: List[float], period: int) -> float:
        if len(closes) < period:
            return closes[-1]
        return sum(closes[-period:]) / period

# ================= DATA FETCHER WITH CACHING =================

class BinanceFetcher:
    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.base_url = "https://fapi.binance.com"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.verify = False
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=3,
            pool_block=False
        )
        self.session.mount('https://', adapter)
        self.cache = {}
        self.cache_time = {}
        self.cache_ttl = {
            "funding_rate": 3600,
            "open_interest": 60,
            "klines_1m": 30,
            "klines_5m": 60,
        }

    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self.cache:
            age = time.time() - self.cache_time.get(key, 0)
            if age < self.cache_ttl.get(key.split('_')[0], 60):
                return self.cache[key]
        return None

    def _set_cached(self, key: str, value: Any):
        self.cache[key] = value
        self.cache_time[key] = time.time()

    def fetch(self, endpoint: str, params: Dict = None) -> Optional[Any]:
        try:
            url = f"{self.base_url}{endpoint}"
            resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            print(f"❌ Fetch error {endpoint}: {e}")
            return None

    def get_price(self) -> Optional[float]:
        data = self.fetch("/fapi/v1/ticker/price", {"symbol": self.symbol})
        return safe_float(data.get("price")) if data else None

    def get_klines(self, interval: str = "1m", limit: int = 100) -> Optional[Dict]:
        cache_key = f"klines_{interval}_{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        data = self.fetch("/fapi/v1/klines", {
            "symbol": self.symbol,
            "interval": interval,
            "limit": limit
        })
        if not data:
            return None
        closes = [safe_float(k[4]) for k in data]
        highs = [safe_float(k[2]) for k in data]
        lows = [safe_float(k[3]) for k in data]
        volumes = [safe_float(k[5]) for k in data]
        result = {"highs": highs, "lows": lows, "closes": closes, "volumes": volumes}
        self._set_cached(cache_key, result)
        return result

    def get_order_book(self, limit: int = 50) -> Optional[Dict]:
        data = self.fetch("/fapi/v1/depth", {"symbol": self.symbol, "limit": limit})
        if not data:
            return None
        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
        return {"bids": bids, "asks": asks}

    def get_trades(self, limit: int = 500) -> Optional[List[Dict]]:
        data = self.fetch("/fapi/v1/trades", {"symbol": self.symbol, "limit": limit})
        if not data:
            return None
        return data

    def get_open_interest(self) -> Optional[float]:
        cache_key = "open_interest"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        data = self.fetch("/fapi/v1/openInterest", {"symbol": self.symbol})
        oi = safe_float(data.get("openInterest")) if data else None
        if oi is not None:
            self._set_cached(cache_key, oi)
        return oi

    def get_oi_history(self, limit: int = 10) -> Optional[List[float]]:
        oi = self.get_open_interest()
        return [oi] if oi else None

    def get_funding_rate(self) -> Optional[float]:
        cache_key = "funding_rate"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        data = self.fetch("/fapi/v1/fundingRate", {"symbol": self.symbol, "limit": 1})
        if data and len(data) > 0:
            rate = safe_float(data[0].get("fundingRate"))
            self._set_cached(cache_key, rate)
            return rate
        return None

    def get_mark_price(self) -> dict:
        """
        Fetch mark price from Binance premiumIndex endpoint.
        Mark price digunakan untuk liquidation calculation.
        """
        data = self.fetch("/fapi/v1/premiumIndex", {"symbol": self.symbol})
        if data:
            return {
                "mark_price": safe_float(data.get("markPrice")),
                "index_price": safe_float(data.get("indexPrice")),
                "last_funding_rate": safe_float(data.get("lastFundingRate"))
            }
        return {"mark_price": None, "index_price": None, "last_funding_rate": None}

    def calculate_wmi(self, short_dist: float, long_dist: float) -> float:
        if short_dist < 0.1 or long_dist < 0.1:
            return 0
        short_mass = 1.0 / (short_dist ** 2)
        long_mass = 1.0 / (long_dist ** 2)
        if short_mass + long_mass == 0:
            return 0
        return ((short_mass - long_mass) / (short_mass + long_mass)) * 100

# ================= LATENCY COMPENSATOR =================

class LatencyCompensator:
    def __init__(self):
        self.latency_history = deque(maxlen=100)
        self.base_threshold = MAX_LATENCY_MS

    def measure_latency(self) -> float:
        try:
            start = time.time()
            requests.get("https://fapi.binance.com/fapi/v1/time", timeout=5)
            latency = (time.time() - start) * 1000
            self.latency_history.append(latency)
            return np.mean(self.latency_history)
        except:
            return 999.0

    def get_adaptive_threshold(self) -> float:
        if not self.latency_history:
            return self.base_threshold
        avg_latency = np.mean(self.latency_history)
        return min(avg_latency * 1.5, 1000)

    def adjust_signal(self, bias: str, latency_ms: float) -> str:
        adaptive = self.get_adaptive_threshold()
        if latency_ms > adaptive:
            return "WAIT"
        return bias

# ================= STATE MANAGER =================

class StateManager:
    def __init__(self):
        self.price_history = deque(maxlen=100)
        self.rsi_history = deque(maxlen=30)
        self.last_bias = "NEUTRAL"
        self.last_entry_price = 0.0
        self.last_entry_time = 0.0

    def update(self, price: float, rsi: float):
        self.price_history.append(price)
        self.rsi_history.append(rsi)

    def update_position(self, bias: str, price: float):
        self.last_bias = bias
        self.last_entry_price = price
        self.last_entry_time = time.time()

    def get_floating_pnl_pct(self, current_price: float) -> float:
        if self.last_bias == "NEUTRAL" or self.last_entry_price == 0:
            return 0.0
        if self.last_bias == "LONG":
            return ((current_price - self.last_entry_price) / self.last_entry_price) * 100
        else:
            return ((self.last_entry_price - current_price) / self.last_entry_price) * 100


# ================= ANALYZER =================

class BinanceAnalyzer:
    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.fetcher = BinanceFetcher(symbol)
        self.state_mgr = StateManager()
        self.voter = MultiStrategyVoting()
        self.voter.register_strategy("energy", 2.0)
        self.voter.register_strategy("vacuum", 1.5)
        self.voter.register_strategy("liquidity_proximity", 1.0)
        self.voter.register_strategy("distribution", 1.0)
        self.voter.register_strategy("momentum", 1.0)
        self.voter.register_strategy("algo_type", 1.2)
        self.voter.register_strategy("hft_6pct", 1.5)

        if IS_KOYEB:
            self.ws = None
            print("⚠️ Koyeb Nano: WebSocket disabled to save resources")
        else:
            self.ws = BinanceWebSocket(symbol.lower())
            self.ws.start()

        self.latency_comp = LatencyCompensator()
        self.last_latency = 0.0
        self.prev_ofi_bias = "NEUTRAL"
        self.prev_ofi_timestamp = 0.0
        self.ofi_consistency_required = 2.0

        # ========== SIGNAL STABILITY FILTERS ==========
        self.flip_cooldown_sec = 3.0           # detik minimal antara flip
        self.last_flip_time = 0.0              # timestamp flip terakhir
        self.last_confirmed_bias = "NEUTRAL"   # bias terakhir yang sudah dikonfirmasi
        self.confirmation_count = 0            # hitungan konfirmasi berturut-turut
        self.confirmation_required = 2         # butuh 2 tick konsisten

        # History untuk AGG persistence (Fix 2)
        self.agg_history = deque(maxlen=20)   # simpan 20 candle terakhir (1m per candle = 20 menit)
        self.last_agg_update = 0
        self.bait_start_time = None   # untuk Time-Weighted Patience Detector (Fix 4)

    def __del__(self):
        if hasattr(self, 'ws') and self.ws is not None:
            self.ws.stop()

    def _is_agg_sustained(self, agg_current: float, threshold: float = 0.75, min_period: int = 5) -> bool:
        """Cek apakah agg >= threshold selama minimal min_period candle terakhir (dalam history)"""
        if len(self.agg_history) < min_period:
            return False
        recent = list(self.agg_history)[-min_period:]
        return all(a >= threshold for a in recent)

    def _is_strong_signal(self, ofi, up_energy, down_energy, change_5m, rsi6) -> bool:
        if (ofi is not None
                and ofi.get("bias") != "NEUTRAL"
                and ofi.get("strength", 0) > 0.7):
            return True
        if ((up_energy < ENERGY_ZERO_THRESHOLD and down_energy > MIN_ENERGY_TO_MOVE)
                or (down_energy < ENERGY_ZERO_THRESHOLD and up_energy > MIN_ENERGY_TO_MOVE)):
            return True
        if abs(change_5m) > 8.0:
            return True
        return False

    def _validate_gamma_execution(self, result: dict) -> dict:
        """
        Validasi apakah gamma executing itu real atau fake.
        Returns dict dengan keys: valid, reason, new_bias (jika perlu override)
        """
        gamma_exec = result.get("greeks_gamma_executing", False)
        who_dies = result.get("greeks_who_dies_first", "")
        kill_dir = result.get("greeks_kill_direction", "")
        rsi6_5m = result.get("rsi6_5m", 50.0)
        volume_ratio = result.get("volume_ratio", 1.0)
        ofi_bias = result.get("ofi_bias", "")
        agg = result.get("agg", 0.5)
        down_energy = result.get("down_energy", 0.0)
        up_energy = result.get("up_energy", 0.0)
        short_liq = result.get("short_liq", 99.0)
        long_liq = result.get("long_liq", 99.0)

        # ===== 1. Fake Gamma Trap =====
        if gamma_exec and who_dies == "BOTH_POSSIBLE":
            return {
                "valid": False,
                "reason": f"FAKE GAMMA TRAP: gamma_executing=True but who_dies={who_dies} → direction not committed",
                "new_bias": "NEUTRAL"
            }

        # ===== 2. Overbought Kill Trap =====
        if kill_dir == "LONG" and rsi6_5m > 90 and volume_ratio < 0.8:
            return {
                "valid": False,
                "reason": f"OVERBOUGHT KILL TRAP: kill_dir=LONG but RSI5m={rsi6_5m:.1f}>90, volume={volume_ratio:.2f}x → blow-off top, reversal expected",
                "new_bias": "SHORT"
            }
        if kill_dir == "SHORT" and rsi6_5m < 10 and volume_ratio < 0.8:
            return {
                "valid": False,
                "reason": f"OVERSOLD KILL TRAP: kill_dir=SHORT but RSI5m={rsi6_5m:.1f}<10 → capitulation bottom, reversal expected",
                "new_bias": "LONG"
            }

        # ===== 3. OFI Spoofing Re-check =====
        if ofi_bias == "SHORT" and agg > 0.6 and down_energy == 0:
            # OFI palsu – ikuti agg dan liquidity
            if short_liq < long_liq:
                correct_bias = "LONG"
            else:
                correct_bias = "SHORT"
            return {
                "valid": False,
                "reason": f"OFI SPOOFING RE-CHECK: OFI={ofi_bias} but agg={agg:.2f}, down_energy=0 → forcing {correct_bias}",
                "new_bias": correct_bias
            }
        if ofi_bias == "LONG" and agg < 0.4 and up_energy == 0:
            if long_liq < short_liq:
                correct_bias = "SHORT"
            else:
                correct_bias = "LONG"
            return {
                "valid": False,
                "reason": f"OFI SPOOFING RE-CHECK: OFI={ofi_bias} but agg={agg:.2f}, up_energy=0 → forcing {correct_bias}",
                "new_bias": correct_bias
            }

        return {"valid": True, "reason": "Gamma execution valid", "new_bias": result.get("bias")}

    def _apply_stability_filters(self, result: dict, phase_result, greeks_dict: dict) -> dict:
        """
        Menerapkan 5 filter stabilitas sinyal:
        1. Flip Cooldown (anti-flip cepat)
        2. Phase Lock (BAIT phase membutuhkan Gamma EXTREME untuk override)
        3. Confirmation Window (butuh N tick konsisten)
        4. Gamma Delay (Gamma EXTREME tapi delta exposure < 0.95 => tunda)
        5. Entry Filter (tambahkan rekomendasi entry di output)
        """
        # ========== AMBIL SEMUA VARIABEL YANG DIPERLUKAN DI AWAL METHOD ==========
        short_liq = result.get("short_liq", 99.0)
        long_liq = result.get("long_liq", 99.0)
        agg_val = result.get("agg", 0.5)
        ofi_bias = result.get("ofi_bias", "NEUTRAL")
        ofi_strength = result.get("ofi_strength", 0.0)
        volume_ratio = result.get("volume_ratio", 1.0)
        rsi6_val = result.get("rsi6", 50.0)
        change_5m_val = result.get("change_5m", 0.0)
        down_energy_val = result.get("down_energy", 0.0)
        up_energy_val = result.get("up_energy", 0.0)
        funding_rate_val = result.get("funding_rate", 0.0)
        obv_trend = result.get("obv_trend", "NEUTRAL")
        obv_value = result.get("obv_value", 0.0)
        kill_direction = result.get("greeks_kill_direction", "")
        who_dies_first = result.get("greeks_who_dies_first", "")
        delta_crowded = result.get("greeks_delta_crowded", "NEUTRAL")
        gamma_executing = result.get("greeks_gamma_executing", False)
        kill_speed = result.get("greeks_kill_speed", 0.0)
        gamma_intensity = result.get("greeks_gamma_intensity", "LOW")
        
        new_bias = result["bias"]
        now = time.time()
        market_phase = phase_result.phase if phase_result else "UNKNOWN"
        
        # ========== PREP PHASE HARD BLOCK (LECTURER FIX 2) ==========
        # Jika market phase PREP, netralkan semua sinyal dan langsung return
        if result.get("market_phase") == "PREP":
            result["bias"] = "NEUTRAL"
            result["confidence"] = "BLOCK"
            result["entry_allowed"] = False
            result["priority_level"] = -20000
            result["reason"] = f"[PREP HARD BLOCK] Market dalam fase PREP (akumulasi) → NO TRADE | " + result.get("reason", "")
            # Tidak perlu proses filter lain
            return result
        
        # ===== BAIT PHASE SOFT BLOCK (tidak langsung block, tapi turunkan confidence) =====
        if market_phase == "BAIT":
            # 🔥 EXCEPTION: short_liq super dekat + no sellers + kill_direction LONG
            kill_dir = result.get("greeks_kill_direction", "")
            
            if (short_liq < 1.5 and 
                down_energy_val < 0.01 and 
                kill_dir == "LONG" and 
                change_5m_val > 0):
                # Jangan block, izinkan sinyal dengan penalti confidence
                if result.get("confidence") == "ABSOLUTE":
                    result["confidence"] = "HIGH"
                result["reason"] = f"[BAIT EXCEPTION: short_liq super close] " + result.get("reason", "")
                # Jangan return, lanjut ke filter lain
            else:
                # Kondisi di mana sinyal di BAIT phase dianggap cukup kuat untuk tetap dipertahankan
                strong_conditions = []
                
                # Kondisi 1: Liquidity sangat dekat (<1%)
                if short_liq < 1.0 and short_liq < long_liq:
                    strong_conditions.append(f"short_liq={short_liq:.2f}%")
                elif long_liq < 1.0 and long_liq < short_liq:
                    strong_conditions.append(f"long_liq={long_liq:.2f}%")
                
                # Kondisi 2: Gamma EXTREME dengan kill_speed tinggi (≥5)
                if gamma_intensity == "EXTREME" and kill_speed >= 5.0:
                    strong_conditions.append(f"gamma_extreme+speed={kill_speed:.1f}")
                
                # Kondisi 3: RSI6 ekstrem (>=98 atau <=2) dengan volume rendah
                if (rsi6_val >= 98 or rsi6_val <= 2) and volume_ratio < 0.7:
                    strong_conditions.append(f"rsi_extreme={rsi6_val:.1f}")
                
                # Kondisi 4: Funding ekstrem + liquidity dekat
                if funding_rate_val is not None:
                    if (funding_rate_val < -0.005 and short_liq < 2.0) or (funding_rate_val > 0.005 and long_liq < 2.0):
                        strong_conditions.append(f"funding_extreme={funding_rate_val:.5f}")
                
                # Kondisi 5: Volume spike > 3x + momentum kuat (>2%)
                latest_vol = result.get("latest_volume", 0)
                vol_ma10 = result.get("volume_ma10", 1)
                vol_spike = latest_vol / vol_ma10 if vol_ma10 > 0 else 1
                if vol_spike > 3.0 and abs(change_5m_val) > 2.0:
                    strong_conditions.append(f"vol_spike={vol_spike:.1f}x")
                
                # Kondisi 6: Overbought/oversold ekstrem dengan liquidity dekat
                if (rsi6_val > 85 and short_liq < 2.0) or (rsi6_val < 15 and long_liq < 2.0):
                    strong_conditions.append(f"rsi_liq_extreme")
                
                # Jika ada kondisi kuat, izinkan sinyal dengan penalti confidence
                if strong_conditions:
                    # Turunkan confidence jika terlalu tinggi (ABSOLUTE -> HIGH)
                    if result.get("confidence") == "ABSOLUTE":
                        result["confidence"] = "HIGH"
                    # Tambahkan informasi ke reason
                    result["reason"] = f"[BAIT PHASE - STRONG SIGNAL: {', '.join(strong_conditions)}] " + result.get("reason", "")
                    # Jangan block, lanjutkan ke filter lain
                else:
                    # Tidak ada kondisi kuat → block sinyal, paksa NEUTRAL
                    result["bias"] = "NEUTRAL"
                    result["confidence"] = "BLOCK"
                    result["entry_allowed"] = False
                    result["greeks_override"] = False
                    result["reason"] = f"[BAIT SOFT BLOCK] No strong signal in BAIT phase. " + result.get("reason", "")
                    return result  # Langsung return, tidak lanjut ke filter lain
        
        # Ambil data Greeks dari result (sudah ada dari greeks_final_screen)
        gamma_intensity = result.get("greeks_gamma_intensity", "LOW")
        delta_exposure = result.get("greeks_delta_exposure", 0.0)
        greeks_override = result.get("greeks_override", False)
        
        # ========== FILTER 4: Gamma Delay ==========
        # Jangan delay jika sudah ada override dari Gamma spoofing (priority -10000)
        is_gamma_spoof_override = result.get("phase") == "GAMMA_LIQUIDITY_ALIGNMENT"
        
        if gamma_intensity == "EXTREME" and delta_exposure < 0.95 and not is_gamma_spoof_override:
            # Gamma fake: belum cukup crowded, tunda override
            if greeks_override:
                result["reason"] += f" | GAMMA DELAY: intensity EXTREME but delta_exposure={delta_exposure:.3f}<0.95 → hold"
                # Jangan terapkan override ini, kembalikan bias sebelumnya
                new_bias = self.last_confirmed_bias if self.last_confirmed_bias != "NEUTRAL" else result["bias"]
                # Turunkan confidence sementara
                result["confidence"] = "HIGH"
                result["greeks_override"] = False  # batalkan override
        
        # ========== FILTER 2: Phase Lock + Greeks Override Blocker ==========
        # Hard block dari lecturer: block greeks override jika BAIT + gamma belum jalan
        if _should_block_greeks_override(result, market_phase):
            result["reason"] += f" | GREEKS OVERRIDE BLOCKED: BAIT phase + gamma not executing"
            result["greeks_override"] = False
            new_bias = self.last_confirmed_bias if self.last_confirmed_bias != "NEUTRAL" else result["bias"]
            result["confidence"] = "HIGH"
        elif market_phase == "BAIT" and greeks_override and gamma_intensity != "EXTREME":
            # Di BAIT phase, hanya Gamma EXTREME yang boleh override
            result["reason"] += f" | PHASE LOCK: BAIT phase, greeks override blocked (gamma={gamma_intensity})"
            result["greeks_override"] = False
            new_bias = self.last_confirmed_bias if self.last_confirmed_bias != "NEUTRAL" else result["bias"]
            result["confidence"] = "HIGH"
        
        # ========== FILTER 3: Confirmation Window ==========
        if new_bias == self.last_confirmed_bias:
            self.confirmation_count += 1
        else:
            self.confirmation_count = 1  # mulai hitung dari 1 untuk bias baru
        
        if self.confirmation_count < self.confirmation_required:
            # Belum cukup konfirmasi, pakai bias terkonfirmasi sebelumnya
            if self.last_confirmed_bias != "NEUTRAL":
                result["reason"] += f" | CONF WINDOW: need {self.confirmation_required} ticks, current {self.confirmation_count} → holding {self.last_confirmed_bias}"
                new_bias = self.last_confirmed_bias
                result["confidence"] = "MEDIUM"
            # jika belum ada bias terkonfirmasi, biarkan new_bias (pertama kali)
        else:
            # Cukup konfirmasi, update bias terkonfirmasi
            self.last_confirmed_bias = new_bias
        
        # ========== FILTER 1: Flip Cooldown ==========
        if self.last_confirmed_bias != "NEUTRAL" and new_bias != self.last_confirmed_bias:
            elapsed = now - self.last_flip_time
            if elapsed < self.flip_cooldown_sec:
                result["reason"] += f" | FLIP COOLDOWN: {elapsed:.1f}s < {self.flip_cooldown_sec}s → block flip to {new_bias}"
                new_bias = self.last_confirmed_bias
                result["confidence"] = "MEDIUM"
            else:
                self.last_flip_time = now
        
        # Terapkan bias akhir
        result["bias"] = new_bias
        
        # ========== OBV-VOLUME VETO (LECTURER FIX 3) ==========
        # Jika OBV NEGATIVE_EXTREME + volume_ratio < 0.4 dan bias = LONG → paksa SHORT
        if obv_trend == "NEGATIVE_EXTREME" and volume_ratio < 0.4 and result.get("bias") == "LONG":
            result["bias"] = "SHORT"
            result["reason"] = f"[OBV-VOLUME VETO] OBV {obv_trend}, volume {volume_ratio:.2f}x → LONG override ditolak, paksa SHORT | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = -1104.6
        
        # Mirror: OBV POSITIVE_EXTREME + volume_ratio < 0.4 dan bias = SHORT → paksa LONG
        if obv_trend == "POSITIVE_EXTREME" and volume_ratio < 0.4 and result.get("bias") == "SHORT":
            result["bias"] = "LONG"
            result["reason"] = f"[OBV-VOLUME VETO] OBV {obv_trend}, volume {volume_ratio:.2f}x → SHORT override ditolak, paksa LONG | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = -1104.6
        
        # ===== CAPITULATION TRAP GUARD (PRIORITY -1104.95) =====
        capitulation_trap = CapitulationTrapGuard.detect(
            rsi6=rsi6_val,
            long_liq=long_liq,
            change_5m=change_5m_val,
            obv_trend=result.get("obv_trend", "NEUTRAL"),
            volume_ratio=volume_ratio
        )
        if capitulation_trap["override"]:
            result["bias"] = capitulation_trap["bias"]
            result["reason"] = f"[CAPITULATION TRAP] {capitulation_trap['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = capitulation_trap["priority"]
            # Matikan override lain yang memaksa LONG
            funding_override = {"override": False}
            volume_dryup_result = {"override": False}
            extreme_oversold_bounce = {"override": False}
            result["_capitulation_trap"] = True

        # ===== GREEKS SHORT TRAP OVERRIDE (PRIORITY -9996) =====
        greeks_short_trap = GreeksShortTrapOverride.detect(
            greeks_liq_7pct=result.get("greeks_liq_7pct", ""),
            agg=agg_val,
            ofi_bias=result.get("ofi_bias", "NEUTRAL"),
            volume_ratio=volume_ratio,
            rsi6=rsi6_val,
            change_5m=change_5m_val
        )
        if greeks_short_trap["override"]:
            result["bias"] = greeks_short_trap["bias"]
            result["reason"] = f"[GREEKS SHORT TRAP] {greeks_short_trap['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = greeks_short_trap["priority"]
            # Matikan override dari Greeks (karena sudah kita override)
            result["greeks_override"] = False
            # Tandai agar tidak diproses ulang
            result["_greeks_short_trap"] = True
        
        # ========== PUMP FAKE SHORT LIQ TRAP (LECTURER FIX - NEW DETECTOR) ==========
        # Priority -1104.8: Deteksi pump palsu dengan short liq dekat sebagai umpan
        pump_fake_trap = PumpFakeShortLiqTrap.detect(
            short_liq=short_liq,
            change_5m=change_5m_val,
            rsi6=rsi6_val,
            volume_ratio=volume_ratio,
            agg=agg_val
        )
        if pump_fake_trap["override"]:
            result["bias"] = pump_fake_trap["bias"]
            result["reason"] = f"[PUMP FAKE TRAP] {pump_fake_trap['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = pump_fake_trap["priority"]
            # Matikan squeeze override yang mungkin sudah ada
            extreme_short_squeeze_result = {"override": False}
            short_liq_super_close_result = {"override": False}
            # Tandai agar tidak diproses lebih lanjut oleh override lain
            result["_pump_fake_override"] = True
        # ========== OBV CONFLICT GUARD (SOLV case) ==========
        # Cek apakah OBV bertentangan keras dengan bias di BAIT phase
        final_bias = result.get("bias", "NEUTRAL")
        if _check_obv_conflict(result, final_bias) and market_phase == "BAIT":
            entry_ok = False
            result["reason"] += f" | OBV CONFLICT IN BAIT PHASE — high risk of trap (bias={final_bias}, obv={result.get('obv_trend', 'N/A')})"
        
        # ========== FILTER 5: Entry Filter + Delayed Entry Logic ==========
        # Tambahkan rekomendasi entry berdasarkan phase + delayed entry system
        kill_check = _check_kill_confirmation(result)
        delayed_entry = _apply_delayed_entry_logic(self.symbol, result, final_bias)
        
        # Base entry logic dari phase
        entry_ok = False
        if market_phase == "KILL" and result.get("greeks_gamma_executing", False):
            entry_ok = True
        elif market_phase == "KILL" and gamma_intensity in ("HIGH", "EXTREME"):
            entry_ok = True
        elif market_phase == "BAIT":
            entry_ok = False
        
        # Override dengan delayed entry logic jika ada BAIT sebelumnya
        if not delayed_entry["entry_allowed"]:
            entry_ok = False
            result["entry_reason_delayed"] = delayed_entry["entry_reason"]
        
        result["entry_allowed"] = entry_ok
        result["entry_reason"] = (
            delayed_entry["entry_reason"] if not entry_ok else
            ("OK to enter (KILL phase + gamma executing)" if entry_ok
             else "WAIT: market in BAIT phase or gamma not executing")
        )
        result["kill_confirmation"] = kill_check  # tambahkan info kill confirmation
        
        # Jika entry tidak diizinkan dan bias bukan NEUTRAL, turunkan confidence
        if not entry_ok and result["bias"] in ("LONG", "SHORT"):
            if result["confidence"] == "ABSOLUTE":
                result["confidence"] = "HIGH"
        
        # ===== NEW: GAMMA EXECUTION VALIDATION (LECTURER'S SARAN) =====
        # Validasi apakah gamma executing itu real atau fake
        gamma_valid = self._validate_gamma_execution(result)
        if not gamma_valid["valid"]:
            result["entry_allowed"] = False
            result["entry_reason"] = gamma_valid["reason"]
            result["bias"] = gamma_valid["new_bias"]
            result["confidence"] = "BLOCK"
            result["priority_level"] = -1100.1  # lebih tinggi dari trap detector
            result["reason"] = f"[GAMMA VALIDATION BLOCK] {gamma_valid['reason']} | " + result.get("reason", "")
        
        # ===== DUAL LIQ TRAP FILTER WITH ADDITIONAL CONDITIONS =====
        # Periksa dual_liq_trap dengan kondisi tambahan setelah validasi gamma
        dual_trap = result.get("dual_liq_trap", {})
        if dual_trap.get("dual_liq_trap", False) and dual_trap.get("trap_score", 0) >= 3:
            # Jika dual trap aktif, jangan izinkan entry meskipun gamma executing
            result["entry_allowed"] = False
            result["entry_reason"] = f"DUAL LIQ TRAP (score {dual_trap['trap_score']}/5) – {dual_trap.get('reason', '')}"
            result["confidence"] = "BLOCK"
            result["priority_level"] = -1100.2
        
        # ===== NEW: KILL-ZONE FLIP TRAP DETECTION INTEGRATION =====
        # 1. Track kill direction history
        symbol = self.symbol
        kill_dir = result.get("greeks_kill_direction", "")
        _track_kill_direction(symbol, kill_dir)

        # 2. Cek Kill Direction Stability
        dir_stability = _check_kill_direction_stability(symbol)
        result["kill_direction_stability"] = dir_stability

        # 3. Cek Dual Liquidity Trap
        dual_trap = _check_dual_liq_trap(result)
        result["dual_liq_trap"] = dual_trap

        # 4. Cek Bias vs Kill Direction Conflict
        bias_conflict = _check_bias_kill_conflict(result, result.get("bias", "NEUTRAL"))
        result["bias_kill_conflict"] = bias_conflict

        # ===== KAIDAH BARU: BIAS-KILL CONFLICT = HARD BLOCK =====
        # Jika bias_kill_conflict = True, sistem sudah tahu bias salah
        # Langsung flip ke kill_direction tanpa perlu nunggu konfirmasi lain
        bias_conflict_data = result.get("bias_kill_conflict", {})
        if (bias_conflict_data.get("has_conflict", False) and
            not result.get("greeks_override", False)):
            
            correct_dir = bias_conflict_data.get("correct_direction", "")
            if correct_dir in ("LONG", "SHORT"):
                
                # Validasi: liquidity juga harus konsisten
                short_liq = result.get("short_liq", 99.0)
                long_liq = result.get("long_liq", 99.0)
                liq_ok = (
                    (correct_dir == "LONG" and short_liq < long_liq) or
                    (correct_dir == "SHORT" and long_liq < short_liq)
                )
                
                kill_speed = abs(result.get("greeks_kill_speed", 0))
                
                if liq_ok and kill_speed > 2.0:
                    result["bias"] = correct_dir
                    result["reason"] = (
                        f"[BIAS-KILL HARD CORRECTION] "
                        f"kill_direction={correct_dir} konflik dengan bias sebelumnya. "
                        f"Liquidity konsisten (liq_ok={liq_ok}), "
                        f"kill_speed={kill_speed:.1f} → paksa ke {correct_dir} | "
                        + result.get("reason", "")
                    )
                    result["confidence"] = "ABSOLUTE"
                    result["priority_level"] = -9998

        # ===== PRIORITY -10003: POST-PUMP DISTRIBUTION (PALING TINGGI) =====
        post_pump_result = PostPumpDistribution.detect(
            change_5m=change_5m_val,
            volume_ratio=volume_ratio,
            obv_trend=result.get("obv_trend", "NEUTRAL"),
            funding_rate=funding_rate_val
        )
        if post_pump_result["override"]:
            result["bias"] = post_pump_result["bias"]
            result["reason"] = f"[POST-PUMP DISTRIBUTION] {post_pump_result['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = post_pump_result["priority"]
            result["_post_pump_override"] = True

        # ===== PRIORITY -10002: EXTREME FUNDING + OBV CONSENSUS =====
        if not result.get("_post_pump_override", False):
            extreme_funding_result = ExtremeFundingObvConsensus.detect(
                funding_rate=funding_rate_val,
                obv_trend=result.get("obv_trend", "NEUTRAL"),
                change_5m=change_5m_val,
                volume_ratio=volume_ratio
            )
            if extreme_funding_result["override"]:
                result["bias"] = extreme_funding_result["bias"]
                result["reason"] = f"[EXTREME FUNDING OBV] {extreme_funding_result['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = extreme_funding_result["priority"]
                result["_funding_obv_override"] = True

        # ===== PRIORITY -10002: VEGA FADE OVERRIDE (BAIT + DUAL TRAP) =====
        # FIX 3: Jika Vega aktif dan (BAIT phase atau dual trap) dengan volume rendah,
        # paksa bias mengikuti greeks_bias (yang biasanya fade direction) dan skip crowded resolver
        vega_fade_triggered = False
        vega_active = result.get("greeks_vega_active", False)
        dual_trap_active = result.get("dual_liq_trap", {}).get("dual_liq_trap", False)
        market_phase = phase_result.phase if phase_result else "UNKNOWN"
        
        if vega_active and (market_phase == "BAIT" or dual_trap_active) and volume_ratio < 0.7:
            greeks_bias = result.get("greeks_bias", "NEUTRAL")
            if greeks_bias in ("LONG", "SHORT"):
                result["bias"] = greeks_bias
                result["reason"] = f"[VEGA FADE OVERRIDE] BAIT phase + dual trap aktif, mengikuti Greeks fade ke {greeks_bias} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = -10002
                result["_vega_fade_override"] = True
                # Skip crowded resolver nanti
                vega_fade_triggered = True
            else:
                vega_fade_triggered = False
        else:
            vega_fade_triggered = False
        
        # ===== PRIORITY -10002: VEGA ACTIVE SHORT OVERRIDE =====
        # FIX 4: Detector baru yang secara eksplisit memaksa SHORT ketika Vega aktif
        if not vega_fade_triggered and not result.get("_post_pump_override", False):
            vega_short_override = VegaActiveShortOverride.detect(
                vega_active=vega_active,
                volume_ratio=volume_ratio,
                change_5m=change_5m_val,
                short_liq=short_liq,
                obv_trend=result.get("obv_trend", "NEUTRAL"),
                agg=agg_val
            )
            if vega_short_override["override"]:
                result["bias"] = vega_short_override["bias"]
                result["reason"] = f"[VEGA SHORT OVERRIDE] {vega_short_override['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = vega_short_override["priority"]
                result["_vega_short_override"] = True
                vega_fade_triggered = True  # Skip crowded resolver

        # 🔥 PRIORITY -1104.5: Agg-OFI Bearish Override
        # Ketika agg < 0.3 (majoritas sell), OFI SHORT kuat, short_liq dekat sebagai umpan
        bearish_override = AggOFIBearishOverride.detect(
            agg=agg_val,
            ofi_bias=result.get("ofi_bias", "NEUTRAL"),
            ofi_strength=result.get("ofi_strength", 0.0),
            short_liq=short_liq,
            change_5m=change_5m_val
        )
        if bearish_override["override"]:
            result["bias"] = bearish_override["bias"]
            result["reason"] = f"[AGG-OFI BEARISH] {bearish_override['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = bearish_override["priority"]
            # Matikan squeeze override
            extreme_short_squeeze_result = {"override": False}
            short_liq_super_close_result = {"override": False}
            # Lanjutkan, jangan return agar filter lain tetap jalan

        # ===== PRIORITY -10001.5: PRESWEEP MISINTERPRETATION GUARD =====
        presweep_triggered = False
        if not result.get("_post_pump_override", False) and not result.get("_funding_obv_override", False) and not vega_fade_triggered:
            presweep_guard = PresweepMisinterpretationGuard.detect(
                long_liq=long_liq,
                short_liq=short_liq,
                agg=agg_val,
                ofi_bias=result.get("ofi_bias", "NEUTRAL"),
                ofi_strength=result.get("ofi_strength", 0.0),
                funding_rate=funding_rate_val,
                rsi6=rsi6_val,
                change_5m=change_5m_val
            )
            if presweep_guard["override"]:
                result["bias"] = presweep_guard["bias"]
                result["reason"] = f"[PRESWEEP GUARD] {presweep_guard['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = presweep_guard["priority"]
                result["_presweep_override"] = True
                presweep_triggered = True

        # ===== FUNDING EXTREME DUAL TRAP OVERRIDE (PRIORITY -10001.5) =====
        dual_trap = result.get("dual_liq_trap", {})
        funding_extreme_override = FundingExtremeDualTrapOverride.detect(
            dual_liq_trap=dual_trap.get("dual_liq_trap", False),
            trap_score=dual_trap.get("trap_score", 0),
            funding_rate=funding_rate_val,
            greeks_kill_direction=result.get("greeks_kill_direction", ""),
            volume_ratio=volume_ratio
        )
        if funding_extreme_override["override"]:
            result["bias"] = funding_extreme_override["bias"]
            result["reason"] = f"[FUNDING EXTREME DUAL TRAP] {funding_extreme_override['reason']} | " + result.get("reason", "")
            result["confidence"] = "ABSOLUTE"
            result["priority_level"] = funding_extreme_override["priority"]
            result["_funding_extreme_override"] = True
            # Skip crowded resolver dan dual liq first move
            presweep_triggered = True  # reuse flag untuk skip
            vega_fade_triggered = True

        # ===== PRIORITY -10001: CROWDED DIRECTION RESOLVER =====
        # Harus dipanggil SETELAH greeks_final_screen karena butuh greeks_delta_crowded
        # Dan hanya jika tidak ada override dari detector priority lebih tinggi
        if not result.get("_post_pump_override", False) and not result.get("_funding_obv_override", False) and not presweep_triggered and not vega_fade_triggered:
            crowded_resolver = CrowdedDirectionLiquidityResolver.detect(
                delta_crowded=result.get("greeks_delta_crowded", "NEUTRAL"),
                short_liq=short_liq,
                long_liq=long_liq,
                agg=agg_val,
                ofi_bias=result.get("ofi_bias", "NEUTRAL"),
                ofi_strength=result.get("ofi_strength", 0.0),
                volume_ratio=volume_ratio,
                rsi6=rsi6_val,
                change_5m=change_5m_val,
                who_dies_first=result.get("greeks_who_dies_first", ""),
                greeks_kill_direction=result.get("greeks_kill_direction", ""),
                funding_rate=funding_rate_val,
                obv_trend=result.get("obv_trend", "NEUTRAL"),
                vega_active=vega_active  # FIX 2: Pass vega_active parameter
            )
            if crowded_resolver["override"]:
                result["bias"] = crowded_resolver["bias"]
                result["reason"] = f"[CROWDED RESOLVER] {crowded_resolver['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = crowded_resolver["priority"]
                result["_crowded_override"] = True

        # ===== PRIORITY -10000.5: AGG SPOOFING WITH CONTEXT =====
        if not result.get("_crowded_override", False):
            agg_spoof_ctx = AggSpoofingWithLiquidityContext.detect(
                agg=agg_val,
                short_liq=short_liq,
                long_liq=long_liq,
                obv_trend=result.get("obv_trend", "NEUTRAL"),
                obv_value=result.get("obv_value", 0.0),
                volume_ratio=volume_ratio,
                change_5m=change_5m_val,
                down_energy=down_energy_val,
                up_energy=up_energy_val,
                funding_rate=funding_rate_val or 0.0
            )
            if agg_spoof_ctx["override"]:
                result["bias"] = agg_spoof_ctx["bias"]
                result["reason"] = f"[AGG CONTEXT] {agg_spoof_ctx['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = agg_spoof_ctx["priority"]
                result["_agg_override"] = True
        
        # ===== AGG PERSISTENCE CHECK (LECTURER FIX 6) =====
        agg_val = result.get("agg", 0.5)
        if agg_val > 0.85 and self._is_agg_sustained(agg_val, threshold=0.8, min_period=5):
            # Jika ada AGG spoofing override yang memaksa SHORT, batalkan
            if result.get("_agg_override") and result.get("bias") == "SHORT":
                result["bias"] = "LONG"
                result["reason"] = f"[AGG SUSTAINED] agg={agg_val:.2f} konsisten selama 5 menit → genuine accumulation, override SHORT menjadi LONG | " + result.get("reason", "")
                result["priority_level"] = -10000.6
                result["_agg_override"] = False

        # ===== PRIORITY -10000: DUAL LIQ FIRST MOVE =====
        if not result.get("_crowded_override", False) and not result.get("_agg_override", False):
            dual_trap_data = result.get("dual_liq_trap", {})
            first_move = DualLiqFirstMoveFollower.detect(
                dual_liq_trap=dual_trap_data.get("dual_liq_trap", False),
                trap_score=dual_trap_data.get("trap_score", 0),
                first_move_direction=dual_trap_data.get("first_move_direction", "UNKNOWN"),
                short_liq=short_liq,
                long_liq=long_liq,
                who_dies_first=result.get("greeks_who_dies_first", ""),
                agg=agg_val,
                volume_ratio=volume_ratio,
                change_5m=change_5m_val,
                funding_rate=funding_rate_val,      # tambahan
                ofi_bias=result.get("ofi_bias", "NEUTRAL")   # tambahan
            )
            if first_move["override"]:
                result["bias"] = first_move["bias"]
                result["reason"] = f"[DUAL LIQ FIRST MOVE] {first_move['reason']} | " + result.get("reason", "")
                result["confidence"] = "ABSOLUTE"
                result["priority_level"] = first_move["priority"]

        # 5. NEW DETECTORS FROM LECTURER FEEDBACK
        # 5a. Bullish Order Flow Divergence Detector
        agg = result.get("agg", 0.0)
        ofi_bias = result.get("ofi_bias", "")
        ofi_strength = result.get("ofi_strength", 0.0)
        change_5m = change_5m_val
        volume_ratio = volume_ratio
        
        divergence_result = BullishOrderFlowDivergence.detect(
            agg=agg,
            ofi_bias=ofi_bias,
            ofi_strength=ofi_strength,
            change_5m=change_5m,
            volume_ratio=volume_ratio,
            greeks_kill_direction=kill_dir
        )
        result["bullish_orderflow_divergence"] = divergence_result
        
        # 5b. Kill-Liquidity Conflict Detector
        short_liq = result.get("short_liq", 99.0)
        long_liq = result.get("long_liq", 99.0)
        gamma_executing = result.get("greeks_gamma_executing", False)
        
        kill_liq_result = KillLiquidityConflict.detect(
            short_dist=short_liq,
            long_dist=long_liq,
            kill_dir=kill_dir,
            gamma_executing=gamma_executing
        )
        result["kill_liquidity_conflict"] = kill_liq_result

        # 5c. BlowOffTopShortLiqTrap Detector (Priority -1102)
        rsi6_5m = result.get("rsi6_5m", 50.0)
        blowoff_result = BlowOffTopShortLiqTrap.detect(
            change_5m=change_5m,
            short_dist=short_liq,
            rsi6_5m=rsi6_5m,
            volume_ratio=volume_ratio,
            ofi_bias=ofi_bias,
            agg=agg
        )
        result["blowoff_top_trap"] = blowoff_result

        # 5d. VolumeDryUpReversal Detector (Priority -1080)
        volume_dryup_result = VolumeDryUpReversal.detect(
            change_5m=change_5m,
            volume_ratio=volume_ratio,
            rsi6_5m=rsi6_5m
        )
        result["volume_dryup_reversal"] = volume_dryup_result

        # 🔥 NEW: LowVolumeOverboughtSqueeze Detector (Priority -1105) - HIGHEST PRIORITY
        down_energy = result.get("down_energy", 0.0)
        low_vol_overbought_result = LowVolumeOverboughtSqueeze.detect(
            volume_ratio=volume_ratio,
            rsi6_5m=rsi6_5m,
            short_dist=short_liq,
            down_energy=down_energy,
            agg=agg,
            change_5m=change_5m
        )
        result["low_volume_overbought_squeeze"] = low_vol_overbought_result

        # 🔥🔥 PRIORITY -1104.5: RSI100AbsoluteReversal (MUST BE BEFORE squeeze detectors)
        rsi6_val = result.get("rsi6", 50.0)
        long_liq_val = result.get("long_dist", 99.0)
        rsi100_rev = RSI100AbsoluteReversal.detect(
            rsi6=rsi6_val,
            rsi6_5m=rsi6_5m,
            change_5m=change_5m,
            short_liq=short_liq,
            long_liq=long_liq_val,
            volume_ratio=volume_ratio
        )
        result["rsi100_absolute_reversal"] = rsi100_rev

        # 🔥🔥 PRIORITY -1103.5: OverboughtSqueezeGuard (guard untuk ExtremeShortLiqSqueeze)
        overbought_guard = OverboughtSqueezeGuard.detect(
            rsi6=rsi6_val,
            rsi6_5m=rsi6_5m,
            short_liq=short_liq,
            change_5m=change_5m
        )
        result["overbought_squeeze_guard"] = overbought_guard

        # 🔥 NEW: ShortLiqSuperCloseOverride Detector (Priority -1104)
        kill_dir = result.get("greeks_kill_direction", "")
        short_liq_super_close_result = ShortLiqSuperCloseOverride.detect(
            short_dist=short_liq,
            long_dist=long_liq,
            down_energy=down_energy,
            kill_direction=kill_dir,
            change_5m=change_5m,
            agg=agg,
            ofi_bias=result.get("ofi_bias", "NEUTRAL"),
            ofi_strength=result.get("ofi_strength", 0.0)
        )
        result["short_liq_super_close"] = short_liq_super_close_result

        # 5e. ExtremeShortLiqSqueezeOverride Detector (Priority -1103) - UPDATED WITH down_energy
        up_energy = result.get("up_energy", 0.0)
        extreme_short_squeeze_result = ExtremeShortLiqSqueezeOverride.detect(
            short_dist=short_liq,
            long_dist=long_liq,
            agg=agg,
            down_energy=down_energy,
            change_5m=change_5m,
            up_energy=up_energy,
            ofi_bias=result.get("ofi_bias", "NEUTRAL"),
            ofi_strength=result.get("ofi_strength", 0.0)
        )
        result["extreme_short_liq_squeeze"] = extreme_short_squeeze_result

        # 5f. EmptyBookSqueezeContinuation Detector (Priority -1102)
        empty_book_squeeze_result = EmptyBookSqueezeContinuation.detect(
            up_energy=up_energy,
            down_energy=down_energy,
            short_dist=short_liq,
            long_dist=long_liq,
            change_5m=change_5m,
            agg=agg
        )
        result["empty_book_squeeze"] = empty_book_squeeze_result

        # 🔥 PRIORITY -1104.6: FundingCrowdedOverride (koreksi delta_crowded berdasarkan funding rate)
        funding_rate_val = result.get("funding_rate", 0.0)
        funding_override = FundingCrowdedOverride.detect(
            funding_rate=funding_rate_val,
            short_liq=short_liq,
            long_liq=long_liq,
            change_5m=change_5m,
            rsi6=rsi6_val
        )
        result["funding_crowded_override"] = funding_override

        # 🔥 PRIORITY -1103.8: BaitPhaseShortLiqTrap
        market_phase = phase_result.phase if phase_result else "UNKNOWN"
        bait_trap = BaitPhaseShortLiqTrap.detect(
            market_phase=market_phase,
            short_liq=short_liq,
            rsi6=rsi6_val,
            funding_rate=funding_rate_val,
            change_5m=change_5m,
            down_energy=down_energy
        )
        result["bait_phase_short_liq_trap"] = bait_trap

        # 6. Apply semua ke entry_allowed dengan priority ladder
        if result.get("entry_allowed", True):  # hanya block jika belum di-block
            
            # 🔥 PRIORITY -1104.6: Funding Crowded Override (koreksi delta_crowded) - HIGHEST PRIORITY AFTER RSI100
            if funding_override.get("override", False):
                result["bias"] = funding_override["bias"]
                result["reason"] = f"[FUNDING CROWDED OVERRIDE] {funding_override['reason']} | " + result.get("reason", "")
                result["priority_level"] = funding_override.get("priority", -1104.6)
                result["confidence"] = "ABSOLUTE"
                # Matikan detector lain yang lebih rendah prioritasnya
                extreme_short_squeeze_result = {"override": False}
                overbought_guard = {"override": False}
            
            # 🔥 PRIORITY -1103.8: BAIT Phase Short Liq Trap (setelah OverboughtSqueezeGuard)
            elif bait_trap.get("override", False):
                result["bias"] = bait_trap["bias"]
                result["reason"] = f"[BAIT PHASE SHORT LIQ TRAP] {bait_trap['reason']} | " + result.get("reason", "")
                result["priority_level"] = bait_trap.get("priority", -1103.8)
                result["confidence"] = "ABSOLUTE"
                # Matikan squeeze detector yang lebih rendah
                extreme_short_squeeze_result = {"override": False}
            
            # 🔥🔥 Priority -1104.5: RSI100AbsoluteReversal (HIGHEST - overrides ALL squeeze detectors)
            elif rsi100_rev.get("override", False):
                result["bias"] = rsi100_rev["bias"]
                result["reason"] = f"[RSI100 ABSOLUTE REVERSAL] {rsi100_rev['reason']} | " + result.get("reason", "")
                result["priority_level"] = rsi100_rev.get("priority", -1104.5)
                result["confidence"] = "ABSOLUTE"
                # Skip semua detector lain yang lebih rendah prioritasnya
                extreme_short_squeeze_result = {"override": False}  # matikan squeeze detector
            
            # Priority -1105: LowVolumeOverboughtSqueeze (PALING TINGGI)
            elif low_vol_overbought_result.get("override", False):
                result["bias"] = low_vol_overbought_result["bias"]
                result["reason"] = f"[LOW VOLUME OVERBOUGHT SQUEEZE] {low_vol_overbought_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = low_vol_overbought_result.get("priority", -1105)
            
            # Priority -1104: ShortLiqSuperCloseOverride
            elif short_liq_super_close_result.get("override", False):
                result["bias"] = short_liq_super_close_result["bias"]
                result["reason"] = f"[SHORT LIQ SUPER CLOSE] {short_liq_super_close_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = short_liq_super_close_result.get("priority", -1104)
            
            # 🔥🔥 Priority -1103.5: OverboughtSqueezeGuard (guard untuk ExtremeShortLiqSqueeze)
            elif overbought_guard.get("override", False):
                result["bias"] = overbought_guard["bias"]
                result["reason"] = f"[OVERBOUGHT SQUEEZE GUARD] {overbought_guard['reason']} | " + result.get("reason", "")
                result["priority_level"] = overbought_guard.get("priority", -1103.5)
                # Override hasil squeeze detector jika ada
                extreme_short_squeeze_result = {"override": False}
            
            # Priority -1103: ExtremeShortLiqSqueezeOverride
            elif extreme_short_squeeze_result.get("override", False):
                result["bias"] = extreme_short_squeeze_result["bias"]
                result["reason"] = f"[EXTREME SHORT LIQ SQUEEZE] {extreme_short_squeeze_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = extreme_short_squeeze_result.get("priority", -1103)
            
            # Priority -1102: EmptyBookSqueezeContinuation
            elif empty_book_squeeze_result.get("override", False):
                result["bias"] = empty_book_squeeze_result["bias"]
                result["reason"] = f"[EMPTY BOOK SQUEEZE] {empty_book_squeeze_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = empty_book_squeeze_result.get("priority", -1102)
            
            # Priority -1102: BlowOffTopShortLiqTrap
            elif blowoff_result.get("override", False):
                result["bias"] = blowoff_result["bias"]
                result["reason"] = f"[BLOW-OFF TOP TRAP] {blowoff_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = blowoff_result.get("priority", -1102)
            
            # Kill Instability Block: Jika kill direction tidak stabil DAN dual liq trap, force NEUTRAL
            elif dir_stability.get("danger") and dual_trap.get("dual_liq_trap"):
                result["bias"] = "NEUTRAL"
                result["entry_allowed"] = False
                result["reason"] = f"[KILL INSTABILITY BLOCK] {dir_stability.get('reason')} + dual liq trap → NO TRADE"
                result["priority_level"] = -1100.5
            
            elif dir_stability["danger"]:
                result["entry_allowed"] = False
                result["entry_reason"] = f"KILL FLIP TRAP — {dir_stability['reason']}"
            
            elif bias_conflict["has_conflict"]:
                result["entry_allowed"] = False
                result["entry_reason"] = f"BIAS-KILL CONFLICT — {bias_conflict['reason']}"
                # Koreksi bias ke arah yang benar
                result["bias_corrected"] = bias_conflict["correct_direction"]
                result["reason"] = f"[BIAS CORRECTED → {bias_conflict['correct_direction']}] " + result.get("reason", "")
            
            elif divergence_result.get("override", False):
                # Override dari Bullish Order Flow Divergence (Priority -1101)
                result["bias"] = divergence_result["bias"]
                result["reason"] = f"[ORDERFLOW DIVERGENCE] {divergence_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = divergence_result.get("priority", -1101)
            
            elif kill_liq_result.get("override", False):
                # Override dari Kill-Liquidity Conflict (Priority -1100)
                result["bias"] = kill_liq_result["bias"]
                result["reason"] = f"[KILL-LIQUIDITY CONFLICT] {kill_liq_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = kill_liq_result.get("priority", -1100)
            
            elif volume_dryup_result.get("override", False):
                # Override dari Volume Dry-Up Reversal (Priority -1080)
                result["bias"] = volume_dryup_result["bias"]
                result["reason"] = f"[VOLUME DRY-UP REVERSAL] {volume_dryup_result['reason']} | " + result.get("reason", "")
                result["priority_level"] = volume_dryup_result.get("priority", -1080)
        
        # ========== CONFLICT SCORE THRESHOLD (LECTURER FIX 5) ==========
        # Hitung jumlah override yang aktif
        override_count = 0
        for key in ['_crowded_override', '_agg_override', '_vega_fade_override', '_presweep_override', 
                    '_post_pump_override', '_funding_obv_override', '_liquidity_extreme_override', 
                    '_obv_veto_long', '_pump_fake_override', '_greeks_short_trap', '_funding_extreme_override', '_capitulation_trap']:
            if result.get(key):
                override_count += 1
        
        if override_count >= 3:
            result["bias"] = "NEUTRAL"
            result["confidence"] = "BLOCK"
            result["entry_allowed"] = False
            result["reason"] = f"[CONFLICT THRESHOLD] {override_count} override aktif → kemungkinan TRAP, NO TRADE | " + result.get("reason", "")
            result["priority_level"] = -1105
        
        return result

    def analyze(self) -> Optional[Dict]:
        try:
            # Initialize _liquidity_extreme_override to prevent UnboundLocalError
            _liquidity_extreme_override = False
            
            self.last_latency = self.latency_comp.measure_latency()
            if self.last_latency > self.latency_comp.get_adaptive_threshold():
                return self._build_latency_result()

            price = self.fetcher.get_price()
            if not price:
                return None

            k1m = self.fetcher.get_klines("1m", 100)
            if not k1m:
                return None

            k5m = self.fetcher.get_klines("5m", 50)

            closes_1m = k1m["closes"]
            highs_1m = k1m["highs"]
            lows_1m = k1m["lows"]
            volumes_1m = k1m["volumes"]

            # ========== MACD DUEL ==========
            if len(closes_1m) >= 50:
                macd, signal_line, hist = calculate_macd(closes_1m, 12, 26, 9)
                hist_scaled = scale_macd(hist)
                macd_decision = macd_duel_logic(hist_scaled)
            else:
                macd_decision = {"action": "NONE"}

            latest_volume = volumes_1m[-1] if volumes_1m else 0.0
            if len(volumes_1m) >= 10:
                volume_ma10 = sum(volumes_1m[-10:]) / 10
            else:
                volume_ma10 = latest_volume

            order_book = self.fetcher.get_order_book(50)
            trades_rest = self.fetcher.get_trades(500)
            oi = self.fetcher.get_open_interest()
            oi_history = self.fetcher.get_oi_history(2)
            funding_rate = self.fetcher.get_funding_rate()
            
            # Fetch mark price data untuk MarkPriceGapDetector
            mark_data = self.fetcher.get_mark_price()
            mark_price = mark_data.get("mark_price")
            
            # Exchange Risk Score (composite, dipakai sebagai info)
            oi_delta = 0.0
            if oi_history and len(oi_history) >= 2:
                oi_delta = ((oi_history[-1] - oi_history[-2]) / oi_history[-2]) * 100 if oi_history[-2] != 0 else 0.0

            ws_trades = []
            ws_order_book = None
            if self.ws and self.ws.connected:
                ws_data = self.ws.get_latest()
                ws_trades = ws_data["trades"]
                ws_order_book = ws_data["order_book"]

            trades = ws_trades if ws_trades else (trades_rest or [])
            if ws_order_book and ws_order_book.get("bids"):
                order_book = ws_order_book

            rsi6 = IndicatorCalculator.calculate_rsi(closes_1m, 6)
            rsi14 = IndicatorCalculator.calculate_rsi(closes_1m, 14)
            stoch_k, stoch_d = IndicatorCalculator.calculate_stoch(highs_1m, lows_1m, closes_1m)
            obv, obv_trend, obv_value = IndicatorCalculator.calculate_obv(closes_1m, volumes_1m)
            obv_magnitude = (
                "HIGH" if abs(obv_value) > 50_000_000
                else "MEDIUM" if abs(obv_value) > 10_000_000
                else "LOW"
            )
            liq = IndicatorCalculator.get_liquidation_zones(highs_1m, lows_1m, price)
            ma25 = IndicatorCalculator.calculate_ma(closes_1m, 25)
            ma99 = IndicatorCalculator.calculate_ma(closes_1m, 99)

            vol_5m = sum(volumes_1m[-5:]) if len(volumes_1m) >= 5 else 0
            vol_10m = sum(volumes_1m[-10:]) if len(volumes_1m) >= 10 else 0
            volume_ratio = safe_div(vol_5m, vol_10m, 1.0)

            if len(closes_1m) >= 5:
                change_5m = ((closes_1m[-1] - closes_1m[-6]) / closes_1m[-6]) * 100
            else:
                change_5m = 0.0

            if len(closes_1m) >= 2:
                change_30s = ((closes_1m[-1] - closes_1m[-2]) / closes_1m[-2]) * 100 * 0.5
            else:
                change_30s = 0.0

            rsi6_5m = 50.0
            if k5m and len(k5m["closes"]) >= 6:
                rsi6_5m = IndicatorCalculator.calculate_rsi(k5m["closes"], 6)

            up_energy, down_energy = IndicatorCalculator.calculate_energy(
                order_book if order_book else {}
            )
            retail_flow = IndicatorCalculator.calculate_retail_order_flow(trades) if trades else 1.0

            if trades:
                buys = 0
                sells = 0
                for t in trades:
                    is_sell = t.get('m', False) or t.get('isBuyerMaker', False)
                    if is_sell:
                        sells += 1
                    else:
                        buys += 1
                agg = safe_div(buys, buys + sells, 0.5)
                # Fix 2: Simpan agg ke history untuk persistence check
                self.agg_history.append(agg)
                flow = agg
            else:
                agg, flow = 0.5, 0.5

            oi_delta = 0.0
            if oi_history and len(oi_history) >= 2:
                oi_delta = ((oi_history[0] - oi_history[1]) / oi_history[1]) * 100 if oi_history[1] != 0 else 0

            volatility = (
                (max(highs_1m[-20:]) - min(lows_1m[-20:])) / price
                if price > 0 else 0
            )

            ofi_raw = OrderFlowImbalance.calculate(trades, window_ms=2000)
            current_time = time.time()
            if (ofi_raw["bias"] == self.prev_ofi_bias
                    and (current_time - self.prev_ofi_timestamp) >= self.ofi_consistency_required):
                ofi = ofi_raw
            else:
                if ofi_raw["bias"] != self.prev_ofi_bias:
                    self.prev_ofi_bias = ofi_raw["bias"]
                    self.prev_ofi_timestamp = current_time
                ofi = {"bias": self.prev_ofi_bias, "strength": ofi_raw["strength"]}

            # ========== OFI Consistency Validator (FIX BUG AIOTUSDT) ==========
            ofi_validated = OFIConsistencyValidator.validate_and_fix(
                ofi["bias"], ofi["strength"],
                agg, flow, up_energy, down_energy
            )
            ofi = ofi_validated  # replace ofi dengan yang sudah divalidasi

            # ========== Data Snapshot Consistency Check (FIX BUG DISPLAY ≠ JSON) ==========
            ofi_resolved = DataSnapshotConsistencyCheck.resolve(
                agg, ofi["bias"], ofi["strength"], up_energy, down_energy
            )
            ofi = ofi_resolved  # replace ofi dengan yang sudah di-resolve

            iceberg = IcebergDetector.detect(trades, price) if trades else {"detected": False}
            cross_lead = CrossExchangeLeader.check_leader(self.symbol)
            funding_trap = FundingRateTrap.detect(funding_rate or 0, oi or 0)
            liq_heat = LiquidationHeatMap.fetch_real_liq(self.symbol)

            # ========== OrderBook Slope ==========
            bid_slope, ask_slope = OrderBookSlope.calculate(order_book)
            slope_signal = OrderBookSlope.signal(bid_slope, ask_slope)

            # ========== Ask-Bid Slope Imbalance Detector (RLSUSDT PATTERN) ==========
            ask_bid_imbalance = AskBidSlopeImbalanceDetector.detect(
                ask_slope, bid_slope, change_5m, rsi6, down_energy, up_energy
            )

            # ========== Falling Knife OBV Confirm (RLSUSDT PATTERN) ==========
            falling_knife_obv = FallingKnifeOBVConfirm.detect(
                obv_value, ask_slope, bid_slope, rsi6, rsi6_5m,
                change_5m, volume_ratio, down_energy
            )

            # ========== Latency Arbitrage Predictor ==========
            predicted_price = LatencyArbitragePredictor.predict_next_price(
                price, change_5m, up_energy, down_energy, LATENCY_MS_ESTIMATE
            )

            # ========== NEW DETECTORS: ShortLiqProximityAggSqueeze & DownEnergyZeroShortLiqClose ==========
            short_liq_agg_squeeze = ShortLiqProximityAggSqueeze.detect(
                liq["short_dist"], liq["long_dist"], agg,
                down_energy, up_energy, change_5m,
                rsi6, volume_ratio
            )

            down_energy_zero_close = DownEnergyZeroShortLiqClose.detect(
                down_energy, liq["short_dist"], liq["long_dist"],
                up_energy, agg
            )

            # ========== Master Squeeze Rule (untuk digunakan di priority ladder) ==========
            master_squeeze = MasterSqueezeRule.detect(
                liq["short_dist"], liq["long_dist"], change_5m,
                down_energy, up_energy, volume_ratio
            )

            # ========== Overbought / Oversold Distribution Traps ==========
            overbought_trap = OverboughtDistributionTrap.detect(
                rsi6, liq["short_dist"], liq["long_dist"], volume_ratio,
                down_energy, up_energy, ofi["bias"], ofi["strength"], change_5m,
                agg=agg  # ← TAMBAHKAN parameter agg
            )

            if overbought_trap["override"]:
                final_bias = overbought_trap["bias"]
                final_reason = overbought_trap["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "OVERBOUGHT_DISTRIBUTION_TRAP"
                priority = overbought_trap["priority"]
                algo_type = {"bias": "NEUTRAL", "confidence": "MEDIUM"}
                hft_6pct = {"bias": "NEUTRAL", "reason": ""}
            else:
                oversold_trap = OversoldSqueezeTrap.detect(
                    rsi6, liq["long_dist"], liq["short_dist"], volume_ratio,
                    up_energy, down_energy, ofi["bias"], ofi["strength"], change_5m
                )
                if oversold_trap["override"]:
                    final_bias = oversold_trap["bias"]
                    final_reason = oversold_trap["reason"]
                    final_confidence = "ABSOLUTE"
                    final_phase = "OVERSOLD_SQUEEZE_TRAP"
                    priority = oversold_trap["priority"]
                    algo_type = {"bias": "NEUTRAL", "confidence": "MEDIUM"}
                    hft_6pct = {"bias": "NEUTRAL", "reason": ""}
                else:
                    empty_book = EmptyBookTrapDetector.detect(
                        down_energy, up_energy, liq["short_dist"], liq["long_dist"],
                        rsi6_5m, volume_ratio, obv_trend, rsi6,
                        ofi["bias"], ofi["strength"],
                        funding_rate  # ← TAMBAHKAN funding_rate
                    )
                    if empty_book["override"]:
                        final_bias = empty_book["bias"]
                        final_reason = empty_book["reason"]
                        final_confidence = "ABSOLUTE"
                        final_phase = "EMPTY_BOOK_TRAP"
                        priority = empty_book["priority"]
                        algo_type = {"bias": "NEUTRAL", "confidence": "MEDIUM"}
                        hft_6pct = {"bias": "NEUTRAL", "reason": ""}
                    else:
                        # ===== PROBABILISTIC ENGINE =====
                        prob_engine = ProbabilisticEngine()
                        algo_type = {"bias": "NEUTRAL", "confidence": "MEDIUM"}
                        hft_6pct = {"bias": "NEUTRAL", "reason": ""}

                        # Initialize variables to prevent UnboundLocalError
                        dead_market = {"override": False}
                        flush = {"wait": False}
                        energy_gap = {"override": False}
                        energy_trap = {"override": False}
                        pump_exhaust = {"override": False}
                        liq_magnet = {"override": False}
                        exhausted_liquidity = {"override": False}
                        near_exhausted = {"override": False}
                        squeeze_trap = {"override": False}
                        overbought_trap_old = {"override": False}

                        # ===== HIGHEST PRIORITY OVERRIDES: LIQUIDITY EXTREME (-2001 to -1998) =====
                        # Definisikan semua detector terlebih dahulu
                        liquidity_extreme = LiquidityExtremeOverride.detect(
                            liq["short_dist"], liq["long_dist"], funding_rate,
                            change_5m, agg, up_energy
                        )
                        funding_squeeze = FundingExtremeSqueeze.detect(
                            liq["short_dist"], liq["long_dist"], funding_rate
                        )
                        rsi_squeeze = Rsi6ExtremeSqueeze.detect(
                            rsi6, liq["short_dist"], liq["long_dist"], change_5m
                        )
                        liq_absolute = LiquidityProximityAbsolute.detect(
                            liq["short_dist"], liq["long_dist"], change_5m
                        )

                        if liquidity_extreme["override"]:
                            final_bias = liquidity_extreme["bias"]
                            final_reason = liquidity_extreme["reason"]
                            final_confidence = liquidity_extreme["confidence"]
                            final_phase = "LIQUIDITY_EXTREME_OVERRIDE"
                            priority = liquidity_extreme["priority"]
                            prob_engine.add(liquidity_extreme["bias"], 20.01)
                            _liquidity_extreme_override = True
                        elif funding_squeeze.get("override"):
                            final_bias = funding_squeeze["bias"]
                            final_reason = funding_squeeze["reason"]
                            final_confidence = funding_squeeze["confidence"]
                            final_phase = "FUNDING_EXTREME_SQUEEZE"
                            priority = funding_squeeze["priority"]
                            prob_engine.add(funding_squeeze["bias"], 20.0)
                            _liquidity_extreme_override = True
                        elif rsi_squeeze.get("override"):
                            final_bias = rsi_squeeze["bias"]
                            final_reason = rsi_squeeze["reason"]
                            final_confidence = rsi_squeeze["confidence"]
                            final_phase = "RSI_EXTREME_SQUEEZE"
                            priority = rsi_squeeze["priority"]
                            prob_engine.add(rsi_squeeze["bias"], 19.99)
                            _liquidity_extreme_override = True
                        elif liq_absolute.get("override"):
                            final_bias = liq_absolute["bias"]
                            final_reason = liq_absolute["reason"]
                            final_confidence = liq_absolute["confidence"]
                            final_phase = "LIQUIDITY_ABSOLUTE"
                            priority = liq_absolute["priority"]
                            prob_engine.add(liq_absolute["bias"], 19.98)
                            _liquidity_extreme_override = True
                        else:
                            _liquidity_extreme_override = False

                        has_extreme_override = _liquidity_extreme_override

                        # ===== HIGH PRIORITY OVERRIDES (cascading if-else) =====

                        # ===== PRIORITY -1107: POST-SQUEEZE REVERSAL =====
                        post_squeeze = PostSqueezeReversal.detect(
                            change_5m, liq["short_dist"], rsi6,
                            volume_ratio, up_energy,
                            liq["long_dist"], down_energy, obv_trend
                        )
                        if not has_extreme_override and post_squeeze["override"]:
                            final_bias = post_squeeze["bias"]
                            final_reason = post_squeeze["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "POST_SQUEEZE_REVERSAL"
                            priority = post_squeeze["priority"]
                            prob_engine.add(post_squeeze["bias"], 10.09)

                        # ===== PRIORITY -1106: OVERBOUGHT LOW VOLUME REVERSAL (NEW) =====
                        olvr = OverboughtLowVolumeReversal.detect(
                            change_5m, rsi6, rsi6_5m, volume_ratio, liq["short_dist"]
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and olvr["override"]:
                            final_bias = olvr["bias"]
                            final_reason = olvr["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OVERBOUGHT_LOW_VOL_REVERSAL"
                            priority = olvr["priority"]
                            prob_engine.add(olvr["bias"], 10.08)

                        # ===== PRIORITY -1106: DOUBLE KILL SEQUENCE DETECTOR =====
                        # Note: kill_check_data dan dual_trap_data akan diambil dari result setelah result dibuat
                        # Untuk sementara, gunakan placeholder - akan di-update setelah result tersedia
                        double_kill = DoubleKillSequenceDetector.detect(
                            who_dies_first="BOTH_POSSIBLE",  # placeholder - akan diupdate setelah result tersedia
                            dual_liq_trap=False,  # placeholder
                            trap_score=0,  # placeholder
                            short_liq=liq["short_dist"],
                            long_liq=liq["long_dist"],
                            kill_confirmed=False,  # placeholder
                            ofi_bias=ofi["bias"],
                            ofi_strength=ofi["strength"],
                            agg=agg,
                            volume_ratio=volume_ratio,
                            kill_direction=""  # placeholder
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and double_kill["override"]:
                            final_bias = double_kill["bias"]
                            final_reason = double_kill["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "DOUBLE_KILL_SEQUENCE"
                            priority = double_kill["priority"]
                            prob_engine.add(double_kill["bias"], 10.07)

                        # ===== PRIORITY -1106: LOW VOLUME DISTRIBUTION CONTINUATION =====
                        low_vol_dist = LowVolumeDistributionContinuation.detect(
                            change_5m, rsi6,
                            liq["long_dist"], liq["short_dist"],
                            obv_trend, volume_ratio,
                            down_energy, up_energy
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and low_vol_dist["override"]:
                            final_bias = low_vol_dist["bias"]
                            final_reason = low_vol_dist["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "LOW_VOL_DISTRIBUTION_CONT"
                            priority = low_vol_dist["priority"]
                            prob_engine.add(low_vol_dist["bias"], 10.06)

                        # ===== PRIORITY -1105: VOLUME DRY-UP OVERBOUGHT TRAP (NEW) =====
                        vol_dry_trap = VolumeDryUpOverboughtTrap.detect(
                            volume_ratio, rsi6, change_5m
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and vol_dry_trap["override"]:
                            final_bias = vol_dry_trap["bias"]
                            final_reason = vol_dry_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "VOL_DRY_UP_OVERBOUGHT"
                            priority = vol_dry_trap["priority"]
                            prob_engine.add(vol_dry_trap["bias"], 10.05)

                        # ===== PRIORITY -1105: PROFIT IMBALANCE REVERSAL (EXCHANGE NEUTRALIZATION) =====
                        # TERTINGGI - di atas semua detector lain karena ini adalah kebijakan exchange level tertinggi
                        profit_reversal = ProfitImbalanceReversal.detect(
                            change_5m, rsi6,
                            liq["long_dist"], liq["short_dist"],
                            volume_ratio
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and profit_reversal["override"]:
                            final_bias = profit_reversal["bias"]
                            final_reason = profit_reversal["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "PROFIT_IMBALANCE_REVERSAL"
                            priority = profit_reversal["priority"]
                            prob_engine.add(profit_reversal["bias"], 10.04)

                        # ===== PRIORITY -1105: PROXIMITY CONTINUATION (DUSDT PATTERN) =====
                        proximity_cont = ProximityContinuationOverride.detect(
                            short_liq=liq["short_dist"], long_liq=liq["long_dist"],
                            change_5m=change_5m, volume_ratio=volume_ratio,
                            down_energy=down_energy, up_energy=up_energy,
                            agg=agg, ofi_bias=ofi["bias"]
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and proximity_cont["override"]:
                            final_bias = proximity_cont["bias"]
                            final_reason = proximity_cont["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "PROXIMITY_CONTINUATION"
                            priority = proximity_cont["priority"]
                            prob_engine.add(proximity_cont["bias"], 10.03)

                        # ===== PRIORITY -1104: KILL DIRECTION WITHOUT MOMENTUM (NEW) =====
                        greeks_dict = {}  # placeholder, akan diisi setelah greeks_final_screen dipanggil
                        kill_no_mom = KillDirectionWithoutMomentum.detect(
                            greeks_dict.get("kill_direction", ""),
                            greeks_dict.get("gamma_executing", False),
                            greeks_dict.get("kill_speed", 0),
                            volume_ratio
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and kill_no_mom["override"]:
                            final_bias = kill_no_mom["bias"]
                            final_reason = kill_no_mom["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "KILL_NO_MOMENTUM"
                            priority = kill_no_mom["priority"]

                        # ===== PRIORITY -1105: RSI DIVERGENCE REVERSAL (NEW) =====
                        rsi_div = RsiDivergenceReversal.detect(rsi6, rsi6_5m, volume_ratio)
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and rsi_div["override"]:
                            final_bias = rsi_div["bias"]
                            final_reason = rsi_div["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "RSI_DIVERGENCE_REVERSAL"
                            priority = rsi_div["priority"]
                            prob_engine.add(rsi_div["bias"], 10.05)

                        # ===== PRIORITY -1104: FAKE KILL DIRECTION (NEW) =====
                        fake_kill = FakeKillDirection.detect(
                            greeks_dict.get("kill_direction", ""),
                            greeks_dict.get("kill_speed", 0),
                            volume_ratio,
                            greeks_dict.get("gamma_executing", False),
                            greeks_dict.get("who_dies_first", "")
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not rsi_div.get("override") and fake_kill["override"]:
                            final_bias = fake_kill["bias"]
                            final_reason = fake_kill["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FAKE_KILL_DIRECTION"
                            priority = fake_kill["priority"]
                            prob_engine.add(fake_kill["bias"], 10.04)

                        # ===== PRIORITY -1103: OBV-PRICE DIVERGENCE (NEW) =====
                        obv_div = ObvPriceDivergence.detect(obv_trend, change_5m, volume_ratio)
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not rsi_div.get("override") and not fake_kill.get("override") and obv_div["override"]:
                            final_bias = obv_div["bias"]
                            final_reason = obv_div["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OBV_PRICE_DIVERGENCE"
                            priority = obv_div["priority"]
                            prob_engine.add(obv_div["bias"], 10.03)

                        # ===== PRIORITY -1102: EXTREME RSI SQUEEZE (NEW) =====
                        extreme_rsi = ExtremeRsiShortSqueeze.detect(rsi6, liq["short_dist"], liq["long_dist"], volume_ratio)
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not rsi_div.get("override") and not fake_kill.get("override") and not obv_div.get("override") and extreme_rsi["override"]:
                            final_bias = extreme_rsi["bias"]
                            final_reason = extreme_rsi["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_RSI_SQUEEZE"
                            priority = extreme_rsi["priority"]
                            prob_engine.add(extreme_rsi["bias"], 10.02)

                        # ===== PRIORITY -1100: OFI-AGG SPOOFING DETECTOR (NEW) =====
                        ofi_agg_spoof = OFIAggSpoofingDetector.detect(
                            ofi["bias"], ofi["strength"], agg, volume_ratio, change_5m
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not olvr.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not vol_dry_trap.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not kill_no_mom.get("override") and ofi_agg_spoof["override"]:
                            final_bias = ofi_agg_spoof["bias"]
                            final_reason = ofi_agg_spoof["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OFI_AGG_SPOOFING"
                            priority = ofi_agg_spoof["priority"]
                            prob_engine.add(ofi_agg_spoof["bias"], 10.0)

                        # ===== PRIORITY -1099: OFI BAIT VALIDATOR =====
                        # Note: dual_trap_data akan diambil dari result setelah result dibuat
                        # Untuk sementara, gunakan placeholder
                        ofi_bait_result = OFIBaitValidator.detect(
                            ofi_bias=ofi["bias"],
                            ofi_strength=ofi["strength"],
                            agg=agg,
                            volume_ratio=volume_ratio,
                            short_liq=liq["short_dist"],
                            long_liq=liq["long_dist"],
                            who_dies_first="BOTH_POSSIBLE",  # placeholder
                            dual_liq_trap=False  # placeholder
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not double_kill.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and ofi_bait_result["override"]:
                            final_bias = ofi_bait_result["bias"]
                            final_reason = ofi_bait_result["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OFI_BAIT_DETECTED"
                            priority = ofi_bait_result["priority"]
                            prob_engine.add(ofi_bait_result["bias"], 9.997)

                        # ===== PRIORITY -1104: OVERSOLD LONG LIQ BOUNCE REVERSAL =====
                        oversold_bounce = OversoldLongLiqBounceReversal.detect(
                            long_liq=liq.get("long_dist", 99.0),
                            rsi6=rsi6,
                            change_5m=change_5m,
                            volume_ratio=volume_ratio,
                            down_energy=down_energy
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and oversold_bounce["override"]:
                            final_bias = oversold_bounce["bias"]
                            final_reason = oversold_bounce["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OVERSOLD_LONG_LIQ_BOUNCE"
                            priority = oversold_bounce["priority"]
                            prob_engine.add(oversold_bounce["bias"], 10.06)

                        # ===== PRIORITY -1104: OVERBOUGHT SHORT LIQ DUMP REVERSAL (MIRROR) =====
                        overbought_dump = OverboughtShortLiqDumpReversal.detect(
                            short_liq=liq.get("short_dist", 99.0),
                            rsi6=rsi6,
                            change_5m=change_5m,
                            volume_ratio=volume_ratio,
                            up_energy=up_energy
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not oversold_bounce.get("override") and overbought_dump["override"]:
                            final_bias = overbought_dump["bias"]
                            final_reason = overbought_dump["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OVERBOUGHT_SHORT_LIQ_DUMP"
                            priority = overbought_dump["priority"]
                            prob_engine.add(overbought_dump["bias"], 10.06)

                        # ===== PRIORITY -1099: ASK WALL LONG TRAP =====
                        ask_wall_trap = AskWallLongTrap.detect(
                            ask_slope=ask_slope,
                            bid_slope=bid_slope,
                            agg=agg,
                            change_5m=change_5m,
                            rsi6=rsi6,
                            rsi6_5m=rsi6_5m,
                            volume_ratio=volume_ratio,
                            down_energy=down_energy
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not oversold_bounce.get("override") and not overbought_dump.get("override") and ask_wall_trap["override"]:
                            final_bias = ask_wall_trap["bias"]
                            final_reason = ask_wall_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "ASK_WALL_LONG_TRAP"
                            priority = ask_wall_trap["priority"]
                            prob_engine.add(ask_wall_trap["bias"], 9.997)

                        # ===== PRIORITY -1098: RSI MULTI-TF DIVERGENCE TRAP =====
                        rsi_tf_trap = RSIMultiTFDivergenceTrap.detect(
                            rsi6=rsi6,
                            rsi6_5m=rsi6_5m,
                            ask_slope=ask_slope,
                            bid_slope=bid_slope,
                            change_5m=change_5m,
                            volume_ratio=volume_ratio,
                            obv_trend=obv_trend
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and not proximity_cont.get("override") and not oversold_bounce.get("override") and not overbought_dump.get("override") and not ask_wall_trap.get("override") and rsi_tf_trap["override"]:
                            final_bias = rsi_tf_trap["bias"]
                            final_reason = rsi_tf_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "RSI_MULTI_TF_DIVERGENCE_TRAP"
                            priority = rsi_tf_trap["priority"]
                            prob_engine.add(rsi_tf_trap["bias"], 9.995)

                        # ===== PRIORITY -1104: OVERSOLD DISTRIBUTION CONTINUATION =====
                        oversold_dist = OversoldDistributionContinuation.detect(
                            change_5m, rsi6,
                            liq["long_dist"], liq["short_dist"],
                            obv_trend, obv_value, volume_ratio,
                            agg, ofi["bias"]
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not low_vol_dist.get("override") and not profit_reversal.get("override") and oversold_dist["override"]:
                            final_bias = oversold_dist["bias"]
                            final_reason = oversold_dist["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OVERSOLD_DISTRIBUTION_CONT"
                            priority = oversold_dist["priority"]
                            prob_engine.add(oversold_dist["bias"], 10.04)

                        # ===== PRIORITY -1104: GLOBAL POSITION IMBALANCE =====
                        global_imbalance = GlobalPositionImbalance.detect(
                            funding_rate, oi_delta, volume_ratio,
                            change_5m, liq["short_dist"], liq["long_dist"]
                        )
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and global_imbalance["override"]:
                            final_bias = global_imbalance["bias"]
                            final_reason = global_imbalance["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "GLOBAL_POSITION_IMBALANCE"
                            priority = global_imbalance["priority"]
                            prob_engine.add(global_imbalance["bias"], 10.05)

                        # ===== PRIORITY -1104/-1100/-1075: ASYMMETRIC LIQUIDITY MAX PAIN =====
                        # FIX DUSDT: long_liq 34% vs short_liq 6% = ratio 5.8x → SHORT
                        # Market menuju liquidity TERBESAR, bukan terdekat
                        if not has_extreme_override and not post_squeeze.get("override") and not global_imbalance.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override"):  # hanya jika GPI, Profit Reversal, dan Oversold Dist tidak trigger
                            asym_liq = AsymmetricLiquidityMaxPain.detect(
                                liq["short_dist"], liq["long_dist"],
                                volume_ratio, change_5m,
                                rsi6, rsi6_5m,
                                ofi["bias"], ofi["strength"],
                                funding_rate,
                                up_energy, down_energy,
                                obv_trend, agg
                            )
                            if asym_liq["override"]:
                                final_bias = asym_liq["bias"]
                                final_reason = asym_liq["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "ASYMMETRIC_LIQ_MAX_PAIN"
                                priority = asym_liq["priority"]
                                prob_engine.add(asym_liq["bias"], asym_liq["weight"])

                        # ===== PRIORITY -1103: DIP THEN RIP SWEEP =====
                        dip_then_rip = DipThenRipSweep.detect(
                            long_liq=liq["long_dist"],
                            short_liq=liq["short_dist"],
                            agg=agg,
                            up_energy=up_energy,
                            down_energy=down_energy,
                            rsi6=rsi6,
                            bid_slope=bid_slope,
                            ask_slope=ask_slope,
                            volume_ratio=volume_ratio,
                            change_5m=change_5m,
                            obv_trend=obv_trend
                        )
                        if dip_then_rip["override"]:
                            final_bias = dip_then_rip["bias"]
                            final_reason = dip_then_rip["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "DIP_THEN_RIP_SWEEP"
                            priority = dip_then_rip["priority"]
                            prob_engine.add(dip_then_rip["bias"], 10.03)

                        # ===== PRIORITY -1101: PHANTOM BUY ENERGY TRAP =====
                        if not dip_then_rip.get("override"):
                            phantom_buy = PhantomBuyEnergyTrap.detect(
                                ask_slope=ask_slope,
                                bid_slope=bid_slope,
                                up_energy=up_energy,
                                down_energy=down_energy,
                                change_5m=change_5m,
                                volume_ratio=volume_ratio,
                                funding_rate=funding_rate,
                                long_liq=liq["long_dist"],
                                short_liq=liq["short_dist"]
                            )
                            if phantom_buy["override"]:
                                final_bias = phantom_buy["bias"]
                                final_reason = phantom_buy["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "PHANTOM_BUY_ENERGY_TRAP"
                                priority = phantom_buy["priority"]
                                prob_engine.add(phantom_buy["bias"], 10.01)

                        # ===== PRIORITY -1103: EXTREME RSI6 OVERBOUGHT REVERSAL =====
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and not dip_then_rip.get("override"):
                            extreme_rsi6_rev = ExtremeRsi6OverboughtReversal.detect(
                                rsi6, volume_ratio, ofi["bias"], up_energy
                            )
                            if extreme_rsi6_rev["override"]:
                                final_bias = extreme_rsi6_rev["bias"]
                                final_reason = extreme_rsi6_rev["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "EXTREME_RSI6_OVERBOUGHT_REVERSAL"
                                priority = extreme_rsi6_rev["priority"]
                                prob_engine.add(extreme_rsi6_rev["bias"], 10.02)

                        # ===== PRIORITY -1103: FUNDING NEGATIVE + SHORT LIQ SUPER CLOSE =====
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and not dip_then_rip.get("override"):
                            funding_short_squeeze = FundingNegativeShortLiqSqueeze.detect(
                                funding_rate, liq["short_dist"], liq["long_dist"],
                                up_energy, down_energy, rsi6, volume_ratio
                            )
                            if funding_short_squeeze["override"]:
                                final_bias = funding_short_squeeze["bias"]
                                final_reason = funding_short_squeeze["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "FUNDING_NEG_SHORT_LIQ_SQUEEZE"
                                priority = funding_short_squeeze["priority"]
                                prob_engine.add(funding_short_squeeze["bias"], 10.03)

                        # ===== PRIORITY -1102: MOMENTUM VOLUME SPIKE PROTECTION =====
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and not dip_then_rip.get("override"):
                            mom_vol_spike = MomentumVolumeSpikeProtection.detect(
                                change_5m, latest_volume, volume_ma10,
                                liq["short_dist"], liq["long_dist"],
                                obv_trend, up_energy, down_energy
                            )
                            if mom_vol_spike["override"]:
                                final_bias = mom_vol_spike["bias"]
                                final_reason = mom_vol_spike["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "MOMENTUM_VOLUME_SPIKE_LOCK"
                                priority = mom_vol_spike["priority"]
                                prob_engine.add(mom_vol_spike["bias"], 10.015)

                        # ===== PRIORITY -1102: MARK PRICE GAP DETECTOR =====
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and not dip_then_rip.get("override"):
                            mark_gap = MarkPriceGapDetector.detect(
                                mark_price, price, funding_rate, change_5m
                            )
                            if mark_gap["override"]:
                                final_bias = mark_gap["bias"]
                                final_reason = mark_gap["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "MARK_PRICE_GAP"
                                priority = mark_gap["priority"]
                                prob_engine.add(mark_gap["bias"], 10.01)

                        # ===== DEFINE EXTREME FUNDING BAN BEFORE USE =====
                        extreme_funding_ban = ExtremeFundingRateLongBan.detect(
                            funding_rate, rsi6_5m, rsi14, change_5m
                        )

                        # ===== PRIORITY -1101: EXTREME FUNDING RATE LONG/SHORT BAN =====
                        if not has_extreme_override and not post_squeeze.get("override") and not profit_reversal.get("override") and not oversold_dist.get("override") and not dip_then_rip.get("override") and extreme_funding_ban["override"]:
                            final_bias = extreme_funding_ban["bias"]
                            final_reason = extreme_funding_ban["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_FUNDING_BAN"
                            priority = extreme_funding_ban["priority"]
                            prob_engine.add(extreme_funding_ban["bias"], 10.01)

                        # 1. MASTER SQUEEZE RULE (Priority -1100)
                        elif not oversold_dist.get("override") and master_squeeze["override"]:
                            final_bias = master_squeeze["bias"]
                            final_reason = master_squeeze["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "MASTER_SQUEEZE_RULE"
                            priority = master_squeeze["priority"]
                            prob_engine.add(master_squeeze["bias"], 10.0)

                        # 1.006. OBV DISTRIBUTION FUNDING TRAP (Priority -1100)
                        elif not oversold_dist.get("override") and OBVDistributionFundingTrap.detect(
                                obv_trend, obv_value,
                                funding_rate or 0.0, rsi6, rsi6_5m,
                                liq["long_dist"], liq["short_dist"],
                                agg, change_5m,
                                down_energy, volume_ratio)["override"]:
                            obv_fund_trap = OBVDistributionFundingTrap.detect(
                                obv_trend, obv_value,
                                funding_rate or 0.0, rsi6, rsi6_5m,
                                liq["long_dist"], liq["short_dist"],
                                agg, change_5m,
                                down_energy, volume_ratio
                            )
                            final_bias = obv_fund_trap["bias"]
                            final_reason = obv_fund_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OBV_DISTRIBUTION_FUNDING_TRAP"
                            priority = obv_fund_trap["priority"]
                            prob_engine.add(obv_fund_trap["bias"], 10.0)

                        # 1.005. BLOW-OFF TOP DETECTOR (Priority -1099)
                        elif not oversold_dist.get("override") and BlowOffTopDetector.detect(
                                rsi6_5m, rsi14, volume_ratio,
                                funding_rate or 0, change_5m,
                                liq["short_dist"], up_energy)["override"]:
                            blow_off = BlowOffTopDetector.detect(
                                rsi6_5m, rsi14, volume_ratio,
                                funding_rate or 0, change_5m,
                                liq["short_dist"], up_energy
                            )
                            final_bias = blow_off["bias"]
                            final_reason = blow_off["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "BLOW_OFF_TOP"
                            priority = blow_off["priority"]
                            prob_engine.add(blow_off["bias"], 9.998)

                        # 1.007. LIQUIDITY DIRECTION ABSOLUTE PRIORITY (Priority -1099)
                        elif LiquidityDirectionAbsolutePriority.detect(
                                liq["short_dist"], liq["long_dist"],
                                agg, ofi["bias"], ofi["strength"],
                                obv_trend, change_5m,
                                down_energy, up_energy,
                                funding_rate=funding_rate or 0.0,  # ← TAMBAH
                                obv_value=obv_value,               # ← TAMBAH
                                rsi6=rsi6,                         # ← TAMBAH
                                rsi6_5m=rsi6_5m                    # ← TAMBAH
                            )["override"]:
                            liq_dir = LiquidityDirectionAbsolutePriority.detect(
                                liq["short_dist"], liq["long_dist"],
                                agg, ofi["bias"], ofi["strength"],
                                obv_trend, change_5m,
                                down_energy, up_energy,
                                funding_rate=funding_rate or 0.0,  # ← TAMBAH
                                obv_value=obv_value,               # ← TAMBAH
                                rsi6=rsi6,                         # ← TAMBAH
                                rsi6_5m=rsi6_5m                    # ← TAMBAH
                            )
                            final_bias = liq_dir["bias"]
                            final_reason = liq_dir["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "LIQUIDITY_DIRECTION_PRIORITY"
                            priority = liq_dir["priority"]
                            prob_engine.add(liq_dir["bias"], 9.997)

                        # 1.01. ABSOLUTE AGG OVERRIDE (Priority -1098)
                        elif AbsoluteAggOverride.detect(
                                agg, down_energy, ofi["bias"], ofi["strength"],
                                up_energy, funding_rate or 0,
                                liq["long_dist"], liq["short_dist"], obv_value)["override"]:
                            absolute_agg = AbsoluteAggOverride.detect(
                                agg, down_energy, ofi["bias"], ofi["strength"],
                                up_energy, funding_rate or 0,
                                liq["long_dist"], liq["short_dist"], obv_value
                            )
                            final_bias = absolute_agg["bias"]
                            final_reason = absolute_agg["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "ABSOLUTE_AGG_OVERRIDE"
                            priority = absolute_agg["priority"]
                            prob_engine.add(absolute_agg["bias"], 9.995)

                        # 1.015. SHORT LIQ PROXIMITY + AGG SQUEEZE (Priority -1097/-1096/-1094)
                        elif short_liq_agg_squeeze["override"]:
                            final_bias = short_liq_agg_squeeze["bias"]
                            final_reason = short_liq_agg_squeeze["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "SHORT_LIQ_PROXIMITY_AGG_SQUEEZE"
                            priority = short_liq_agg_squeeze["priority"]
                            prob_engine.add(short_liq_agg_squeeze["bias"], 9.993)

                        # 1.02. DOWN ENERGY ZERO + SHORT LIQ CLOSE (Priority -1093)
                        elif down_energy_zero_close["override"]:
                            final_bias = down_energy_zero_close["bias"]
                            final_reason = down_energy_zero_close["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "DOWN_ENERGY_ZERO_SHORT_LIQ_CLOSE"
                            priority = down_energy_zero_close["priority"]
                            prob_engine.add(down_energy_zero_close["bias"], 9.991)

                        # 1.025. VOLUME SPIKE BOUNCE (Priority -1092)
                        elif VolumeSpikeBounceDetector.detect(
                                latest_volume, volume_ma10, rsi6,
                                up_energy, down_energy, agg,
                                change_5m, liq["long_dist"], liq["short_dist"])["override"]:
                            volume_spike_bounce = VolumeSpikeBounceDetector.detect(
                                latest_volume, volume_ma10, rsi6,
                                up_energy, down_energy, agg,
                                change_5m, liq["long_dist"], liq["short_dist"]
                            )
                            final_bias = volume_spike_bounce["bias"]
                            final_reason = volume_spike_bounce["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "VOLUME_SPIKE_BOUNCE"
                            priority = volume_spike_bounce["priority"]
                            prob_engine.add(volume_spike_bounce["bias"], 9.99)

                        # 1.03. TWO-PHASE HFT SWEEP (Priority -1095)
                        elif TwoPhaseHFTSweepDetector.detect(
                                liq["short_dist"], liq["long_dist"], rsi6,
                                down_energy, up_energy, change_5m,
                                ofi["bias"], ofi["strength"], volume_ratio)["override"]:
                            two_phase_hft = TwoPhaseHFTSweepDetector.detect(
                                liq["short_dist"], liq["long_dist"], rsi6,
                                down_energy, up_energy, change_5m,
                                ofi["bias"], ofi["strength"], volume_ratio
                            )
                            final_bias = two_phase_hft["bias"]
                            final_reason = two_phase_hft["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "TWO_PHASE_HFT_SWEEP"
                            priority = two_phase_hft["priority"]
                            prob_engine.add(two_phase_hft["bias"], 9.98)

                        # 1.05. HFT EXPLICIT DUMP (Priority -1090)
                        elif HFTExplicitDumpOverride.detect(
                                hft_6pct["bias"], hft_6pct["reason"],
                                agg, ofi["bias"], change_5m,
                                funding_rate or 0, volume_ratio)["override"]:
                            hft_dump = HFTExplicitDumpOverride.detect(
                                hft_6pct["bias"], hft_6pct["reason"],
                                agg, ofi["bias"], change_5m,
                                funding_rate or 0, volume_ratio
                            )
                            final_bias = hft_dump["bias"]
                            final_reason = hft_dump["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "HFT_EXPLICIT_DUMP"
                            priority = hft_dump["priority"]
                            prob_engine.add(hft_dump["bias"], 9.95)

                        # 1.055. FUNDING NEGATIVE + OBV POSITIVE SQUEEZE FIRST (Priority -1088)
                        elif FundingNegativeOBVPositiveSqueezeFirst.detect(
                                funding_rate, obv_trend, obv_value,
                                liq["short_dist"], liq["long_dist"],
                                rsi6_5m, down_energy, change_5m, up_energy)["override"]:
                            fund_obv_sqz = FundingNegativeOBVPositiveSqueezeFirst.detect(
                                funding_rate, obv_trend, obv_value,
                                liq["short_dist"], liq["long_dist"],
                                rsi6_5m, down_energy, change_5m, up_energy
                            )
                            final_bias = fund_obv_sqz["bias"]
                            final_reason = fund_obv_sqz["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FUNDING_NEG_OBV_POS_SQUEEZE_FIRST"
                            priority = fund_obv_sqz["priority"]
                            prob_engine.add(fund_obv_sqz["bias"], 9.93)

                        # 1.06. EXTREME FUNDING RATE TRAP (Priority -1085)
                        elif ExtremeFundingRateTrap.detect(
                                funding_rate or 0, agg, hft_6pct["bias"],
                                ofi["bias"], ofi["strength"],
                                change_5m, volume_ratio)["override"]:
                            extreme_funding = ExtremeFundingRateTrap.detect(
                                funding_rate or 0, agg, hft_6pct["bias"],
                                ofi["bias"], ofi["strength"],
                                change_5m, volume_ratio
                            )
                            final_bias = extreme_funding["bias"]
                            final_reason = extreme_funding["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_FUNDING_TRAP"
                            priority = extreme_funding["priority"]
                            prob_engine.add(extreme_funding["bias"], 9.9)

                        # 1.07. EXTREME FUNDING LIQUIDITY (Priority -1086)
                        elif ExtremeFundingLiquidityOverride.detect(
                                funding_rate or 0, liq["long_dist"], liq["short_dist"],
                                hft_6pct["bias"], agg, change_5m)["override"]:
                            extreme_funding_liq = ExtremeFundingLiquidityOverride.detect(
                                funding_rate or 0, liq["long_dist"], liq["short_dist"],
                                hft_6pct["bias"], agg, change_5m
                            )
                            final_bias = extreme_funding_liq["bias"]
                            final_reason = extreme_funding_liq["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_FUNDING_LIQUIDITY"
                            priority = extreme_funding_liq["priority"]
                            prob_engine.add(extreme_funding_liq["bias"], 9.95)

                        # 1.1. EXTREME OVERSOLD IGNORE LIQUIDITY (Priority -1080)
                        elif ExtremeOversoldIgnoreLiquidity.detect(rsi6, volume_ratio)["override"]:
                            extreme_oversold_ignore = ExtremeOversoldIgnoreLiquidity.detect(
                                rsi6, volume_ratio
                            )
                            final_bias = extreme_oversold_ignore["bias"]
                            final_reason = extreme_oversold_ignore["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_OVERSOLD_IGNORE_LIQUIDITY"
                            priority = extreme_oversold_ignore["priority"]
                            prob_engine.add(extreme_oversold_ignore["bias"], 9.9)

                        # 1.2. EXTREME OVERBOUGHT IGNORE LIQUIDITY (Priority -1080)
                        elif ExtremeOverboughtIgnoreLiquidity.detect(rsi6, volume_ratio)["override"]:
                            extreme_overbought_ignore = ExtremeOverboughtIgnoreLiquidity.detect(
                                rsi6, volume_ratio
                            )
                            final_bias = extreme_overbought_ignore["bias"]
                            final_reason = extreme_overbought_ignore["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_OVERBOUGHT_IGNORE_LIQUIDITY"
                            priority = extreme_overbought_ignore["priority"]
                            prob_engine.add(extreme_overbought_ignore["bias"], 9.9)

                        # 🔥 1.21. FRESH SHORT TRAP (Priority -1082) - NEW
                        elif FreshShortTrapDetector.detect(
                                change_5m, rsi6, ofi["bias"], volume_ratio,
                                liq["long_dist"], liq["short_dist"], agg, up_energy, down_energy)["override"]:
                            fresh_short_trap = FreshShortTrapDetector.detect(
                                change_5m, rsi6, ofi["bias"], volume_ratio,
                                liq["long_dist"], liq["short_dist"], agg, up_energy, down_energy
                            )
                            final_bias = fresh_short_trap["bias"]
                            final_reason = fresh_short_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FRESH_SHORT_TRAP"
                            priority = fresh_short_trap["priority"]
                            prob_engine.add(fresh_short_trap["bias"], 9.85)

                        # 1.3. CROWDED LONG DISTRIBUTION (Priority -165)
                        elif CrowdedLongDistribution.detect(
                                rsi6, volume_ratio, ofi["bias"], change_5m)["override"]:
                            crowded_long = CrowdedLongDistribution.detect(
                                rsi6, volume_ratio, ofi["bias"], change_5m
                            )
                            final_bias = crowded_long["bias"]
                            final_reason = crowded_long["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "CROWDED_LONG_DISTRIBUTION"
                            priority = crowded_long["priority"]
                            prob_engine.add(crowded_long["bias"], 4.5)

                        # 1.4. CROWDED SHORT ACCUMULATION (Priority -165)
                        elif CrowdedShortAccumulation.detect(
                                rsi6, volume_ratio, ofi["bias"], change_5m)["override"]:
                            crowded_short = CrowdedShortAccumulation.detect(
                                rsi6, volume_ratio, ofi["bias"], change_5m
                            )
                            final_bias = crowded_short["bias"]
                            final_reason = crowded_short["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "CROWDED_SHORT_ACCUMULATION"
                            priority = crowded_short["priority"]
                            prob_engine.add(crowded_short["bias"], 4.5)

                        # 1.5. HFT-ALGO CONSENSUS (Priority -170)
                        elif HFTAlgoConsensusOverride.detect(
                                algo_type["bias"], hft_6pct["bias"], volume_ratio, change_5m,
                                liq["short_dist"], liq["long_dist"])["override"]:
                            hft_algo_consensus = HFTAlgoConsensusOverride.detect(
                                algo_type["bias"], hft_6pct["bias"], volume_ratio, change_5m,
                                liq["short_dist"], liq["long_dist"]
                            )
                            final_bias = hft_algo_consensus["bias"]
                            final_reason = hft_algo_consensus["reason"]
                            final_confidence = hft_algo_consensus.get("confidence", "ABSOLUTE")
                            final_phase = "HFT_ALGO_CONSENSUS"
                            priority = hft_algo_consensus["priority"]
                            prob_engine.add(hft_algo_consensus["bias"], 9.0)

                        # 1.55. AGG/FLOW DIVERGENCE FILTER (Priority -175)
                        elif AggFlowDivergenceFilter.detect(
                                agg, change_5m, ofi["bias"], hft_6pct["bias"], volume_ratio)["override"]:
                            agg_divergence = AggFlowDivergenceFilter.detect(
                                agg, change_5m, ofi["bias"], hft_6pct["bias"], volume_ratio
                            )
                            final_bias = agg_divergence["bias"]
                            final_reason = agg_divergence["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "AGG_FLOW_DIVERGENCE"
                            priority = agg_divergence["priority"]
                            prob_engine.add(agg_divergence["bias"], 9.0)

                        # 1.6. EXHAUSTED LIQUIDITY REVERSAL (Priority -1060)
                        elif ExhaustedLiquidityReversal.detect(
                                liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"])["override"]:
                            exhausted_liquidity = ExhaustedLiquidityReversal.detect(
                                liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"]
                            )
                            final_bias = exhausted_liquidity["bias"]
                            final_reason = exhausted_liquidity["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXHAUSTED_LIQUIDITY_REVERSAL"
                            priority = exhausted_liquidity["priority"]
                            prob_engine.add(exhausted_liquidity["bias"], 9.6)

                        # 1.65. AGG CONFIRMED BOUNCE (Priority -1070)
                        elif AggConfirmedBounce.detect(
                                agg, rsi6, liq["long_dist"], liq["short_dist"],
                                up_energy, down_energy, change_5m)["override"]:
                            agg_confirmed = AggConfirmedBounce.detect(
                                agg, rsi6, liq["long_dist"], liq["short_dist"],
                                up_energy, down_energy, change_5m
                            )
                            final_bias = agg_confirmed["bias"]
                            final_reason = agg_confirmed["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "AGG_CONFIRMED_BOUNCE"
                            priority = agg_confirmed["priority"]
                            prob_engine.add(agg_confirmed["bias"], 9.75)

                        # 1.655. RSI 5m VS 1m DIVERGENCE (Priority -1068)
                        elif RSI5mVs1mDivergence.detect(
                                rsi6, rsi6_5m, down_energy, up_energy,
                                agg, funding_rate or 0)["override"]:
                            rsi_divergence = RSI5mVs1mDivergence.detect(
                                rsi6, rsi6_5m, down_energy, up_energy,
                                agg, funding_rate or 0
                            )
                            final_bias = rsi_divergence["bias"]
                            final_reason = rsi_divergence["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "RSI_5m_VS_1m_DIVERGENCE"
                            priority = rsi_divergence["priority"]
                            prob_engine.add(rsi_divergence["bias"], 9.73)

                        # 1.66. AGG MAJORITY BOUNCE (Priority -1072)
                        elif AggMajorityBounce.detect(
                                agg, rsi6, liq["long_dist"], change_5m,
                                up_energy, down_energy)["override"]:
                            agg_majority = AggMajorityBounce.detect(
                                agg, rsi6, liq["long_dist"], change_5m,
                                up_energy, down_energy
                            )
                            final_bias = agg_majority["bias"]
                            final_reason = agg_majority["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "AGG_MAJORITY_BOUNCE"
                            priority = agg_majority["priority"]
                            prob_engine.add(agg_majority["bias"], 9.7)

                        # 1.7. FUNDING RATE CROWDED SHORT (Priority -1076)
                        elif FundingRateCrowdedShortOverride.detect(
                                funding_rate, rsi6, ofi["bias"], ofi["strength"],
                                liq["long_dist"], liq["short_dist"], volume_ratio)["override"]:
                            funding_crowded = FundingRateCrowdedShortOverride.detect(
                                funding_rate, rsi6, ofi["bias"], ofi["strength"],
                                liq["long_dist"], liq["short_dist"], volume_ratio
                            )
                            final_bias = funding_crowded["bias"]
                            final_reason = funding_crowded["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FUNDING_CROWDED_SHORT"
                            priority = funding_crowded["priority"]
                            prob_engine.add(funding_crowded["bias"], 9.8)

                        # 1.55b. SHORT SQUEEZE TRAP OVERRIDE (Priority -1060)
                        elif ShortSqueezeTrapOverride.detect(
                                liq["short_dist"], liq["long_dist"], up_energy, down_energy,
                                volume_ratio, rsi6_5m, ofi["bias"], ofi["strength"], change_5m)["override"]:
                            squeeze_trap = ShortSqueezeTrapOverride.detect(
                                liq["short_dist"], liq["long_dist"], up_energy, down_energy,
                                volume_ratio, rsi6_5m, ofi["bias"], ofi["strength"], change_5m
                            )
                            final_bias = squeeze_trap["bias"]
                            final_reason = squeeze_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "SHORT_SQUEEZE_TRAP_OVERRIDE"
                            priority = squeeze_trap["priority"]
                            prob_engine.add(squeeze_trap["bias"], 9.6)

                        # 1.6b. NEAR EXHAUSTED LIQUIDITY REVERSAL (Priority -1055)
                        elif NearExhaustedLiquidityReversal.detect(
                                liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"])["override"]:
                            near_exhausted = NearExhaustedLiquidityReversal.detect(
                                liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"]
                            )
                            final_bias = near_exhausted["bias"]
                            final_reason = near_exhausted["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "NEAR_EXHAUSTED_LIQUIDITY_REVERSAL"
                            priority = near_exhausted["priority"]
                            prob_engine.add(near_exhausted["bias"], 9.7)

                        # 1.7b. STRICT LIQUIDITY PROXIMITY (Priority -1050)
                        elif LiquidityProximityStrict.detect(
                                liq["short_dist"], liq["long_dist"], volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"], rsi6, obv_trend, change_5m)["override"]:
                            strict_liq = LiquidityProximityStrict.detect(
                                liq["short_dist"], liq["long_dist"], volume_ratio, rsi6_5m,
                                ofi["bias"], ofi["strength"], rsi6, obv_trend, change_5m
                            )
                            final_bias = strict_liq["bias"]
                            final_reason = strict_liq["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "STRICT_LIQUIDITY"
                            priority = strict_liq["priority"]
                            prob_engine.add(strict_liq["bias"], 9.5)

                        # 1.7c. LIQUIDITY MAGNET OVERRIDE (Priority -1075)
                        elif LiquidityMagnetOverride.detect(
                                liq["short_dist"], liq["long_dist"], volume_ratio,
                                rsi6_5m, change_5m)["override"]:
                            liq_magnet_override = LiquidityMagnetOverride.detect(
                                liq["short_dist"], liq["long_dist"], volume_ratio,
                                rsi6_5m, change_5m
                            )
                            final_bias = liq_magnet_override["bias"]
                            final_reason = liq_magnet_override["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "LIQUIDITY_MAGNET_OVERRIDE"
                            priority = liq_magnet_override["priority"]
                            prob_engine.add(liq_magnet_override["bias"], 9.8)

                        # 2. LIQUIDITY MAGNET CONTINUATION (Priority -1000)
                        elif LiquidityMagnetContinuation.detect(
                                liq["short_dist"], liq["long_dist"], change_5m,
                                up_energy, down_energy, volume_ratio)["override"]:
                            liq_magnet = LiquidityMagnetContinuation.detect(
                                liq["short_dist"], liq["long_dist"], change_5m,
                                up_energy, down_energy, volume_ratio
                            )
                            final_bias = liq_magnet["bias"]
                            final_reason = liq_magnet["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "LIQUIDITY_MAGNET_CONTINUATION"
                            priority = liq_magnet["priority"]
                            prob_engine.add(liq_magnet["bias"], 9.0)

                        # 3. OFI ABSORPTION SQUEEZE (Priority -950)
                        elif OFIAbsorptionSqueeze.detect(
                                ofi["bias"], ofi["strength"], change_5m,
                                liq["short_dist"], liq["long_dist"])["override"]:
                            ofi_absorption = OFIAbsorptionSqueeze.detect(
                                ofi["bias"], ofi["strength"], change_5m,
                                liq["short_dist"], liq["long_dist"]
                            )
                            final_bias = ofi_absorption["bias"]
                            final_reason = ofi_absorption["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OFI_ABSORPTION_SQUEEZE"
                            priority = ofi_absorption["priority"]
                            prob_engine.add(ofi_absorption["bias"], 8.5)

                        # 4. VELOCITY DECAY REVERSAL (Priority -900)
                        elif VelocityDecayReversal.detect(
                                change_5m, change_30s,
                                liq["short_dist"], liq["long_dist"])["override"]:
                            velocity_decay = VelocityDecayReversal.detect(
                                change_5m, change_30s,
                                liq["short_dist"], liq["long_dist"]
                            )
                            final_bias = velocity_decay["bias"]
                            final_reason = velocity_decay["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "VELOCITY_DECAY_REVERSAL"
                            priority = velocity_decay["priority"]
                            prob_engine.add(velocity_decay["bias"], 8.0)

                        # 4.5. RSI-ENERGY DIVERGENCE (Priority -856)
                        elif RSIEnergyDivergence.detect(
                                rsi6, up_energy, down_energy,
                                liq["short_dist"], liq["long_dist"])["override"]:
                            rsi_energy_div = RSIEnergyDivergence.detect(
                                rsi6, up_energy, down_energy,
                                liq["short_dist"], liq["long_dist"]
                            )
                            final_bias = rsi_energy_div["bias"]
                            final_reason = rsi_energy_div["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "RSI_ENERGY_DIVERGENCE"
                            priority = rsi_energy_div["priority"]
                            prob_engine.add(rsi_energy_div["bias"], 7.6)

                        # 4.6. OFI DIVERGENCE TRAP (Priority -855)
                        elif OFIDivergenceTrap.detect(
                                ofi["bias"], ofi["strength"], down_energy,
                                change_5m, liq["short_dist"], volume_ratio)["override"]:
                            ofi_div_trap = OFIDivergenceTrap.detect(
                                ofi["bias"], ofi["strength"], down_energy,
                                change_5m, liq["short_dist"], volume_ratio
                            )
                            final_bias = ofi_div_trap["bias"]
                            final_reason = ofi_div_trap["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "OFI_DIVERGENCE_TRAP"
                            priority = ofi_div_trap["priority"]
                            prob_engine.add(ofi_div_trap["bias"], 7.55)

                        # 5. EMPTY BOOK MOMENTUM (Priority -850)
                        elif EmptyBookMomentum.detect(
                                down_energy, up_energy, change_5m,
                                liq["short_dist"], liq["long_dist"])["override"]:
                            empty_book_mom = EmptyBookMomentum.detect(
                                down_energy, up_energy, change_5m,
                                liq["short_dist"], liq["long_dist"]
                            )
                            final_bias = empty_book_mom["bias"]
                            final_reason = empty_book_mom["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EMPTY_BOOK_MOMENTUM"
                            priority = empty_book_mom["priority"]
                            prob_engine.add(empty_book_mom["bias"], 7.5)

                        # 6. SQUEEZE CONTINUATION (Priority -265)
                        elif SqueezeContinuationDetector.detect(
                                rsi6_5m, change_5m, volume_ratio,
                                liq["short_dist"], up_energy, down_energy,
                                ofi["bias"], ofi["strength"], bid_slope, ask_slope)["override"]:
                            squeeze_cont = SqueezeContinuationDetector.detect(
                                rsi6_5m, change_5m, volume_ratio,
                                liq["short_dist"], up_energy, down_energy,
                                ofi["bias"], ofi["strength"], bid_slope, ask_slope
                            )
                            final_bias = squeeze_cont["bias"]
                            final_reason = squeeze_cont["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "SQUEEZE_CONTINUATION"
                            priority = squeeze_cont["priority"]
                            prob_engine.add(squeeze_cont["bias"], 5.0)

                        # 6.5. FLUSH EXHAUSTION REVERSAL (Priority -250)
                        elif FlushExhaustionReversal.detect(
                                change_5m, rsi6, volume_ratio,
                                down_energy, liq["long_dist"])["override"]:
                            flush_exhaust = FlushExhaustionReversal.detect(
                                change_5m, rsi6, volume_ratio,
                                down_energy, liq["long_dist"]
                            )
                            final_bias = flush_exhaust["bias"]
                            final_reason = flush_exhaust["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FLUSH_EXHAUSTION"
                            priority = flush_exhaust["priority"]
                            prob_engine.add(flush_exhaust["bias"], 4.0)

                        # 7. CASCADE DUMP
                        elif CascadeDumpDetector.detect(
                                change_5m, liq["short_dist"], down_energy, volume_ratio)["override"]:
                            cascade = CascadeDumpDetector.detect(
                                change_5m, liq["short_dist"], down_energy, volume_ratio
                            )
                            final_bias = cascade["bias"]
                            final_reason = cascade["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "CASCADE_DUMP"
                            priority = cascade["priority"]
                            prob_engine.add(cascade["bias"], 5.0)

                        # ===== PRIORITY -105: INSURANCE FUND PROTECTION =====
                        insurance_signal = InsuranceFundProtection.detect(
                            change_5m, volume_ratio, oi_delta, funding_rate,
                            liq["short_dist"], liq["long_dist"]
                        )
                        if insurance_signal["override"]:
                            final_bias = insurance_signal["bias"]
                            final_reason = insurance_signal["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "INSURANCE_FUND_PROTECT"
                            priority = insurance_signal["priority"]
                            prob_engine.add(insurance_signal["bias"], 2.5)

                        # ===== PRIORITY -104: ADL RISK SCORING =====
                        adl_signal = ADLRiskScoring.score(
                            funding_rate, volume_ratio, rsi6, change_5m,
                            liq["short_dist"], liq["long_dist"]
                        )
                        if adl_signal.get("override"):
                            final_bias = adl_signal["bias"]
                            final_reason = adl_signal["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "ADL_RISK_SIGNAL"
                            priority = adl_signal["priority"]
                            prob_engine.add(adl_signal["bias"], 2.0)

                        # ===== PRIORITY -103: EXCHANGE VOLATILITY CONTROL =====
                        vol_control = ExchangeVolatilityControl.detect(
                            volume_ratio, change_5m, liq["short_dist"], liq["long_dist"],
                            funding_rate, rsi6
                        )
                        if vol_control["override"]:
                            final_bias = vol_control["bias"]
                            final_reason = vol_control["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXCHANGE_VOL_CONTROL"
                            priority = vol_control["priority"]
                            prob_engine.add(vol_control["bias"], 1.5)

                        # 8. LOW VOLUME CONTINUATION
                        elif LowVolumeContinuation.detect(
                                volume_ratio, obv_trend, price, ma25, ma99, down_energy)["override"]:
                            low_vol_cont = LowVolumeContinuation.detect(
                                volume_ratio, obv_trend, price, ma25, ma99, down_energy
                            )
                            final_bias = low_vol_cont["bias"]
                            final_reason = low_vol_cont["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "LOW_VOL_CONT"
                            priority = low_vol_cont["priority"]
                            prob_engine.add(low_vol_cont["bias"], 4.0)

                        # 9. FAKE BOUNCE TRAP
                        elif FakeBounceTrap.detect(
                                rsi6, change_5m, volume_ratio,
                                liq["short_dist"], liq["long_dist"],
                                up_energy, down_energy,
                                ofi["bias"], ofi["strength"])["override"]:
                            fake_bounce = FakeBounceTrap.detect(
                                rsi6, change_5m, volume_ratio,
                                liq["short_dist"], liq["long_dist"],
                                up_energy, down_energy,
                                ofi["bias"], ofi["strength"]
                            )
                            final_bias = fake_bounce["bias"]
                            final_reason = fake_bounce["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "FAKE_BOUNCE"
                            priority = fake_bounce["priority"]
                            prob_engine.add(fake_bounce["bias"], 4.0)

                        # 10. POST DROP BOUNCE (Priority -140)
                        elif PostDropBounceOverride.detect(
                                change_5m, volume_ratio, ofi["bias"],
                                ofi["strength"], liq["short_dist"])["override"]:
                            post_drop_bounce = PostDropBounceOverride.detect(
                                change_5m, volume_ratio, ofi["bias"],
                                ofi["strength"], liq["short_dist"]
                            )
                            final_bias = post_drop_bounce["bias"]
                            final_reason = post_drop_bounce["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "POST_DROP_BOUNCE"
                            priority = post_drop_bounce["priority"]
                            prob_engine.add(post_drop_bounce["bias"], 3.5)

                        else:
                            # ===== LOWER PRIORITY CHECKS =====
                            flush = LiquidityFlushConfirmation.detect(
                                liq["short_dist"], liq["long_dist"], agg
                            )
                            if flush["wait"]:
                                return self._build_result(
                                    price, rsi6, rsi14, stoch_k, stoch_d, obv_trend, obv_value,
                                    volume_ratio, change_5m, liq, up_energy, down_energy,
                                    agg, flow, "WAIT", "ABSOLUTE", flush["reason"],
                                    "FLUSH_CONFIRMATION", -255, ofi, iceberg, funding_trap, liq_heat,
                                    cross_lead, None, funding_rate, latest_volume, volume_ma10, rsi6_5m
                                )

                            dead_market = DeadMarketProximityRule.detect(
                                agg, flow, liq["short_dist"], liq["long_dist"],
                                up_energy, down_energy
                            )
                            if dead_market["override"]:
                                final_bias = dead_market["bias"]
                                final_reason = dead_market["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "DEAD_MARKET"
                                priority = dead_market["priority"]
                                prob_engine.add(dead_market["bias"], 3.0)

                            elif ExtremeOversoldReversalFilter.detect(
                                    rsi6, rsi14, stoch_k, obv_value, obv_trend,
                                    liq["long_dist"], down_energy,
                                    ofi["bias"], ofi["strength"], change_5m)["override"]:
                                extreme_oversold = ExtremeOversoldReversalFilter.detect(
                                    rsi6, rsi14, stoch_k, obv_value, obv_trend,
                                    liq["long_dist"], down_energy,
                                    ofi["bias"], ofi["strength"], change_5m
                                )
                                final_bias = extreme_oversold["bias"]
                                final_reason = extreme_oversold["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "EXTREME_OVERSOLD_REVERSAL"
                                priority = extreme_oversold["priority"]
                                prob_engine.add(extreme_oversold["bias"], 3.0)

                            elif PanicDropExhaustionDetector.detect(
                                    change_5m, volume_ratio, rsi6, down_energy, obv_trend)["override"]:
                                panic_exhaustion = PanicDropExhaustionDetector.detect(
                                    change_5m, volume_ratio, rsi6, down_energy, obv_trend
                                )
                                final_bias = panic_exhaustion["bias"]
                                final_reason = panic_exhaustion["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "PANIC_EXHAUSTION"
                                priority = panic_exhaustion["priority"]
                                prob_engine.add(panic_exhaustion["bias"], 3.0)

                            elif ShortSqueezeTrapDetector.detect(
                                    liq["long_dist"], rsi6, ofi["bias"], ofi["strength"],
                                    down_energy, agg, flow)["override"]:
                                short_squeeze = ShortSqueezeTrapDetector.detect(
                                    liq["long_dist"], rsi6, ofi["bias"], ofi["strength"],
                                    down_energy, agg, flow
                                )
                                final_bias = short_squeeze["bias"]
                                final_reason = short_squeeze["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "SHORT_SQUEEZE_TRAP"
                                priority = short_squeeze["priority"]
                                prob_engine.add(short_squeeze["bias"], 3.0)

                            elif HFTTrapDetector.detect_fake_energy(
                                    down_energy, up_energy, change_5m, volume_ratio, rsi14,
                                    liq["short_dist"], liq["long_dist"], rsi6_5m, rsi6)["override"]:
                                fake_energy = HFTTrapDetector.detect_fake_energy(
                                    down_energy, up_energy, change_5m, volume_ratio, rsi14,
                                    liq["short_dist"], liq["long_dist"], rsi6_5m, rsi6
                                )
                                final_bias = fake_energy["bias"]
                                final_reason = fake_energy["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "FAKE_ENERGY_TRAP"
                                priority = fake_energy["priority"]
                                prob_engine.add(fake_energy["bias"], 4.0)

                            elif OversoldContinuation.detect(
                                    rsi6, obv_trend, price, ma25, ma99, volume_ratio,
                                    down_energy, ofi["bias"], ofi["strength"], liq["long_dist"])["override"]:
                                oversold_cont = OversoldContinuation.detect(
                                    rsi6, obv_trend, price, ma25, ma99, volume_ratio,
                                    down_energy, ofi["bias"], ofi["strength"], liq["long_dist"]
                                )
                                final_bias = oversold_cont["bias"]
                                final_reason = oversold_cont["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "OVERSOLD_CONT"
                                priority = oversold_cont["priority"]
                                prob_engine.add(oversold_cont["bias"], 3.0)

                            elif OversoldBounce.detect(
                                    rsi6, obv_trend, down_energy, liq["long_dist"],
                                    price, liq["recent_low"], up_energy, ma25, ma99,
                                    ofi["bias"], ofi["strength"], volume_ratio)["override"]:
                                oversold_bounce = OversoldBounce.detect(
                                    rsi6, obv_trend, down_energy, liq["long_dist"],
                                    price, liq["recent_low"], up_energy, ma25, ma99,
                                    ofi["bias"], ofi["strength"], volume_ratio
                                )
                                final_bias = oversold_bounce["bias"]
                                final_reason = oversold_bounce["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "OVERSOLD_BOUNCE"
                                priority = oversold_bounce["priority"]
                                prob_engine.add(oversold_bounce["bias"], 3.0)

                            elif OFIExtremeOversoldConfirm.detect(
                                    rsi6, ofi["bias"], ofi["strength"],
                                    liq["long_dist"], down_energy, up_energy,
                                    volume_ratio)["override"]:
                                ofi_extreme = OFIExtremeOversoldConfirm.detect(
                                    rsi6, ofi["bias"], ofi["strength"],
                                    liq["long_dist"], down_energy, up_energy,
                                    volume_ratio
                                )
                                final_bias = ofi_extreme["bias"]
                                final_reason = ofi_extreme["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "OFI_EXTREME_CONFIRM"
                                priority = ofi_extreme["priority"]
                                prob_engine.add(ofi_extreme["bias"], 3.0)

                            elif StrongBearishOverride.detect(
                                    rsi6, obv_trend, price, ma25, ma99, volume_ratio, down_energy)["override"]:
                                strong_bearish = StrongBearishOverride.detect(
                                    rsi6, obv_trend, price, ma25, ma99, volume_ratio, down_energy
                                )
                                final_bias = strong_bearish["bias"]
                                final_reason = strong_bearish["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "STRONG_BEARISH"
                                priority = strong_bearish["priority"]
                                prob_engine.add(strong_bearish["bias"], 3.0)

                            elif OFIConflictFilter.detect(
                                    ofi["bias"], ofi["strength"],
                                    liq["short_dist"], liq["long_dist"],
                                    up_energy, down_energy, rsi6, change_5m)["override"]:
                                ofi_conflict = OFIConflictFilter.detect(
                                    ofi["bias"], ofi["strength"],
                                    liq["short_dist"], liq["long_dist"],
                                    up_energy, down_energy, rsi6, change_5m
                                )
                                final_bias = ofi_conflict["bias"]
                                final_reason = ofi_conflict["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "OFI_CONFLICT"
                                priority = ofi_conflict["priority"]
                                prob_engine.add(ofi_conflict["bias"], 3.0)

                            elif LiquidityPriorityEnergyCheck.detect(
                                    liq["short_dist"], liq["long_dist"],
                                    up_energy, down_energy, change_5m)["override"]:
                                liq_priority_energy = LiquidityPriorityEnergyCheck.detect(
                                    liq["short_dist"], liq["long_dist"],
                                    up_energy, down_energy, change_5m
                                )
                                final_bias = liq_priority_energy["bias"]
                                final_reason = liq_priority_energy["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "LIQUIDITY_PRIORITY_ENERGY_CHECK"
                                priority = liq_priority_energy["priority"]
                                prob_engine.add(liq_priority_energy["bias"], 3.0)

                            elif OverboughtLiquidityTrap.detect(
                                    liq["short_dist"], liq["long_dist"],
                                    rsi6, up_energy, down_energy,
                                    ofi["bias"], ofi["strength"], volume_ratio,
                                    funding_rate or 0)["override"]:
                                overbought_trap_old = OverboughtLiquidityTrap.detect(
                                    liq["short_dist"], liq["long_dist"],
                                    rsi6, up_energy, down_energy,
                                    ofi["bias"], ofi["strength"], volume_ratio,
                                    funding_rate or 0
                                )
                                final_bias = overbought_trap_old["bias"]
                                final_reason = overbought_trap_old["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "OVERBOUGHT_LIQ_TRAP"
                                priority = overbought_trap_old["priority"]
                                prob_engine.add(overbought_trap_old["bias"], 3.0)

                            else:
                                # LIQUIDITY PRIORITY OVERRIDE
                                liq_priority = LiquidityPriorityOverride.detect(
                                    liq["short_dist"], liq["long_dist"], volume_ratio, rsi6_5m,
                                    rsi6, ofi["bias"], ofi["strength"]
                                )
                                if liq_priority["override"]:
                                    bait = LiquidityBaitDetector.detect(
                                        liq["short_dist"], liq["long_dist"],
                                        up_energy, down_energy, agg, flow, volume_ratio
                                    )
                                    if bait["override"]:
                                        final_bias = bait["bias"]
                                        final_reason = bait["reason"]
                                        final_confidence = "ABSOLUTE"
                                        final_phase = "LIQUIDITY_BAIT"
                                        priority = bait["priority"]
                                        prob_engine.add(bait["bias"], 3.0)
                                    else:
                                        final_bias = liq_priority["bias"]
                                        final_reason = liq_priority["reason"]
                                        final_confidence = "ABSOLUTE"
                                        final_phase = "LIQUIDITY_PRIORITY"
                                        priority = liq_priority["priority"]
                                        prob_engine.add(liq_priority["bias"], 3.0)

                                elif LiquidityEnergyCheck.detect(
                                        liq["short_dist"], liq["long_dist"],
                                        up_energy, down_energy,
                                        volume_ratio, ofi["bias"], ofi["strength"], rsi6_5m,
                                        obv_magnitude)["override"]:
                                    liq_energy = LiquidityEnergyCheck.detect(
                                        liq["short_dist"], liq["long_dist"],
                                        up_energy, down_energy,
                                        volume_ratio, ofi["bias"], ofi["strength"], rsi6_5m,
                                        obv_magnitude
                                    )
                                    final_bias = liq_energy["bias"]
                                    final_reason = liq_energy["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "LIQUIDITY_ENERGY_TRAP"
                                    priority = liq_energy["priority"]
                                    prob_engine.add(liq_energy["bias"], 3.0)

                                elif ExtremeEnergyImbalance.detect(
                                        up_energy, down_energy, volume_ratio, rsi14,
                                        change_5m, ofi["bias"], ofi["strength"],
                                        rsi6, rsi6_5m)["override"]:
                                    energy_imbalance = ExtremeEnergyImbalance.detect(
                                        up_energy, down_energy, volume_ratio, rsi14,
                                        change_5m, ofi["bias"], ofi["strength"],
                                        rsi6, rsi6_5m
                                    )
                                    final_bias = energy_imbalance["bias"]
                                    final_reason = energy_imbalance["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "ENERGY_IMBALANCE"
                                    priority = energy_imbalance["priority"]
                                    prob_engine.add(energy_imbalance["bias"], 3.0)

                                elif ThinOrderBookPump.detect(
                                        up_energy, down_energy, change_5m, volume_ratio,
                                        ofi["bias"], ofi["strength"], liq["short_dist"])["override"]:
                                    thin_pump = ThinOrderBookPump.detect(
                                        up_energy, down_energy, change_5m, volume_ratio,
                                        ofi["bias"], ofi["strength"], liq["short_dist"]
                                    )
                                    final_bias = thin_pump["bias"]
                                    final_reason = thin_pump["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "THIN_ORDER_BOOK_PUMP"
                                    priority = thin_pump["priority"]
                                    prob_engine.add(thin_pump["bias"], 3.0)

                                elif PumpExhaustionTrap.detect(
                                        change_5m, volume_ratio, down_energy,
                                        liq["long_dist"], liq["short_dist"], rsi6)["override"]:
                                    pump_exhaust = PumpExhaustionTrap.detect(
                                        change_5m, volume_ratio, down_energy,
                                        liq["long_dist"], liq["short_dist"], rsi6
                                    )
                                    final_bias = pump_exhaust["bias"]
                                    final_reason = pump_exhaust["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "PUMP_EXHAUSTION_TRAP"
                                    priority = pump_exhaust["priority"]
                                    prob_engine.add(pump_exhaust["bias"], 3.0)

                                elif EnergyTrapFilter.detect(
                                        up_energy, down_energy, change_5m, volume_ratio, rsi14,
                                        liq["short_dist"], rsi6_5m)["override"]:
                                    energy_trap = EnergyTrapFilter.detect(
                                        up_energy, down_energy, change_5m, volume_ratio, rsi14,
                                        liq["short_dist"], rsi6_5m
                                    )
                                    final_bias = energy_trap["bias"]
                                    final_reason = energy_trap["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "ENERGY_TRAP"
                                    priority = energy_trap["priority"]
                                    prob_engine.add(energy_trap["bias"], 3.0)

                                elif EnergyGapTrapDetector.detect(rsi14, up_energy, down_energy)["override"]:
                                    energy_gap = EnergyGapTrapDetector.detect(rsi14, up_energy, down_energy)
                                    final_bias = energy_gap["bias"]
                                    final_reason = energy_gap["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "ENERGY_GAP_TRAP"
                                    priority = energy_gap["priority"]
                                    prob_engine.add(energy_gap["bias"], 3.0)

                                else:
                                    # ===== FALLBACK TO PROBABILISTIC VOTING =====
                                    agg_boost = 1.0
                                    if agg > 0.85 or agg < 0.15:
                                        agg_boost = 3.0

                                    prob_engine.add(ofi["bias"], ofi["strength"] * 2.0 * agg_boost)

                                    energy_bias = "LONG" if up_energy < down_energy else "SHORT"
                                    energy_weight = 1.0 * agg_boost
                                    prob_engine.add(energy_bias, energy_weight)

                                    if liq["short_dist"] < liq["long_dist"]:
                                        prob_engine.add("LONG", 1.0)
                                    else:
                                        prob_engine.add("SHORT", 1.0)

                                    wmi = self.fetcher.calculate_wmi(
                                        liq["short_dist"], liq["long_dist"]
                                    )
                                    if wmi > 20:
                                        prob_engine.add("LONG", 0.5)
                                    elif wmi < -20:
                                        prob_engine.add("SHORT", 0.5)

                                    if rsi6 > 50 and stoch_k > stoch_d:
                                        prob_engine.add("LONG", 0.5)
                                    elif rsi6 < 50 and stoch_k < stoch_d:
                                        prob_engine.add("SHORT", 0.5)

                                    algo_type = AlgoTypeAnalyzer.analyze(
                                        order_book, trades, price,
                                        liq["short_dist"], liq["long_dist"],
                                        up_energy, down_energy
                                    )
                                    prob_engine.add(algo_type["bias"], 1.2)

                                    hft_6pct = HFT6PercentDirection.determine(
                                        price, liq["short_dist"], liq["long_dist"],
                                        up_energy, down_energy, oi_delta, agg, flow
                                    )
                                    hft_weight = 1.5
                                    if liq["short_dist"] < 2.0 or liq["long_dist"] < 2.0:
                                        hft_weight *= 2.0
                                    prob_engine.add(hft_6pct["bias"], hft_weight)

                                    if slope_signal["bias"] != "NEUTRAL":
                                        prob_engine.add(slope_signal["bias"], 1.0)

                                    if funding_trap["bias"] != "NEUTRAL":
                                        prob_engine.add(funding_trap["bias"], 2.0)

                                    prob_bias, prob_conf = prob_engine.result()

                                    # Voting system
                                    strategy_signals = {}
                                    if liq["short_dist"] < liq["long_dist"]:
                                        strategy_signals["liquidity_proximity"] = "LONG"
                                    else:
                                        strategy_signals["liquidity_proximity"] = "SHORT"
                                    if up_energy < down_energy:
                                        strategy_signals["energy"] = "LONG"
                                    else:
                                        strategy_signals["energy"] = "SHORT"
                                    if wmi > 20:
                                        strategy_signals["distribution"] = "LONG"
                                    elif wmi < -20:
                                        strategy_signals["distribution"] = "SHORT"
                                    if rsi6 > 50 and stoch_k > stoch_d:
                                        strategy_signals["momentum"] = "LONG"
                                    elif rsi6 < 50 and stoch_k < stoch_d:
                                        strategy_signals["momentum"] = "SHORT"
                                    if algo_type["bias"] != "NEUTRAL":
                                        strategy_signals["algo_type"] = algo_type["bias"]
                                    if hft_6pct["bias"] != "NEUTRAL":
                                        strategy_signals["hft_6pct"] = hft_6pct["bias"]
                                    if ofi["bias"] != "NEUTRAL":
                                        strategy_signals["ofi"] = ofi["bias"]

                                    self.voter.update_weights({"agg": agg, "flow": flow})
                                    vote_result = self.voter.vote(strategy_signals)

                                    # Combine probabilistic with voting
                                    if prob_bias != "NEUTRAL" and vote_result["bias"] != "NEUTRAL":
                                        if prob_bias == vote_result["bias"]:
                                            final_bias = prob_bias
                                            final_reason = f"Probabilistic ({prob_conf:.1%}) + Voting consensus"
                                            final_confidence = "ABSOLUTE" if prob_conf > 0.7 else "HIGH"
                                        else:
                                            if prob_conf > vote_result["confidence"]:
                                                final_bias = prob_bias
                                                final_reason = (
                                                    f"Probabilistic override ({prob_conf:.1%}) "
                                                    f"over voting ({vote_result['confidence']:.1%})"
                                                )
                                            else:
                                                final_bias = vote_result["bias"]
                                                final_reason = (
                                                    f"Voting override ({vote_result['confidence']:.1%}) "
                                                    f"over probabilistic ({prob_conf:.1%})"
                                                )
                                            final_confidence = (
                                                "ABSOLUTE"
                                                if max(prob_conf, vote_result["confidence"]) > 0.7
                                                else "HIGH"
                                            )
                                    elif prob_bias != "NEUTRAL":
                                        final_bias = prob_bias
                                        final_reason = f"Probabilistic engine: {prob_conf:.1%}"
                                        final_confidence = "ABSOLUTE" if prob_conf > 0.7 else "HIGH"
                                    elif vote_result["bias"] != "NEUTRAL":
                                        final_bias = vote_result["bias"]
                                        final_reason = f"Voting: {vote_result['confidence']:.1%}"
                                        final_confidence = (
                                            "ABSOLUTE"
                                            if vote_result["confidence"] > 0.7
                                            else "HIGH"
                                        )
                                    else:
                                        final_bias = (
                                            "LONG"
                                            if liq["short_dist"] < liq["long_dist"]
                                            else "SHORT"
                                        )
                                        final_reason = "Fallback to liquidity proximity"
                                        final_confidence = "MEDIUM"

                                    final_phase = "PROBABILISTIC_VOTING"
                                    priority = 0

            # ========== EXTREME OVERBOUGHT/OVERSOLD CONTINUATION ==========
            extreme_oversold_short = ExtremeOversoldShortContinuation.detect(
                rsi6, volume_ratio, ofi["bias"], ofi["strength"], down_energy, liq["long_dist"]
            )
            if extreme_oversold_short["override"]:
                final_bias = extreme_oversold_short["bias"]
                final_reason = extreme_oversold_short["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXTREME_OVERSOLD_SHORT"
                priority = extreme_oversold_short["priority"]
            else:
                extreme_overbought_long = ExtremeOverboughtLongContinuation.detect(
                    rsi6, volume_ratio, ofi["bias"], ofi["strength"], up_energy, liq["short_dist"]
                )
                if extreme_overbought_long["override"]:
                    final_bias = extreme_overbought_long["bias"]
                    final_reason = extreme_overbought_long["reason"]
                    final_confidence = "ABSOLUTE"
                    final_phase = "EXTREME_OVERBOUGHT_LONG"
                    priority = extreme_overbought_long["priority"]
                else:
                    extreme_overbought_cont = ExtremeOverboughtContinuation.detect(
                        rsi6_5m, volume_ratio, ofi["bias"], ofi["strength"],
                        up_energy, liq["short_dist"],
                        funding_rate=funding_rate or 0.0, rsi14=rsi14, rsi6=rsi6
                    )
                    if extreme_overbought_cont["override"]:
                        final_bias = extreme_overbought_cont["bias"]
                        final_reason = extreme_overbought_cont["reason"]
                        final_confidence = "ABSOLUTE"
                        final_phase = "EXTREME_OVERBOUGHT_CONT"
                        priority = extreme_overbought_cont["priority"]
                    else:
                        extreme_oversold_cont = ExtremeOversoldContinuation.detect(
                            rsi6_5m, volume_ratio, ofi["bias"], ofi["strength"],
                            down_energy, liq["long_dist"]
                        )
                        if extreme_oversold_cont["override"]:
                            final_bias = extreme_oversold_cont["bias"]
                            final_reason = extreme_oversold_cont["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_OVERSOLD_CONT"
                            priority = extreme_oversold_cont["priority"]

            # ========== EXTREME OVERSOLD/OVERBOUGHT BOUNCE/DUMP OVERRIDE ==========
            extreme_oversold_bounce = ExtremeOversoldBounceOverride.detect(
                rsi6, volume_ratio, change_5m, ofi["bias"], ofi["strength"], liq["long_dist"]
            )
            if extreme_oversold_bounce["override"]:
                final_bias = extreme_oversold_bounce["bias"]
                final_reason = extreme_oversold_bounce["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXTREME_OVERSOLD_BOUNCE"
                priority = extreme_oversold_bounce["priority"]
            else:
                extreme_overbought_dump = ExtremeOverboughtDumpOverride.detect(
                    rsi6, volume_ratio, change_5m, ofi["bias"], ofi["strength"],
                    liq["short_dist"], up_energy
                )
                if extreme_overbought_dump["override"]:
                    final_bias = extreme_overbought_dump["bias"]
                    final_reason = extreme_overbought_dump["reason"]
                    final_confidence = "ABSOLUTE"
                    final_phase = "EXTREME_OVERBOUGHT_DUMP"
                    priority = extreme_overbought_dump["priority"]

           # ========== EXHAUSTION DUMP (BLOW-OFF TOP) ==========
            exhaustion_dump = ExhaustionDumpOverride.detect(
                rsi6_5m, volume_ratio, change_5m, up_energy, liq["short_dist"]
            )
            if exhaustion_dump["override"]:
                final_bias = exhaustion_dump["bias"]
                final_reason = exhaustion_dump["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXHAUSTION_DUMP"
                priority = exhaustion_dump["priority"]

            # ========== ULTRA CLOSE SQUEEZE (SHORT LIQ <0.5%) ==========
            ultra_squeeze = UltraCloseSqueezeOverride.detect(
                liq["short_dist"], ofi["bias"], ofi["strength"],
                down_energy, volume_ratio, change_5m
            )
            if ultra_squeeze["override"]:
                final_bias = ultra_squeeze["bias"]
                final_reason = ultra_squeeze["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "ULTRA_CLOSE_SQUEEZE"
                priority = ultra_squeeze["priority"]

            # ========== ABSORPTION REVERSAL (BEAR TRAP) ==========
            absorption_reversal = AbsorptionReversalOverride.detect(
                ofi["bias"], ofi["strength"], down_energy, change_5m, liq["short_dist"], volume_ratio
            )
            if absorption_reversal["override"]:
                final_bias = absorption_reversal["bias"]
                final_reason = absorption_reversal["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "ABSORPTION_REVERSAL"
                priority = absorption_reversal["priority"]

            # ========== OVERSOLD LIQUIDITY BOUNCE ===========
            oversold_liquidity_bounce = OversoldLiquidityBounce.detect(
                rsi6_5m, volume_ratio, liq["long_dist"], down_energy,
                algo_type["bias"], hft_6pct["bias"], change_5m
            )
            if oversold_liquidity_bounce["override"]:
                final_bias = oversold_liquidity_bounce["bias"]
                final_reason = oversold_liquidity_bounce["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "OVERSOLD_LIQUIDITY_BOUNCE"
                priority = oversold_liquidity_bounce["priority"]


            # ========== LIQUIDITY ABSORPTION REVERSAL (BEAR TRAP) ==========
            liq_absorption_rev = LiquidityAbsorptionReversal.detect(
                liq["long_dist"], rsi6, ofi["bias"], ofi["strength"],
                down_energy, volume_ratio, change_5m
            )
            if liq_absorption_rev["override"]:
                final_bias = liq_absorption_rev["bias"]
                final_reason = liq_absorption_rev["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "LIQUIDITY_ABSORPTION_REV"
                priority = liq_absorption_rev["priority"]

            # ========== OVERSOLD LIQUIDITY CONTINUATION (FALLING KNIFE) ==========
            oversold_liquidity_cont = OversoldLiquidityContinuation.detect(
                volume_ratio, liq["long_dist"], down_energy,
                ofi["bias"], ofi["strength"], change_5m, rsi6,
                up_energy, agg, latest_volume, volume_ma10
            )
            if oversold_liquidity_cont["override"]:
                final_bias = oversold_liquidity_cont["bias"]
                final_reason = oversold_liquidity_cont["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "OVERSOLD_LIQUIDITY_CONT"
                priority = oversold_liquidity_cont["priority"]
                
                # ========== ENERGY AGG CONSENSUS OVERRIDE (Priority -143) ==========
                # Check if energy and agg consensus overrides this SHORT signal
                energy_agg_consensus = EnergyAggConsensus.detect(
                    up_energy, down_energy, agg, final_bias, rsi6, change_5m
                )
                if energy_agg_consensus["override"]:
                    final_bias = energy_agg_consensus["bias"]
                    final_reason = energy_agg_consensus["reason"]
                    final_confidence = "ABSOLUTE"
                    final_phase = "ENERGY_AGG_CONSENSUS"
                    priority = energy_agg_consensus["priority"]

            # ========== FALLING KNIFE OVERRIDE (Priority -139) ==========
            falling_knife = FallingKnifeOverride.detect(
                rsi6, rsi6_5m, liq["long_dist"], volume_ratio,
                up_energy, down_energy, algo_type["bias"], hft_6pct["bias"], change_5m
            )
            if falling_knife["override"]:
                final_bias = falling_knife["bias"]
                final_reason = falling_knife["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "FALLING_KNIFE_OVERRIDE"
                priority = falling_knife["priority"]

            # ========== EXTREME OVERSOLD CLOSE LIQUIDITY BOUNCE (Priority -141) ==========
            extreme_oversold_bounce = ExtremeOversoldCloseLiquidityBounce.detect(
                rsi6, liq["long_dist"], up_energy, change_5m
            )
            if extreme_oversold_bounce["override"]:
                final_bias = extreme_oversold_bounce["bias"]
                final_reason = extreme_oversold_bounce["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXTREME_OVERSOLD_CLOSE_LIQ_BOUNCE"
                priority = extreme_oversold_bounce["priority"]

            # ========== EXHAUSTION DROP REVERSAL (Priority -141) ==========
            exhaustion_drop_rev = ExhaustionDropReversal.detect(
                change_5m, volume_ratio, down_energy, liq["short_dist"], rsi6_5m
            )
            if exhaustion_drop_rev["override"]:
                final_bias = exhaustion_drop_rev["bias"]
                final_reason = exhaustion_drop_rev["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXHAUSTION_DROP_REVERSAL"
                priority = exhaustion_drop_rev["priority"]

            # ========== EXTREME OVERBOUGHT DISTRIBUTION (PRIORITY -270) ==========
            extreme_overbought_dist = ExtremeOverboughtDistribution.detect(
                rsi6, rsi6_5m, volume_ratio,
                ofi["bias"], ofi["strength"], up_energy,
                liq["short_dist"], change_5m
            )
            if extreme_overbought_dist["override"]:
                final_bias = extreme_overbought_dist["bias"]
                final_reason = extreme_overbought_dist["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "EXTREME_OVERBOUGHT_DIST"
                priority = extreme_overbought_dist["priority"]

            # ========== TRAPPED SHORT SQUEEZE (Priority -160) ==========
            trapped_short = TrappedShortSqueeze.detect(
                ofi["bias"], ofi["strength"], down_energy,
                up_energy, volume_ratio, liq["short_dist"],
                liq["long_dist"], change_5m, rsi6
            )
            if trapped_short["override"]:
                final_bias = trapped_short["bias"]
                final_reason = trapped_short["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "TRAPPED_SHORT_SQUEEZE"
                priority = trapped_short["priority"]

            # ========== TRAPPED LONG SQUEEZE (Mirror, Priority -160) ==========
            trapped_long = TrappedLongSqueeze.detect(
                ofi["bias"], ofi["strength"], up_energy,
                down_energy, volume_ratio, liq["short_dist"],
                liq["long_dist"], change_5m
            )
            if trapped_long["override"]:
                final_bias = trapped_long["bias"]
                final_reason = trapped_long["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "TRAPPED_LONG_SQUEEZE"
                priority = trapped_long["priority"]

            # ========== NEW: Oversold/Overbought False Bounce Trap ==========
            oversold_false_bounce = OversoldFalseBounceTrap.detect(
                rsi6, volume_ratio, ofi["bias"], ofi["strength"], change_5m, liq["long_dist"]
            )
            if oversold_false_bounce["override"]:
                final_bias = oversold_false_bounce["bias"]
                final_reason = oversold_false_bounce["reason"]
                final_confidence = "ABSOLUTE"
                final_phase = "OVERSOLD_FALSE_BOUNCE"
                priority = oversold_false_bounce["priority"]
            else:
                overbought_false_bounce = OverboughtFalseBounceTrap.detect(
                    rsi6, volume_ratio, ofi["bias"], ofi["strength"], change_5m, liq["short_dist"]
                )
                if overbought_false_bounce["override"]:
                    final_bias = overbought_false_bounce["bias"]
                    final_reason = overbought_false_bounce["reason"]
                    final_confidence = "ABSOLUTE"
                    final_phase = "OVERBOUGHT_FALSE_BOUNCE"
                    priority = overbought_false_bounce["priority"]

            # ========== NEW DETECTORS: Funding Squeeze, OFI Spoofing, Liquidity Ambiguity ==========
            # Priority Lock System: kumpulkan semua kandidat dan pilih yang prioritas tertinggi
            
            candidates = []  # list of (priority, bias, reason, phase)
            
            # 1. Funding Negative Short Squeeze Override (Priority -1102)
            funding_squeeze = FundingNegativeShortSqueezeOverride.detect(
                funding_rate, liq["long_dist"], liq["short_dist"],
                rsi6, down_energy, up_energy, rsi6_5m, obv_trend
            )
            if funding_squeeze["override"]:
                candidates.append((
                    funding_squeeze["priority"],
                    funding_squeeze["bias"],
                    funding_squeeze["reason"],
                    "FUNDING_SHORT_SQUEEZE"
                ))
            
            # 2. OFI Spoofing Detector (Priority -1099)
            ofi_spoof = OFISpoofingDetector.detect(
                ofi["bias"], ofi["strength"], agg,
                up_energy, down_energy,
                liq["short_dist"], liq["long_dist"],
                volume_ratio
            )
            if ofi_spoof["override"]:
                candidates.append((
                    ofi_spoof["priority"],
                    ofi_spoof["bias"],
                    ofi_spoof["reason"],
                    "OFI_SPOOFING"
                ))
            
            # 3. Liquidity Ambiguity Resolver (Priority -1097)
            liq_ambiguity = LiquidityAmbiguityResolver.detect(
                liq["short_dist"], liq["long_dist"],
                ofi["bias"], agg,
                down_energy, up_energy,
                funding_rate,
                rsi6, rsi6_5m
            )
            if liq_ambiguity["override"]:
                candidates.append((
                    liq_ambiguity["priority"],
                    liq_ambiguity["bias"],
                    liq_ambiguity["reason"],
                    "LIQUIDITY_AMBIGUITY"
                ))
            
            # Pilih kandidat dengan priority paling tinggi (paling negatif)
            if candidates:
                candidates.sort(key=lambda x: x[0])  # sort ascending (paling negatif dulu)
                best = candidates[0]
                
                # Hanya override jika priority kandidat lebih tinggi dari current priority
                current_priority = locals().get('priority', 0)
                if best[0] < current_priority:
                    final_bias = best[1]
                    final_reason = best[2]
                    final_confidence = "ABSOLUTE"
                    final_phase = best[3]
                    priority = best[0]

            # ========== OFI DOMINANCE OVERRIDE (Priority -145) ==========
            # 🔥 Jika volume rendah dan OFI sangat kuat (>0.7), paksa arah OFI
            
            # 🔥 Filter OFI dominance jika HFT dan liquidity bertentangan
            def should_block_ofi_dominance(ofi_bias, agg, hft_bias, long_dist, short_dist, rsi6):
                # Jika OFI dominance memaksa LONG tapi HFT bilang SHORT dan long_liq lebih dekat serta RSI netral
                if agg >= 0.95 and ofi_bias == "LONG" and hft_bias == "SHORT" and long_dist < short_dist and 35 < rsi6 < 65:
                    return True
                return False
            
            if volume_ratio < 0.6 and ofi["strength"] > 0.7:
                # 🔥 Jika sudah ada sinyal dengan priority lebih tinggi (lebih negatif dari -200), jangan override
                if 'priority' in locals() and priority < -200:
                    final_reason += f" | OFI dominance skipped due to higher priority signal (priority {priority})"
                elif should_block_ofi_dominance(ofi["bias"], agg, hft_6pct["bias"], liq["long_dist"], liq["short_dist"], rsi6):
                    # Jangan paksa LONG, biarkan sinyal lain yang menentukan
                    final_reason += f" | OFI dominance blocked by HFT-liquidity conflict"
                else:
                    # 🔥 TAMBAH: validasi OFI vs agg sebelum dominance
                    ofi_is_spoofed = (
                        ofi["bias"] == "SHORT" and agg > 0.65 and down_energy < 0.01
                    ) or (
                        ofi["bias"] == "LONG" and agg < 0.35 and up_energy < 0.01
                    )
                    
                    if ofi_is_spoofed:
                        final_reason += f" | OFI dominance blocked (agg={agg:.2f} contradicts OFI {ofi['bias']})"
                    else:
                        # Apply dominance
                        if ofi["bias"] == "LONG":
                            final_bias = "LONG"
                            final_reason = f"OFI dominance: {ofi['strength']:.2f} with low volume → forcing LONG"
                        elif ofi["bias"] == "SHORT":
                            final_bias = "SHORT"
                            final_reason = f"OFI dominance: {ofi['strength']:.2f} with low volume → forcing SHORT"
                        final_confidence = "ABSOLUTE"
                        final_phase = "OFI_DOMINANCE"
                        priority = -145

            # ========== MACD DUEL OVERRIDE (WITH LECTURER'S SARAN FILTER) ==========
            if macd_decision["action"] != "NONE":
                # Apply lecturer's saran filter for REVERSE actions
                if macd_decision["action"] == "REVERSE":
                    new_bias, action, filter_reason = apply_macd_duel_safe(
                        macd_decision, final_bias, algo_type, hft_6pct, ofi, change_5m, liq, rsi6_5m, volume_ratio
                    )
                    
                    if action == "REVERSE":
                        # Lolos semua filter → lakukan reverse
                        original = final_bias
                        final_bias = new_bias
                        final_reason += f" | MACD Duel REVERSE ({macd_decision['mode']}): {macd_decision['duel']} [PASS]"
                        final_phase = "MACD_DUEL_REVERSE"
                        final_confidence = "ABSOLUTE"
                    elif action == "BLOCKED":
                        # Reverse diblokir karena sinyal asli terlalu kuat atau kondisi lain
                        final_reason += f" | MACD Duel REVERSE BLOCKED ({filter_reason})"
                        final_phase = "MACD_DUEL_BLOCKED"
                    elif action == "IGNORED":
                        # Reverse diabaikan karena duel terlalu kecil
                        final_reason += f" | MACD Duel REVERSE IGNORED ({filter_reason})"
                        final_phase = "MACD_DUEL_IGNORED"
                else:  # FOLLOW
                    final_reason += f" | MACD Duel FOLLOW ({macd_decision['mode']}): {macd_decision['duel']}"
                    final_phase = "MACD_DUEL_FOLLOW"
                    final_confidence = "ABSOLUTE"

            # ========== Anti-reversal guard ==========
            if AntiReversalGuard.should_block_long(obv_trend, rsi6, volume_ratio, ofi["bias"], ofi["strength"], liq["long_dist"]):
                if final_bias == "LONG":
                    final_bias = "SHORT"
                    final_reason = f"Anti-reversal guard: OBV extreme, RSI {rsi6:.1f}<30, low volume → blocking LONG, force SHORT"
                    final_confidence = "ABSOLUTE"
                    final_phase = "ANTI_REVERSAL"

            if AntiReversalGuard.should_block_short(obv_trend, rsi6, volume_ratio, ofi["bias"], ofi["strength"], liq["short_dist"]):
                if final_bias == "SHORT":
                    final_bias = "LONG"
                    final_reason = f"Anti-reversal guard: OBV extreme, RSI {rsi6:.1f}>70, low volume → blocking SHORT, force LONG"
                    final_confidence = "ABSOLUTE"
                    final_phase = "ANTI_REVERSAL_SHORT"

            # ========== Latency arb check ==========
            if not LatencyArbitragePredictor.is_safe(final_bias, price, predicted_price):
                final_bias = "WAIT"
                final_reason = f"Latency arb: predicted {predicted_price:.2f} vs current {price:.2f} → waiting"
                final_confidence = "ABSOLUTE"
                final_phase = "LATENCY_ARB_WAIT"

            # Apply volume confidence and multi‑TF filters
            if final_bias in ["LONG", "SHORT"]:
                final_confidence, final_reason = VolumeConfidenceFilter.apply(volume_ratio, final_confidence, final_reason)
                if rsi6_5m is not None:
                    final_confidence, final_reason = MultiTimeframeConfirmation.check(rsi6, rsi6_5m, final_confidence, final_reason)
                final_bias, final_reason = OBVStochasticReversal.apply(
                    obv_trend, obv_value, stoch_k, stoch_d, final_bias, final_reason,
                    volume_ratio, rsi6, rsi6_5m,
                    change_5m, liq["short_dist"], liq["long_dist"],
                    latest_volume, volume_ma10
                )

            volume_trap = VolumeTrapDetector.detect(volume_ratio, change_5m, final_bias)
            if volume_trap["warning"]:
                if final_confidence == "ABSOLUTE":
                    final_confidence = "MEDIUM"
                final_reason += f" | {volume_trap['reason']}"

            if volume_ratio < 0.8 and ofi["bias"] == "NEUTRAL":
                if final_bias != "NEUTRAL":
                    final_confidence = "MEDIUM" if final_confidence == "ABSOLUTE" else final_confidence
                    final_reason += f" | Low volume ({volume_ratio:.2f}x) & OFI neutral → caution"

            # ========== Compute floating PnL ===========
            floating_pnl = self.state_mgr.get_floating_pnl_pct(price)

            # ========== FIXED Volume Filter: Jangan reverse jika bias sudah searah liquidity atau sinyal HFT/Algo kuat ==========
            if final_bias in ["LONG", "SHORT"] and len(volumes_1m) >= 10:
                if latest_volume < volume_ma10:
                    # 🔥 JANGAN REVERSE jika priority tinggi (< -250) ATAU priority dari crowded detectors (-165)
                    if priority < -250 or priority == -165:
                        final_reason += f" | High priority signal (priority {priority}) → volume filter bypassed"
                        # Skip reverse, tetap pakai bias original
                    else:
                        # Tentukan arah liquidity
                        liquidity_bias = "LONG" if liq["short_dist"] < liq["long_dist"] else "SHORT"
                        # Cek apakah HFT dan Algo Type konsisten (sama dan TIDAK NEUTRAL)
                        hft_algo_agree = (hft_6pct["bias"] == algo_type["bias"] and 
                                          hft_6pct["bias"] != "NEUTRAL" and 
                                          algo_type["bias"] != "NEUTRAL")
                        # Gabungkan dengan sinyal kuat yang sudah ada
                        is_strong = self._is_strong_signal(ofi, up_energy, down_energy, change_5m, rsi6) or hft_algo_agree

                        # --- Tambahan: jika volume sangat rendah dan RSI 5m oversold/overbought, jangan reverse ---
                        if volume_ratio < 0.5 and (rsi6_5m < 30 or rsi6_5m > 70):
                            is_strong = True   # Anggap sinyal kuat, jangan reverse
                            final_reason += f" | Very low volume with extreme RSI5m ({rsi6_5m:.1f}) → holding"
                        # ------------------------------------------------------------------------

                        # Jangan reverse jika ada sinyal kuat
                        if is_strong:
                            final_reason += f" | Volume low but strong signal (HFT+Algo agree) → holding"
                        # Jangan reverse jika bias sudah searah liquidity
                        elif final_bias == liquidity_bias:
                            final_reason += f" | Volume low but aligned with liquidity ({liquidity_bias}) → holding"
                        else:
                            # Deteksi apakah di zona squeeze (likuiditas dekat)
                            is_near_liquidity = liq["short_dist"] < LIQ_SQUEEZE_THRESHOLD or liq["long_dist"] < LIQ_SQUEEZE_THRESHOLD
                            if not is_near_liquidity:
                                original_bias = final_bias
                                final_bias = "LONG" if original_bias == "SHORT" else "SHORT"
                                final_reason += f" | Volume {latest_volume:.2f} < MA10 {volume_ma10:.2f} → reverse from {original_bias} to {final_bias}"
                                final_confidence = "ABSOLUTE"
                                final_phase = "VOLUME_FILTER_REVERSE"
                            else:
                                final_reason += f" | Volume Low but Near Liquidity ({liq['short_dist']}%/{liq['long_dist']}%) → Hold Squeeze Bias"
                                if final_confidence == "ABSOLUTE":
                                    final_confidence = "HIGH"
                else:
                    # Jika volume tidak rendah, hanya beri warning jika perlu
                    final_reason += f" | Volume {latest_volume:.2f} >= MA10 {volume_ma10:.2f} (normal)"

            # ========== Low Cap Mode ==========
            if latest_volume < LOW_CAP_VOLUME_THRESHOLD:
                final_reason += " | Low cap mode activated: prioritizing liquidity"
                # Cek double sweep: kedua likuiditas dekat
                if liq["short_dist"] < 2.0 and liq["long_dist"] < 2.0:
                    final_bias = "WAIT"
                    final_reason = f"Low cap mode: double sweep zone (short liq {liq['short_dist']}%, long liq {liq['long_dist']}%) → waiting"
                    final_confidence = "ABSOLUTE"
                    final_phase = "LOW_CAP_DOUBLE_SWEEP"
                else:
                    # 🔥 Jangan override jika extreme oversold/overbought dengan volume rendah
                    if volume_ratio < 0.6 and (rsi6 < 20 or rsi6 > 80):
                        final_reason += f" | Low cap but extreme RSI6 ({rsi6:.1f}) with low volume → skip liquidity override"
                    else:
                        if liq["short_dist"] < liq["long_dist"]:
                            if final_bias != "LONG":
                                final_bias = "LONG"
                                final_reason = f"Low cap mode: overriding to LONG (short liq closer)"
                        else:
                            if final_bias != "SHORT":
                                final_bias = "SHORT"
                                final_reason = f"Low cap mode: overriding to SHORT (long liq closer)"
                        final_confidence = "ABSOLUTE"
                        final_phase = "LOW_CAP_SNIPER"

            # Latency compensator
            final_bias = self.latency_comp.adjust_signal(final_bias, self.last_latency)

            # Time decay filter (anti‑flip)
            final_bias = TimeDecayFilter.apply(final_bias)

            # ========== FLOATING PNL STABILITY FILTER ==========
            # Prevents signal flip-flop when price hasn't moved significantly
            current_floating_pnl = self.state_mgr.get_floating_pnl_pct(price)
            if self.state_mgr.last_bias != "NEUTRAL" and self.state_mgr.last_bias != final_bias:
                # Jika floating PnL masih sangat kecil (<0.5%) dan pergerakan harga kecil (<1%)
                # dan sinyal tidak berasal dari prioritas sangat tinggi (misal < -900)
                if abs(current_floating_pnl) < 0.5 and abs(change_5m) < 1.0 and priority > -900:
                    final_bias = self.state_mgr.last_bias
                    final_reason += f" | Stability hold: floating PnL {current_floating_pnl:.2f}% < 0.5%, keep previous bias"

            # Position sizing
            trap_strength = 0.0
            if "Fake bounce" in final_reason or "Cascade dump" in final_reason:
                trap_strength = 0.7
            elif "Low volume continuation" in final_reason:
                trap_strength = 0.5
            elif "Empty Book Trap" in final_reason:
                trap_strength = 0.8
            else:
                trap_strength = 0.2
            position_multiplier = PositionSizer.size(prob_conf if 'prob_conf' in locals() else 0.5, trap_strength, volume_ratio)

            # Update state
            if final_bias in ["LONG", "SHORT"] and final_bias != self.state_mgr.last_bias:
                self.state_mgr.update_position(final_bias, price)

            # Build result dictionary
            exchange_risk = ExchangeRiskScore.calculate(
                funding_rate, liq["short_dist"], liq["long_dist"],
                volume_ratio, change_5m, oi_delta
            )
            
            result = {
                "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "symbol": self.symbol,
                "price": round(price, 4),
                "rsi6": round(rsi6, 1),
                "rsi14": round(rsi14, 1),
                "stoch_k": round(stoch_k, 1),
                "stoch_d": round(stoch_d, 1),
                "stoch_j": round(3 * stoch_k - 2 * stoch_d, 1),
                "obv_trend": obv_trend,
                "obv_value": round(obv_value, 2),
                "obv_magnitude": "HIGH" if abs(obv_value) > 50_000_000 else "MEDIUM" if abs(obv_value) > 10_000_000 else "LOW",
                "volume_ratio": round(volume_ratio, 2),
                "change_5m": round(change_5m, 2),
                "short_liq": liq["short_dist"],
                "long_liq": liq["long_dist"],
                "up_energy": round(up_energy, 2),
                "down_energy": round(down_energy, 2),
                "agg": round(agg, 2),
                "flow": round(flow, 2),
                "crowded_multiplier": 1.0,  # not used now
                "bias": final_bias,
                "confidence": final_confidence,
                "reason": final_reason,
                "phase": final_phase,
                "priority_level": priority,
                "algo_type_bias": algo_type["bias"],
                "hft_6pct_bias": hft_6pct["bias"],
                "hft_6pct_reason": hft_6pct["reason"],
                "ofi_bias": ofi["bias"],
                "ofi_strength": ofi["strength"],
                "funding_rate": funding_rate,
                "latency_ms": self.last_latency,
                "latest_volume": round(latest_volume, 2),
                "volume_ma10": round(volume_ma10, 2),
                "floating_pnl": round(floating_pnl, 2),
                "rsi6_5m": round(rsi6_5m, 1),
                "bid_slope": round(bid_slope, 2),
                "ask_slope": round(ask_slope, 2),
                "predicted_price": round(predicted_price, 4),
                "position_multiplier": round(position_multiplier, 2),
                # Exchange Risk Engine fields
                "exchange_risk_score": exchange_risk["risk_score"],
                "exchange_risk_level": exchange_risk["risk_level"],
                "exchange_safe_direction": exchange_risk["safe_direction"]
            }

            # Stability filter (global anti‑flip)
            global LAST_BIAS, LAST_BIAS_TIME
            now = time.time()
            if LAST_BIAS is not None and result["bias"] != LAST_BIAS and (now - LAST_BIAS_TIME) < 1.0:
                result["bias"] = LAST_BIAS
                result["reason"] += " | Stability lock (anti-flip)"
            if result["bias"] in ["LONG", "SHORT"]:
                LAST_BIAS = result["bias"]
                LAST_BIAS_TIME = now

            # ========== MARKET PHASE DETECTOR INTEGRATION ==========
            phase_data = {
                "change_5m": change_5m,
                "volume_ratio": volume_ratio,
                "down_energy": down_energy,
                "up_energy": up_energy,
                "ofi_bias": ofi["bias"],
                "rsi6": rsi6,
                "obv_trend": obv_trend,
            }
            phase_result = detect_market_phase(phase_data)
            result = apply_phase_override(result, phase_result)

            # Fix 4: Update bait_start_time untuk Time-Weighted Patience Detector
            if phase_result.phase == "BAIT" and self.bait_start_time is None:
                self.bait_start_time = time.time()
            elif phase_result.phase != "BAIT":
                self.bait_start_time = None

            # ========== GREEKS FINAL SCREENER INTEGRATION ==========
            result = greeks_final_screen(result)

            # ========== GAMMA SPOOFING DETECTOR (setelah Greeks) ==========
            gamma_spoof = GammaLiquidityAlignment.detect(
                result.get("greeks_gamma_intensity", "LOW"),
                result.get("greeks_kill_direction", "NEUTRAL"),
                liq["short_dist"], liq["long_dist"],
                result.get("greeks_gamma_executing", False),
                volume_ratio,
                result.get("market_phase", "UNKNOWN"),
                kill_speed=result.get("greeks_kill_speed", 0.0)  # TAMBAHAN: kill_speed untuk validasi spoofing
            )
            if gamma_spoof["override"]:
                # Override hasil Greeks dengan priority -10000
                result["bias"] = gamma_spoof["bias"]
                result["reason"] = f"[GAMMA SPOOFING] {gamma_spoof['reason']} | Original: {result.get('reason', '')}"
                result["confidence"] = "ABSOLUTE"
                result["phase"] = "GAMMA_LIQUIDITY_ALIGNMENT"
                result["priority_level"] = gamma_spoof["priority"]
                result["greeks_override"] = True  # tandai bahwa Greeks di-override

            # ========== STABILITY FILTERS (Flip Cooldown, Phase Lock, Confirmation, Gamma Delay, Entry) ==========
            # Simpan flag extreme override ke result sebelum stability filter
            result["_liquidity_extreme_override"] = locals().get('_liquidity_extreme_override', False)
            
            result = self._apply_stability_filters(result, phase_result, {})

            return result

        except Exception as e:
            print(f"❌ Error analyzing {self.symbol}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _build_result(self, price, rsi6, rsi14, stoch_k, stoch_d, obv_trend, obv_value,
                      volume_ratio, change_5m, liq, up_energy, down_energy,
                      agg, flow, final_bias, final_confidence, final_reason,
                      final_phase, priority, ofi=None, iceberg=None, funding_trap=None, liq_heat=None,
                      cross_lead=None, ws_data=None, funding_rate=None, latest_volume=None, volume_ma10=None, rsi6_5m=None):
        # Apply volume confidence and multi-TF filters to override results
        if final_bias in ["LONG", "SHORT"]:
            final_confidence, final_reason = VolumeConfidenceFilter.apply(volume_ratio, final_confidence, final_reason)
            if rsi6_5m is not None:
                final_confidence, final_reason = MultiTimeframeConfirmation.check(rsi6, rsi6_5m, final_confidence, final_reason)
            final_bias, final_reason = OBVStochasticReversal.apply(
                obv_trend, obv_value, stoch_k, stoch_d, final_bias, final_reason,
                volume_ratio, rsi6, rsi6_5m,
                change_5m, liq["short_dist"], liq["long_dist"],
                latest_volume or 0.0, volume_ma10 or 1.0
            )

        volume_trap = VolumeTrapDetector.detect(volume_ratio, change_5m, final_bias)
        if volume_trap["warning"]:
            if final_confidence == "ABSOLUTE":
                final_confidence = "MEDIUM"
            final_reason += f" | {volume_trap['reason']}"

        if volume_ratio < 0.8 and ofi is not None and ofi.get("bias") == "NEUTRAL":
            if final_bias != "NEUTRAL":
                final_confidence = "MEDIUM" if final_confidence == "ABSOLUTE" else final_confidence
                final_reason += f" | Low volume ({volume_ratio:.2f}x) & OFI neutral → caution"

        if latest_volume is not None and volume_ma10 is not None and final_bias in ["LONG", "SHORT"]:
            if latest_volume < volume_ma10:
                is_near_liquidity = liq["short_dist"] < LIQ_SQUEEZE_THRESHOLD or liq["long_dist"] < LIQ_SQUEEZE_THRESHOLD
                if not self._is_strong_signal(ofi, up_energy, down_energy, change_5m, rsi6) and not is_near_liquidity:
                    original_bias = final_bias
                    final_bias = "LONG" if original_bias == "SHORT" else "SHORT"
                    final_reason += f" | Volume {latest_volume:.2f} < MA10 {volume_ma10:.2f} → reverse from {original_bias} to {final_bias}"
                    final_confidence = "ABSOLUTE"
                    final_phase = "VOLUME_FILTER_REVERSE"
                elif is_near_liquidity:
                    final_reason += f" | Volume Low but Near Liquidity ({liq['short_dist']}%/{liq['long_dist']}%) → Hold Squeeze Bias"
                    if final_confidence == "ABSOLUTE":
                        final_confidence = "HIGH"
                else:
                    final_reason += f" | Volume {latest_volume:.2f} < MA10 {volume_ma10:.2f} (warning, but signal strong)"

        final_bias = self.latency_comp.adjust_signal(final_bias, self.last_latency)

        result = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "symbol": self.symbol,
            "price": round(price, 4),
            "rsi6": round(rsi6, 1),
            "rsi14": round(rsi14, 1),
            "stoch_k": round(stoch_k, 1),
            "stoch_d": round(stoch_d, 1),
            "stoch_j": round(3 * stoch_k - 2 * stoch_d, 1),
            "obv_trend": obv_trend,
            "obv_value": round(obv_value, 2),
            "obv_magnitude": "HIGH" if abs(obv_value) > 50_000_000 else "MEDIUM" if abs(obv_value) > 10_000_000 else "LOW",
            "volume_ratio": round(volume_ratio, 2),
            "change_5m": round(change_5m, 2),
            "short_liq": liq["short_dist"],
            "long_liq": liq["long_dist"],
            "up_energy": round(up_energy, 2),
            "down_energy": round(down_energy, 2),
            "agg": round(agg, 2),
            "flow": round(flow, 2),
            "crowded_multiplier": 1.0,
            "bias": final_bias,
            "confidence": final_confidence,
            "reason": final_reason,
            "phase": final_phase,
            "priority_level": priority,
            "algo_type_bias": "NEUTRAL",
            "hft_6pct_bias": "NEUTRAL",
            "hft_6pct_reason": "",
            "ofi_bias": ofi["bias"] if ofi else "NEUTRAL",
            "ofi_strength": ofi["strength"] if ofi else 0.0,
            "funding_rate": funding_rate,
            "latency_ms": self.last_latency,
            "latest_volume": round(latest_volume, 2) if latest_volume else 0,
            "volume_ma10": round(volume_ma10, 2) if volume_ma10 else 0,
            "floating_pnl": 0.0,
            "rsi6_5m": round(rsi6_5m, 1) if rsi6_5m else 0,
            "bid_slope": 0.0,
            "ask_slope": 0.0,
            "predicted_price": 0.0,
            "position_multiplier": 1.0
        }

        # Stability filter (global anti‑flip)
        global LAST_BIAS, LAST_BIAS_TIME
        now = time.time()
        if LAST_BIAS is not None and result["bias"] != LAST_BIAS and (now - LAST_BIAS_TIME) < 1.0:
            result["bias"] = LAST_BIAS
            result["reason"] += " | Stability lock (anti-flip)"
        if result["bias"] in ["LONG", "SHORT"]:
            LAST_BIAS = result["bias"]
            LAST_BIAS_TIME = now

        return result

    def _build_latency_result(self):
        result = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "symbol": self.symbol,
            "price": 0.0,
            "bias": "WAIT",
            "confidence": "ABSOLUTE",
            "reason": f"High latency ({self.last_latency:.0f}ms) - skipping entry",
            "phase": "LATENCY_WAIT",
            "priority_level": -260,
            "rsi6": 0, "rsi14": 0, "stoch_k": 0, "stoch_d": 0, "stoch_j": 0,
            "obv_trend": "NEUTRAL", "obv_value": 0.0, "obv_magnitude": "LOW",
            "volume_ratio": 0, "change_5m": 0,
            "short_liq": 0, "long_liq": 0, "up_energy": 0, "down_energy": 0,
            "agg": 0, "flow": 0, "crowded_multiplier": 1.0,
            "algo_type_bias": "NEUTRAL", "hft_6pct_bias": "NEUTRAL", "hft_6pct_reason": "",
            "ofi_bias": "NEUTRAL", "ofi_strength": 0.0,
            "funding_rate": None, "latency_ms": self.last_latency,
            "latest_volume": 0, "volume_ma10": 0, "floating_pnl": 0.0,
            "rsi6_5m": 0, "bid_slope": 0.0, "ask_slope": 0.0, "predicted_price": 0.0,
            "position_multiplier": 1.0
        }
        return result

# ================= OUTPUT FORMATTER =================
class OutputFormatter:
    @staticmethod
    def print_header():
        print("\n" + "="*80)
        print("🔥 BINANCE LIQUIDATION HUNTER - ULTIMATE EDITION v8 (LIQUIDITY SQUEEZE FOCUS)")
        print("="*80)
        print("\n🧠 INTEGRATED MODULES:")
        print(" 📍 WebSocket Real-time Data (optional on Koyeb, no startup sleep)")
        print(" 📍 Order Flow Imbalance (OFI) with smoothing and conflict filter")
        print(" 📍 Iceberg Order Detector")
        print(" 📍 Cross-Exchange Leader (placeholder)")
        print(" 📍 Funding Rate Trap")
        print(" 📍 Latency Compensator (adaptive threshold)")
        print(" 📍 Data Caching (reduces REST calls)")
        print(" 📍 ⭐ Stability Filter (anti‑flip within 1 second)")
        print(" 📍 ⭐ Time Decay Filter (signal persistence)")
        print(" 📍 ⭐ Low Volume Continuation Detector")
        print(" 📍 ⭐ Anti‑Reversal Guard")
        print(" 📍 ⭐ Cascade Dump Detector")
        print(" 📍 ⭐ Fake Bounce Trap")
        print(" 📍 ⭐ Order Book Slope Analysis")
        print(" 📍 ⭐ Latency Arbitrage Predictor")
        print(" 📍 ⭐ Probabilistic Scoring Engine")
        print(" 📍 ⭐ Dynamic Position Sizing")
        print(" 📍 ⭐ Overbought Distribution Trap (NEW) - overrides Empty Book Trap when overbought")
        print(" 📍 ⭐ Oversold Squeeze Trap (NEW) - overrides when oversold")
        print(" 📍 ⭐ Empty Book Trap (NEW)")
        print(" 📍 ⭐ Squeeze Continuation Detector (NEW) - prevents SHORT traps in strong uptrend")
        print(" 📍 ⭐ HFT6PercentDirection - Liquidity Priority (FIXED)")
        print(" 📍 ⭐ Volume Filter - No Reversal Near Liquidity (FIXED)")
        print(" 📍 ⭐ Low Cap Sniper Mode")
        print("="*80 + "\n")

    @staticmethod
    def print_signal(result: Dict):
        print("="*80)
        print(f"🔥 {result.get('symbol', 'UNKNOWN')} @ {result.get('timestamp', '')}")
        print(f"💰 Price: ${result.get('price', 0):.4f}")
        print("="*80)

        print(f"\n{'='*40}")
        bias = result.get('bias', 'NEUTRAL')
        bias_color = "🟢" if bias == "LONG" else "🔴" if bias == "SHORT" else "🟡"
        conf = result.get('confidence', 'MEDIUM')
        conf_icon = {"ABSOLUTE": "⚡⚡⚡", "HIGH": "🔥🔥🔥", "MEDIUM": "🔥🔥", "LOW": "🔥"}.get(conf, "🔥")
        print(f"{bias_color} FINAL BIAS: {bias}")
        print(f"{conf_icon} CONFIDENCE: {conf}")
        print(f"📌 REASON: {result.get('reason', '')}")
        print(f"🎯 PHASE: {result.get('phase', '')}")
        print(f"💰 POSITION SIZE MULTIPLIER: {result.get('position_multiplier', 1.0):.2f}")

        if result.get('entry_allowed') is not None:
            entry_status = "✅ ALLOWED" if result['entry_allowed'] else "⛔ WAIT"
            print(f"🚦 ENTRY STATUS: {entry_status} - {result.get('entry_reason', '')}")
            if result.get('entry_reason_delayed'):
                print(f"   └─ Delayed Entry: {result.get('entry_reason_delayed')}")
            if result.get('kill_confirmation'):
                kc = result['kill_confirmation']
                print(f"   └─ Kill Confirmation: {'✅ CONFIRMED' if kc.get('kill_confirmed') else '⏳ WAITING'} (score: {kc.get('kill_score', 0)}/4) - {kc.get('reason', '')}")
            
            # NEW: Kill-Zone Flip Trap Detection Output
            if result.get('kill_direction_stability'):
                kds = result['kill_direction_stability']
                stability_icon = "⚠️ DANGER" if kds.get('danger') else "✅ STABLE"
                print(f"   └─ Kill Direction Stability: {stability_icon} (flips: {kds.get('flip_count', 0)}, dominant: {kds.get('dominant_direction', 'UNKNOWN')})")
            
            if result.get('dual_liq_trap'):
                dlt = result['dual_liq_trap']
                trap_icon = "🚨 TRAP DETECTED" if dlt.get('dual_liq_trap') else "✅ OK"
                print(f"   └─ Dual Liq Trap: {trap_icon} (score: {dlt.get('trap_score', 0)}/5)")
            
            if result.get('bias_kill_conflict'):
                bkc = result['bias_kill_conflict']
                conflict_icon = "⚠️ CONFLICT" if bkc.get('has_conflict') else "✅ NO CONFLICT"
                print(f"   └─ Bias-Kill Conflict: {conflict_icon}")
                if bkc.get('has_conflict'):
                    print(f"      └─ Corrected Direction: {bkc.get('correct_direction', 'N/A')}")
            
            # 🔥 PRESWEEP GUARD OUTPUT
            if result.get('_presweep_override'):
                print("   └─ 🔥 PRESWEEP GUARD ACTIVE")

        print(f"\n{'='*40}")
        print("📊 KEY METRICS:")
        print(f"📈 RSI(6): {result.get('rsi6', 0)} | RSI(14): {result.get('rsi14', 0)} | RSI(6) 5m: {result.get('rsi6_5m', 0)}")
        print(f"🎲 Stochastic: K={result.get('stoch_k', 0):.1f} D={result.get('stoch_d', 0):.1f} J={result.get('stoch_j', 0):.1f}")
        print(f"📊 OBV: {result.get('obv_trend', 'NEUTRAL')} (value: {result.get('obv_value', 0):,.0f}, magnitude: {result.get('obv_magnitude', 'LOW')})")
        print(f"💸 Volume Ratio: {result.get('volume_ratio', 0):.2f}x | 5m Change: {result.get('change_5m', 0):.2f}%")
        print(f"📊 Latest Volume: {result.get('latest_volume', 0):.2f} | Volume MA10: {result.get('volume_ma10', 0):.2f}")
        print(f"🎯 Short Liq: +{result.get('short_liq', 0)}% | Long Liq: -{result.get('long_liq', 0)}%")
        print(f"⚡ Energy: up={result.get('up_energy', 0):.2f} down={result.get('down_energy', 0):.2f}")
        print(f"🧠 Agg/Flow: {result.get('agg', 0):.2f}/{result.get('flow', 0):.2f}")
        print(f"🕒 Latency: {result.get('latency_ms', 0):.0f} ms")
        print(f"💰 Floating PnL: {result.get('floating_pnl', 0):.2f}%")
        print(f"📐 Order Book Slope: bid={result.get('bid_slope', 0):.2f} ask={result.get('ask_slope', 0):.2f}")
        print(f"🔮 Predicted Price: ${result.get('predicted_price', 0):.4f}")

        print("\n🎯 ALGO TYPE & HFT 6% DIRECTION:")
        print(f" Algo Type Bias: {result.get('algo_type_bias', 'NEUTRAL')}")
        print(f" HFT 6% Bias: {result.get('hft_6pct_bias', 'NEUTRAL')}")
        print(f" HFT Reason: {result.get('hft_6pct_reason', '')}")

        if result.get('ofi_bias') != "NEUTRAL":
            print(f"\n📊 ORDER FLOW IMBALANCE: {result['ofi_bias']} (strength {result['ofi_strength']:.2f})")

        if result.get('funding_rate') is not None:
            print(f"💰 Funding Rate: {result['funding_rate']:.6f}")

        print("\n" + "="*80)

# ================= GLOBAL VARIABLES =================
POPULAR_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT",
    "PIPPINUSDT", "POWERUSDT", "SAHARAUSDT", "ROBOUSDT", "PHAUSDT",
    "SIRENUSDT", "ARCUSDT", "RIVERUSDT", "JTOUSDT", "CYBERUSDT"
]

# ================= MAIN =================
def main():
    import sys

    if len(sys.argv) > 1:
        symbol = sys.argv[1].upper()
    else:
        symbol = input("\nSymbol (e.g. BTCUSDT): ").upper() or "BTCUSDT"

    analyzer = BinanceAnalyzer(symbol)
    OutputFormatter.print_header()

    print(f"\n🔍 Analyzing {symbol}...")
    result = analyzer.analyze()

    if result:
        OutputFormatter.print_signal(result)
        print_greeks_section(result)

    if len(sys.argv) > 2 and sys.argv[2] == "--loop":
        print("\n🔄 Auto-refresh every 10 seconds. Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(10)
                result = analyzer.analyze()
                if result:
                    print("\n" + "="*80)
                    print(f"🔄 UPDATE @ {result['timestamp']}")
                    print(f"🎯 Bias: {result['bias']} ({result['confidence']})")
                    print(f"📌 {result['reason']}")
        except KeyboardInterrupt:
            print("\n\n👋 Stopped by user")
    else:
        print(f"❌ Failed to analyze {symbol}")

def api_mode(symbol: str) -> str:
    analyzer = BinanceAnalyzer(symbol)
    result = analyzer.analyze()
    if result:
        return json.dumps(result, indent=2, default=str)
    return json.dumps({"error": f"Failed to analyze {symbol}"})

def batch_mode(symbols: List[str]):
    OutputFormatter.print_header()
    results = []
    analyzers = {}
    for sym in symbols:
        analyzers[sym] = BinanceAnalyzer(sym)

    print("\n" + "="*80)
    print("📊 BATCH ANALYSIS RESULTS:")
    print("="*80)

    for sym in symbols:
        print(f"\n🔍 Analyzing {sym}...")
        result = analyzers[sym].analyze()
        if result:
            results.append(result)
            bias_icon = "🟢" if result['bias'] == "LONG" else "🔴" if result['bias'] == "SHORT" else "🟡"
            conf_icon = "⚡" if result['confidence'] == "ABSOLUTE" else "🔥" if result['confidence'] == "HIGH" else "📈"
            print(f"{conf_icon} {bias_icon} {sym}: {result['bias']} ({result['confidence']})")
            print(f" 📌 {result['reason']}")
        else:
            print(f"❌ {sym}: Failed")

    print("\n" + "="*80)
    return results

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "--api":
            symbol = sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"
            print(api_mode(symbol))
        elif sys.argv[1] == "--batch":
            symbols = sys.argv[2:] if len(sys.argv) > 2 else POPULAR_SYMBOLS
            batch_mode(symbols)
        elif sys.argv[1] == "--help":
            print("""
🔥 Binance Liquidation Hunter - Ultimate Edition v8 (Liquidity Squeeze Focus)

Usage:
python script.py SYMBOL # Analyze single symbol
python script.py SYMBOL --loop # Auto-refresh every 10s
python script.py --batch [SYMBOLS] # Analyze multiple symbols
python script.py --api SYMBOL # JSON output for API
python script.py --help # Show this help

NEW IN v8:
- Fixed Volume Filter: no reversal when near liquidity (squeeze zone)
- Fixed HFT6PercentDirection: prioritizes close liquidity (<1%)
- Empty Book Trap detector: overrides when order book is empty but liq close
- Low Cap Sniper Mode: activated when volume < 100,000, forces bias to liquidity direction
- Overbought Distribution Trap: prevents LONG traps in overbought conditions with low volume
- Oversold Squeeze Trap: prevents SHORT traps in oversold conditions
- Squeeze Continuation Detector: catches short squeeze continuation when price keeps rising despite selling pressure
""")
        else:
            main()
    else:
        main()
