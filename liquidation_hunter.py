#!/usr/bin/env python3
"""
🔥 BINANCE LIQUIDATION HUNTER - ULTIMATE EDITION v9 (LECTURER'S SARAN LOGIC)
🎯 Integrated: Liquidity Magnet Continuation, OFI Absorption Squeeze, Velocity Decay Reversal
🎯 Priority Ladder: MasterSqueezeRule (-1100) > LiquidityMagnet (-1000) > OFIAbsorption (-950) > VelocityDecay (-900) > EmptyBook (-850)
🎯 Golden Rule: LONG UNTIL SHORT LIQ SWEPT / SHORT UNTIL LONG LIQ SWEPT
"""

import requests
from datetime import datetime
import urllib3
import numpy as np
from typing import Optional, Dict, List, Tuple, Any
import time
import json
import threading
import websocket
import os
from collections import deque

# Nonaktifkan SSL warning
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
PERSISTENCE_THRESHOLD = 2.0        # Minimal floating PnL sebelum flip
TRADES_MAXLEN = 200 if IS_KOYEB else 1000
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# NEW CONFIG
SIGNAL_PERSISTENCE_SEC = 2.5        # Time decay for signal flipping
LATENCY_MS_ESTIMATE = 150           # Default latency for arb predictor
LIQ_SQUEEZE_THRESHOLD = 1.5         # If liq < 1.5%, squeeze zone
LOW_CAP_VOLUME_THRESHOLD = 100000   # Absolute volume threshold for low‑cap mode

# ================= STABILITY FILTER GLOBAL =================
LAST_BIAS = None          # last directional bias (LONG/SHORT)
LAST_BIAS_TIME = 0        # timestamp when LAST_BIAS was set

# ================= TIME DECAY GLOBAL =================
LAST_SIGNAL = None
LAST_SIGNAL_TIME = 0

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
        pattern: the 6‑element array
    """
    if len(hist_scaled) < 6:
        return {"action": "NONE"}

    last6 = hist_scaled[-6:]   # index 0 .. 5
    final = last6[5]           # index 5 = last candle

    if final < 0:
        a = last6[3]           # baris 4 (index 3)
        b = last6[1]           # baris 2 (index 1)
        mode = "4vs2"
    else:
        a = last6[1]           # baris 2
        b = last6[3]           # baris 4
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
def apply_macd_duel_safe(macd_decision, final_bias, algo_type, hft_6pct, ofi, change_5m, liq, rsi6_5m, volume_ratio):
    """
    Filter pembatas agar MACD duel tidak membalik sinyal ketika sinyal asli sudah sangat kuat dan konsisten.
    
    Filters:
    1. Filter Kekuatan Sinyal Asli - MACD duel hanya boleh REVERSE jika sinyal asli tidak terlalu kuat
    2. Filter Arah OFI & Algo - Jika OFI, Algo, HFT semuanya sama dan bukan NEUTRAL, maka MACD duel tidak boleh membalik
    3. Filter MACD Duel dengan Ambang Batas - reverse hanya terjadi jika duel cukup besar (abs > 5)
    4. Filter Momentum & Likuiditas - Jika harga sudah bergerak cukup besar dan likuiditas target dekat, 
       jangan reverse kecuali dalam kondisi overbought/oversold ekstrem.
    5. Filter OFI Conflict - Block reversal yang bertentangan dengan OFI kuat saat volume rendah.
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
    
    # Filter 2: triple confirmation (OFI, Algo, HFT semua sama dan bukan NEUTRAL)
    if ofi["bias"] == algo_type["bias"] == hft_6pct["bias"] != "NEUTRAL":
        return final_bias, "BLOCKED", "triple_confirmation"
    
    # Filter 3: duel terlalu kecil (ambang batas abs(duel) < 5)
    if abs(macd_decision.get("duel", 0)) < 5:
        return final_bias, "IGNORED", f"duel_too_small={macd_decision.get('duel', 0)}"
    
    # Filter 4: momentum besar & likuiditas dekat
    if abs(change_5m) > 2.0 and (liq["short_dist"] < 2.0 or liq["long_dist"] < 2.0):
        # Jika kondisi ekstrem overbought/oversold, izinkan reverse (tidak di-block)
        if (rsi6_5m >= 75 and final_bias == "LONG") or (rsi6_5m <= 25 and final_bias == "SHORT"):
            # tetap lanjutkan, tidak block
            pass
        else:
            return final_bias, "BLOCKED", "momentum_and_liq_proximity"
    
    # 🔥 Filter 5: Block reverse if it conflicts with strong OFI under low volume
    new_bias = "SHORT" if final_bias == "LONG" else "LONG"
    if new_bias == "LONG" and ofi["bias"] == "SHORT" and ofi["strength"] > 0.7 and volume_ratio < 0.6:
        return final_bias, "BLOCKED", "ofi_short_conflict"
    if new_bias == "SHORT" and ofi["bias"] == "LONG" and ofi["strength"] > 0.7 and volume_ratio < 0.6:
        return final_bias, "BLOCKED", "ofi_long_conflict"
    
    # Lolos semua filter → lakukan reverse
    return new_bias, "REVERSE", "passed_all_filters"

# ================= WEBSOCKET CONNECTOR (unchanged) =================
class BinanceWebSocket:
    """Real-time WebSocket for order book and trades"""
    def __init__(self, symbol: str):
        self.symbol = symbol.lower()
        self.ws_url = f"wss://fstream.binance.com/ws/{self.symbol}@depth20@100ms/{self.symbol}@trade"
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
        self.ws = websocket.WebSocketApp(self.ws_url,
                                         on_open=self.on_open,
                                         on_message=self.on_message,
                                         on_error=self.on_error,
                                         on_close=self.on_close)
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

class LowVolumeContinuation:
    @staticmethod
    def detect(volume_ratio: float, obv_trend: str, price: float,
               ma25: float, ma99: float, down_energy: float) -> Dict:
        """Low volume continuation → force SHORT (block fake reversals)"""
        if (volume_ratio < 0.6 and
            obv_trend == "NEGATIVE_EXTREME" and
            price < ma25 and price < ma99):
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
        """
        Returns True if LONG reversal should be blocked.
        Exceptions:
        - Extreme oversold with low volume (rsi6 < 20 and volume_ratio < 0.6)
        - OFI strongly LONG (strength > 0.5)
        - Long liquidity very close (< 1.0%)
        """
        if (obv_trend == "NEGATIVE_EXTREME" and
            rsi6 < 30 and
            volume_ratio < 0.7):
            # Exceptions
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
        """
        Returns True if SHORT reversal should be blocked.
        Exceptions:
        - Extreme overbought with low volume (rsi6 > 80 and volume_ratio < 0.6)
        - OFI strongly SHORT (strength > 0.5)
        - Short liquidity very close (< 1.0%)
        """
        if (obv_trend == "POSITIVE_EXTREME" and
            rsi6 > 70 and
            volume_ratio < 0.7):
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
        if (change_5m < -3 and
            short_liq > 10 and
            down_energy < 0.05 and
            volume_ratio < 0.7):
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
        if (rsi6 < 35 and
            change_5m > 1.0 and
            volume_ratio < 0.7 and
            short_dist > long_dist and
            down_energy < up_energy * 0.3 and
            ofi_bias == "LONG" and ofi_strength > 0.3):
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
    def detect(change_5m: float, volume_ratio: float, ofi_bias: str, ofi_strength: float, short_liq: float) -> Dict:
        # Turunkan threshold dari -5.0 menjadi -3.5 agar menangkap drop sedang sekalipun
        if change_5m < -3.5 and volume_ratio < 0.6:
            # Jangan override jika short liq sangat dekat (potensi short squeeze)
            if short_liq < 2.0:
                pass  # biarkan liquidity logic yang menentukan
            else:
                if ofi_bias != "SHORT" or ofi_strength < 0.6:
                    return {
                        "override": True,
                        "bias": "LONG",
                        "reason": f"Exhaustion drop: price dropped {change_5m:.1f}% with low volume ({volume_ratio:.2f}x), no strong selling → bounce likely",
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

class LatencyArbitragePredictor:
    @staticmethod
    def predict_next_price(price: float, change_5m: float, up_energy: float,
                           down_energy: float, latency_ms: float) -> float:
        """Estimate current price based on latency"""
        # Velocity per ms
        velocity = change_5m / 300000.0  # 5 minutes = 300k ms

        # Adjust for energy imbalance
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

        # Confidence scaling
        if confidence > 0.8:
            base *= 1.5
        elif confidence < 0.6:
            base *= 0.5

        # Trap strength
        base *= (1.0 + trap_strength)

        # Low volume risk
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

# ================= NEW DETECTOR FROM MENTOR (Overbought/Oversold Traps) =================
class OverboughtDistributionTrap:
    @staticmethod
    def detect(rsi6: float, short_dist: float, long_dist: float, volume_ratio: float,
               down_energy: float, up_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float) -> Dict:
        """
        Mendeteksi perangkap distribusi ketika market overbought namun ada sinyal LONG palsu.
        Priority lebih tinggi dari Empty Book Trap (-261 vs -260).
        
        NEW: Tidak override jika short liq sangat dekat (<2%) dan lebih dekat dari long liq
        → liquidity mengarah LONG, jangan SHORT.
        
        PATCH: Jika short liq SANGAT dekat (<0.5%) dan down_energy=0
        → HFT belum selesai sweep, jangan SHORT dulu (biarkan TwoPhaseHFTSweep yang handle)
        """
        # 🔥 PATCH: Jika short liq SANGAT dekat (<0.5%) dan down_energy=0
        # → HFT belum selesai sweep, jangan SHORT dulu
        if short_dist < 0.5 and down_energy < 0.01 and change_5m > 0:
            return {"override": False}  # biarkan TwoPhaseHFTSweep yang handle
        
        # 🔥 EXTREME OVERBOUGHT: force SHORT regardless of liquidity proximity
        if rsi6 > 85 and volume_ratio < 0.6 and down_energy < 0.01:
            # 🔥 Jangan paksa SHORT jika short liq sangat dekat dan OFI SHORT kuat (squeeze)
            if short_dist < 1.0 and ofi_bias == "SHORT" and ofi_strength > 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme overbought: RSI {rsi6:.1f} > 85, volume {volume_ratio:.2f}x, no sellers → forced dump",
                "priority": -262   # higher than regular traps
            }
        
        # Jika short liq sangat dekat dan lebih dekat dari long liq → liquidity mengarah LONG, jangan SHORT
        # 🔥 TAPI jika overbought ekstrem (RSI > 75) dan volume rendah, tetap SHORT (distribution trap)
        if short_dist < 2.0 and short_dist < long_dist:
            # Pengecualian untuk overbought ekstrem dengan volume rendah
            if rsi6 > 75 and volume_ratio < 0.7:
                # tetap lanjut ke pengecekan berikutnya, jangan return False
                pass
            else:
                return {"override": False}
        
        # Kasus 1: Overbought + short liq dekat + volume rendah + tidak ada bid support
        if (rsi6 > 70 and
            short_dist < 2.0 and
            volume_ratio < 0.8 and
            down_energy < 0.1 and
            up_energy < 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Overbought short squeeze trap: RSI {rsi6:.1f} overbought, short liq close, low volume, no bids → akan dump",
                "priority": -261
            }
        # Kasus 2: Overbought + OFI LONG strength tinggi + volume rendah → distribusi
        if (rsi6 > 70 and
            ofi_bias == "LONG" and
            ofi_strength > 0.6 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Overbought distribution: RSI {rsi6:.1f} + OFI LONG {ofi_strength:.2f} with low volume → smart money distributing",
                "priority": -261
            }
        # Kasus 3: Overbought + harga sudah naik >2% dalam 5m dengan volume rendah → exhaustion
        if (rsi6 > 70 and
            change_5m > 2.0 and
            volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Overbought exhaustion: price up {change_5m:.1f}% with low volume → pullback likely",
                "priority": -261
            }
        return {"override": False}

class OversoldSqueezeTrap:
    @staticmethod
    def detect(rsi6: float, long_dist: float, short_dist: float, volume_ratio: float,
               up_energy: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float) -> Dict:
        """
        Mendeteksi perangkap squeeze ketika market oversold namun ada sinyal SHORT palsu.
        Priority lebih tinggi dari Empty Book Trap (-261 vs -260).
        
        NEW: Tidak override jika long liq sangat dekat (<2%) dan lebih dekat dari short liq
        → liquidity mengarah SHORT, jangan LONG.
        
        🔥 OFI FILTER: Block jika OFI strongly SHORT dengan volume rendah
        """
        # 🔥 EXTREME OVERSOLD: force LONG regardless of liquidity proximity
        if rsi6 < 15 and volume_ratio < 0.6 and up_energy < 0.01:
            # Block if OFI strongly SHORT (selling pressure)
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme oversold: RSI {rsi6:.1f} < 15, volume {volume_ratio:.2f}x, no buyers → forced bounce",
                "priority": -262
            }
        
        # Jika long liq sangat dekat dan lebih dekat dari short liq → liquidity mengarah SHORT, jangan LONG
        if long_dist < 2.0 and long_dist < short_dist:
            return {"override": False}
        
        if (rsi6 < 30 and
            long_dist < 2.0 and
            volume_ratio < 0.8 and
            up_energy < 0.1 and
            down_energy < 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Oversold squeeze trap: RSI {rsi6:.1f} oversold, long liq close ({long_dist}%), low volume, no asks → akan pump",
                "priority": -261
            }
        return {"override": False}

class EmptyBookTrapDetector:
    @staticmethod
    def detect(down_energy: float, up_energy: float, short_dist: float, long_dist: float,
               rsi6_5m: float, volume_ratio: float, obv_trend: str, rsi6: float,
               ofi_bias: str, ofi_strength: float) -> Dict:
        # Jika short liq sudah sangat dekat (<0.5%), squeeze sudah habis → jangan override LONG
        if short_dist < 0.5:
            return {"override": False}
        
        # ===== CABANG LONG (bid kosong) =====
        if down_energy < 0.1 and short_dist < 2.0:
            # 🔥 Block LONG jika overbought ekstrem (RSI6 > 90 atau RSI5m > 90)
            if (rsi6 > 90 or rsi6_5m > 90) and volume_ratio < 1.5:
                return {"override": False}
            # 🔥 Block LONG jika overbought ekstrem dengan OBV positif ekstrem
            if rsi6 > 75 and obv_trend == "POSITIVE_EXTREME" and volume_ratio < 0.8:
                return {"override": False}
            # Filter sebelumnya
            if rsi6_5m >= 75 and volume_ratio < 0.6:
                return {"override": False}
            if long_dist < short_dist:
                return {"override": False}
            # 🔥 Batalkan LONG jika OFI SHORT kuat dan volume rendah
            if ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Empty Book Trap: No bid support ({down_energy:.2f}) + Short Liq dekat ({short_dist:.2f}%) → Rawan Short Squeeze",
                "priority": -260
            }
        
        # ===== CABANG SHORT (ask kosong) =====
        if up_energy < 0.1 and long_dist < 2.0:
            # 🔥 Block SHORT jika oversold ekstrem (RSI6 < 10 atau RSI5m < 10)
            if (rsi6 < 10 or rsi6_5m < 10) and volume_ratio < 1.5:
                return {"override": False}
            # 🔥 Block SHORT jika oversold ekstrem dengan OBV negatif ekstrem
            if rsi6 < 25 and obv_trend == "NEGATIVE_EXTREME" and volume_ratio < 0.8:
                return {"override": False}
            # Filter sebelumnya
            if rsi6_5m <= 25 and volume_ratio < 0.6:
                return {"override": False}
            if short_dist < long_dist:
                return {"override": False}
            if long_dist < 0.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Empty Book Trap: No ask resistance ({up_energy:.2f}) + Long Liq dekat ({long_dist:.2f}%) → Rawan Long Squeeze",
                "priority": -260
            }
        return {"override": False}

class ExhaustedLiquidityReversal:
    """
    🔥 DETECTS WHEN LIQUIDITY TARGET IS NEARLY EXHAUSTED AND MARKET OVERBOUGHT/OVERSOLD
    Overrides forced LONG when short liq is too small (<0.5%) and overbought.
    Priority -1060 (between MasterSqueeze -1100 and StrictLiquidity -1050)
    
    FILTER: Jangan reverse jika volume sangat rendah (volume_ratio < 0.7) dan 
    RSI 5m overbought/oversold sedang (rsi6_5m > 60 untuk SHORT, atau rsi6_5m < 40 untuk LONG)
    → squeeze continuation akan tetap berjalan, bukan di‑reverse.
    
    NEW FILTER: Jangan reverse jika OFI bertentangan (OFI LONG kuat saat mau SHORT, atau OFI SHORT kuat saat mau LONG)
    dan volume rendah (volume_ratio < 0.7).
    
    🔥 NEW FILTER 2024: Jika long liq habis dan oversold ekstrem, jangan paksa LONG.
                       Jika short liq habis dan overbought ekstrem, jangan paksa SHORT.
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float) -> Dict:
        # 🔥 NEW: Jika long liq habis dan oversold ekstrem, jangan paksa LONG
        if long_dist < 0.5 and rsi6 < 25 and volume_ratio < 1.0:
            return {"override": False}
        # 🔥 NEW: Jika short liq habis dan overbought ekstrem, jangan paksa SHORT
        if short_dist < 0.5 and rsi6 > 85 and volume_ratio < 1.0:
            return {"override": False}
        # Short liq sangat kecil (<0.5%) dan overbought (RSI>70) dan volume rendah -> reversal ke SHORT
        if short_dist < 0.5 and rsi6 > 70 and volume_ratio < 1.0:
            # Jangan reverse jika overbought dan volume sangat rendah (masih squeeze)
            if volume_ratio < 0.7 and rsi6_5m > 60:
                return {"override": False}
            # Filter OFI bertentangan (OFI LONG kuat) dan volume rendah
            if volume_ratio < 0.7 and ofi_bias == "LONG" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Exhausted liquidity reversal: short liq {short_dist:.2f}% sudah hampir habis, RSI {rsi6:.1f} overbought → HFT akan ambil long stop setelah sapu short",
                "priority": -1060
            }
        # Mirror untuk long liq exhausted dengan oversold
        if long_dist < 0.5 and rsi6 < 30 and volume_ratio < 1.0:
            # Jangan reverse jika oversold dan volume sangat rendah (masih squeeze)
            if volume_ratio < 0.7 and rsi6_5m < 40:
                return {"override": False}
            # Filter OFI bertentangan (OFI SHORT kuat) dan volume rendah
            if volume_ratio < 0.7 and ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Exhausted liquidity reversal: long liq {long_dist:.2f}% sudah hampir habis, RSI {rsi6:.1f} oversold → HFT akan ambil short stop setelah sapu long",
                "priority": -1060
            }
        return {"override": False}


class NearExhaustedLiquidityReversal:
    """
    🔥 DETECTS WHEN LIQUIDITY TARGET IS NEARLY EXHAUSTED (<1.5%) AND MARKET OVERBOUGHT/OVERSOLD
    Overrides strict liquidity to prevent forced LONG when short liq is almost gone.
    Priority -1055 (between ExhaustedLiquidityReversal -1060 and StrictLiquidityProximity -1050)
    
    FILTER: Jangan reverse jika volume sangat rendah (volume_ratio < 0.7) dan 
    RSI 5m overbought/oversold sedang (rsi6_5m > 60 untuk SHORT, atau rsi6_5m < 40 untuk LONG)
    → squeeze continuation akan tetap berjalan, bukan di‑reverse.
    
    NEW FILTER: Jangan reverse jika OFI bertentangan (OFI LONG kuat saat mau SHORT, atau OFI SHORT kuat saat mau LONG)
    dan volume rendah (volume_ratio < 0.7).
    
    🔥 NEW FILTER 2024: Jika long liq mendekati habis dan oversold ekstrem, jangan paksa LONG.
                       Jika short liq mendekati habis dan overbought ekstrem, jangan paksa SHORT.
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float) -> Dict:
        # 🔥 NEW: Jika long liq mendekati habis dan oversold ekstrem, jangan paksa LONG
        if long_dist < 1.5 and rsi6 < 15 and volume_ratio < 1.0:
            return {"override": False}
        # 🔥 NEW: Jika short liq mendekati habis dan overbought ekstrem, jangan paksa SHORT
        if short_dist < 1.5 and rsi6 > 85 and volume_ratio < 1.0:
            return {"override": False}
        # Short liq mendekati habis (<1.5%) dan overbought (RSI>70) -> reversal ke SHORT
        if short_dist < 1.5 and rsi6 > 70 and volume_ratio < 1.0:
            # Jangan reverse jika overbought dan volume sangat rendah (masih squeeze)
            if volume_ratio < 0.7 and rsi6_5m > 60:
                return {"override": False}
            # Filter OFI bertentangan (OFI LONG kuat) dan volume rendah
            if volume_ratio < 0.7 and ofi_bias == "LONG" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Near exhausted liquidity reversal: short liq {short_dist:.2f}% sudah mendekati habis, RSI {rsi6:.1f} overbought → HFT akan ambil long stop setelah sapu short",
                "priority": -1055
            }
        # Long liq mendekati habis (<1.5%) dan oversold (RSI<30) -> reversal ke LONG
        if long_dist < 1.5 and rsi6 < 30 and volume_ratio < 1.0:
            # Jangan reverse jika oversold dan volume sangat rendah (masih squeeze)
            if volume_ratio < 0.7 and rsi6_5m < 40:
                return {"override": False}
            # Filter OFI bertentangan (OFI SHORT kuat) dan volume rendah
            if volume_ratio < 0.7 and ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Near exhausted liquidity reversal: long liq {long_dist:.2f}% sudah mendekati habis, RSI {rsi6:.1f} oversold → HFT akan ambil short stop setelah sapu long",
                "priority": -1055
            }
        return {"override": False}


class ShortSqueezeTrapOverride:
    """
    🔥 Mencegah SHORT trap pada long liq dekat ketika ada buy pressure dan OFI SHORT (short trapped).
    Priority -1060 (antara StrictLiquidityProximity -1050 dan ExhaustedLiquidityReversal -1060)
    """
    @staticmethod
    def detect(short_liq: float, long_liq: float, up_energy: float,
               down_energy: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float, change_5m: float) -> Dict:
        # Long liq lebih dekat, tapi up_energy positif dan OFI SHORT
        if (long_liq < short_liq and
            long_liq < 2.0 and
            up_energy > 1.0 and
            down_energy == 0 and
            volume_ratio < 1.0 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.6 and
            change_5m > 0):  # harga sedang naik
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Short squeeze trap: long liq {long_liq:.2f}% closer, up_energy={up_energy:.2f}, OFI SHORT {ofi_strength:.2f} → short sellers trapped, squeeze up",
                "priority": -1060
            }
        # Mirror: short liq lebih dekat tapi down_energy positif dan OFI LONG
        if (short_liq < long_liq and
            short_liq < 2.0 and
            down_energy > 1.0 and
            up_energy == 0 and
            volume_ratio < 1.0 and
            ofi_bias == "LONG" and
            ofi_strength > 0.6 and
            change_5m < 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Long squeeze trap: short liq {short_liq:.2f}% closer, down_energy={down_energy:.2f}, OFI LONG {ofi_strength:.2f} → long sellers trapped, dump down",
                "priority": -1060
            }
        return {"override": False}

# ================= NEW: SARAN LOGIC FROM LECTURER (LIQUIDITY MAGNET + OFI ABSORPTION + VELOCITY DECAY) =================
class LiquidityMagnetContinuation:
    """
    🔥 Missing logic #1: LIQUIDITY MAGNET MOMENTUM OVERRIDE
    Priority tertinggi (-1000) karena liq magnet adalah faktor paling kuat.
    
    Rule: Jika short_dist < 0.8% dan price sudah pump >4%, maka squeeze akan lanjut.
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, change_5m: float, 
               up_energy: float, down_energy: float, volume_ratio: float) -> Dict:
        # SHORT SQUEEZE CONTINUATION
        if (
            short_dist < 0.8 and
            change_5m > 4.0 and
            up_energy > 0 and
            volume_ratio < 0.8
        ):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity Magnet Continuation: Short liq {short_dist:.2f}% terlalu dekat + momentum {change_5m:.1f}% → HFT akan naikin dikit lagi buat sapu short stop sebelum dump",
                "priority": -1000
            }
        # LONG SQUEEZE CONTINUATION (mirror)
        if (
            long_dist < 0.8 and
            change_5m < -4.0 and
            down_energy > 0 and
            volume_ratio < 0.8
        ):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Liquidity Magnet Continuation: Long liq {long_dist:.2f}% terlalu dekat + momentum {change_5m:.1f}% → HFT akan turunin dikit lagi buat sapu long stop sebelum pump",
                "priority": -1000
            }
        return {"override": False}


class OFIAbsorptionSqueeze:
    """
    🚨 Missing logic #2: OFI SHORT bisa bullish saat squeeze
    Ini jebakan paling brutal. OFI SHORT dengan strength tinggi saat pump = fuel bullish.
    
    Rule: Jika OFI bias SHORT tapi harga tetap naik >5%, berarti selling being absorbed.
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, change_5m: float, 
               short_dist: float, long_dist: float) -> Dict:
        # BULLISH ABSORPTION (OFI SHORT tapi harga naik)
        if (
            ofi_bias == "SHORT" and
            ofi_strength > 0.8 and
            change_5m > 5 and
            short_dist < 1.0
        ):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OFI Absorption Squeeze: Heavy selling (OFI SHORT {ofi_strength:.2f}) absorbed + price up {change_5m:.1f}% + short liq {short_dist:.2f}% → squeeze continues, sell order = short baru masuk atau trapped short averaging",
                "priority": -950
            }
        # BEARISH ABSORPTION (OFI LONG tapi harga turun) - mirror
        if (
            ofi_bias == "LONG" and
            ofi_strength > 0.8 and
            change_5m < -5 and
            long_dist < 1.0
        ):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OFI Absorption Squeeze: Heavy buying (OFI LONG {ofi_strength:.2f}) absorbed + price down {change_5m:.1f}% + long liq {long_dist:.2f}% → dump continues, buy order = long baru masuk atau trapped long averaging",
                "priority": -950
            }
        return {"override": False}


class VelocityDecayReversal:
    """
    ⚡ DETECTOR PALING PENTING YANG BELUM ADA: Velocity decay detector
    Ini pembeda continuation vs reversal.
    
    Rule: Kalau 5m pump besar tapi 30s terakhir melemah + short liq masih jauh = squeeze selesai, baru short valid.
    """
    @staticmethod
    def detect(change_5m: float, change_30s: float, short_dist: float, long_dist: float) -> Dict:
        # PUMP VELOCITY DECAY → SHORT
        if (
            change_5m > 5 and
            change_30s < 0.3 and
            short_dist > 1.5
        ):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Velocity Decay Reversal: 5m pump {change_5m:.1f}% tapi 30s cuma {change_30s:.1f}% + short liq {short_dist:.2f}% sudah jauh → squeeze selesai, reversal incoming",
                "priority": -900
            }
        # DUMP VELOCITY DECAY → LONG - mirror
        if (
            change_5m < -5 and
            change_30s > -0.3 and
            long_dist > 1.5
        ):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Velocity Decay Reversal: 5m dump {change_5m:.1f}% tapi 30s cuma {change_30s:.1f}% + long liq {long_dist:.2f}% sudah jauh → exhaustion selesai, bounce incoming",
                "priority": -900
            }
        return {"override": False}


class EmptyBookMomentum:
    """
    ⚡ Missing logic #3: down_energy = 0 bukan bearish saat momentum tinggi
    Dalam pump continuation artinya: tidak ada seller pressure, path ke atas kosong.
    
    Rule: Jika down_energy == 0 dan price_up >3%, itu berarti thin asks + no resistance → LONG
    """
    @staticmethod
    def detect(down_energy: float, up_energy: float, change_5m: float,
               short_dist: float, long_dist: float) -> Dict:
        # EMPTY DOWNSIDE + MOMENTUM UP → LONG CONTINUATION
        if (
            down_energy == 0 and
            change_5m > 3 and
            short_dist < 1.0
        ):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Empty Book Momentum: No seller pressure (down_energy=0) + price up {change_5m:.1f}% + short liq {short_dist:.2f}% → path ke atas kosong, squeeze continuation",
                "priority": -850
            }
        # EMPTY UPSIDE + MOMENTUM DOWN → SHORT CONTINUATION - mirror
        if (
            up_energy == 0 and
            change_5m < -3 and
            long_dist < 1.0
        ):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Empty Book Momentum: No buyer pressure (up_energy=0) + price down {change_5m:.1f}% + long liq {long_dist:.2f}% → path ke bawah kosong, dump continuation",
                "priority": -850
            }
        return {"override": False}


class MasterSqueezeRule:
    """
    💎 MASTER RULE PALING AMPAH: GOLDEN RULE YANG BELUM ADA
    
    Kalau kondisi kayak gini:
    - price already +5% sampai +10%
    - short liq < 1%
    - volume low
    - OFI SHORT tinggi
    - down_energy = 0
    
    Maka default bias harus: LONG UNTIL SHORT LIQ SWEPT
    
    Priority: -1100 (tertinggi absolut)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, change_5m: float,
               down_energy: float, up_energy: float, volume_ratio: float) -> Dict:
        # MASTER LONG RULE
        if (
            short_dist < 0.8 and
            change_5m > 5 and
            down_energy == 0
        ):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"MASTER SQUEEZE RULE: Short liq {short_dist:.2f}% terlalu dekat + momentum {change_5m:.1f}% + no seller (down_energy=0) → LONG UNTIL SHORT LIQ SWEPT",
                "priority": -1100
            }
        # MASTER SHORT RULE - mirror
        if (
            long_dist < 0.8 and
            change_5m < -5 and
            up_energy == 0
        ):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"MASTER SQUEEZE RULE: Long liq {long_dist:.2f}% terlalu dekat + momentum {change_5m:.1f}% + no buyer (up_energy=0) → SHORT UNTIL LONG LIQ SWEPT",
                "priority": -1100
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: ABSOLUTE AGG OVERRIDE (Priority -1098) =================

class AbsoluteAggOverride:
    """
    🔥 KRIMINAL DETECTOR #1: agg = 1.00 (100% BUY) + down_energy = 0
    
    Ini adalah kondisi paling bullish yang ada:
    - Tidak satu pun trade adalah SELL
    - Tidak ada seller di order book (down_energy = 0)
    - OFI LONG kuat
    
    Tidak peduli liquidity jauh atau RSI berapa — ini LONG.
    HFT tidak bisa dump kalau tidak ada yang mau jual.
    
    Priority: -1098 (hampir setara MasterSqueeze, di bawah VolSpike -1092)
    """
    @staticmethod
    def detect(agg: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, up_energy: float,
               funding_rate: float) -> Dict:

        # 100% BUY + no seller + OFI LONG kuat
        if (agg >= 0.95 and
            down_energy < 0.01 and
            ofi_bias == "LONG" and
            ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"ABSOLUTE AGG: agg={agg:.2f} (100% buy trades), down_energy=0, OFI LONG {ofi_strength:.2f} → tidak ada seller, forced LONG",
                "priority": -1098
            }

        # Mirror: 100% SELL + no buyer
        if (agg <= 0.05 and
            up_energy < 0.01 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"ABSOLUTE AGG: agg={agg:.2f} (100% sell trades), up_energy=0, OFI SHORT {ofi_strength:.2f} → tidak ada buyer, forced SHORT",
                "priority": -1098
            }

        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: RSI 5m VS 1m DIVERGENCE (Priority -1068) =================

class RSI5mVs1mDivergence:
    """
    🔥 DUSDT PATTERN: RSI 5m jauh lebih oversold dari RSI 1m
    
    Kalau RSI_5m < 25 tapi RSI_1m > 30:
    - 5m menunjukkan oversold BESAR (trend turun sudah lama)
    - 1m sudah mulai recover (short-term bounce dimulai)
    - Ini adalah sinyal LONG yang kuat = reversal multi-timeframe
    
    Mirror: RSI_5m > 75 tapi RSI_1m < 70 = distribusi, SHORT
    
    Priority: -1068 (antara AggConfirmedBounce dan ExhaustedLiquidity)
    """
    @staticmethod
    def detect(rsi6_1m: float, rsi6_5m: float,
               down_energy: float, up_energy: float,
               agg: float, funding_rate: float) -> Dict:

        # 5m sangat oversold, 1m sudah recover → bounce multi-TF
        if (rsi6_5m < 25 and
            rsi6_1m > 28 and          # 1m sudah mulai naik
            rsi6_1m - rsi6_5m > 8 and # divergensi minimal 8 poin
            down_energy < 0.1 and
            up_energy > 0):
            
            # Bonus: funding negatif = crowded short = squeeze material
            funding_bonus = " + funding negatif (crowded short)" if funding_rate is not None and funding_rate < -0.001 else ""
            
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"RSI multi-TF divergence: RSI_5m={rsi6_5m:.1f} oversold tapi RSI_1m={rsi6_1m:.1f} recovering, no sellers{funding_bonus} → bounce imminent",
                "priority": -1068
            }

        # Mirror: 5m sangat overbought, 1m sudah mulai turun
        if (rsi6_5m > 75 and
            rsi6_1m < 72 and
            rsi6_5m - rsi6_1m > 8 and
            up_energy < 0.1 and
            down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"RSI multi-TF divergence: RSI_5m={rsi6_5m:.1f} overbought tapi RSI_1m={rsi6_1m:.1f} declining, no buyers → dump imminent",
                "priority": -1068
            }

        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: FUNDING NEGATIVE AGG LONG OVERRIDE (Priority -1072) =================

class FundingNegativeAggLongOverride:
    """
    🔥 DUSDT PATTERN: Funding negatif + agg tinggi + OFI LONG
    
    Funding negatif berarti semua orang short (membayar longs).
    Kalau di atas itu agg tinggi (mayoritas BUY) dan OFI LONG kuat,
    berarti ada akumulasi diam-diam → short squeeze segera terjadi.
    
    Ini tidak butuh liquidity dekat — funding negatif ADALAH trigger-nya.
    
    Priority: -1072 (antara LiquidityMagnetOverride dan AggConfirmedBounce)
    """
    @staticmethod
    def detect(funding_rate: float, agg: float, ofi_bias: str,
               ofi_strength: float, down_energy: float,
               rsi6_5m: float, volume_ratio: float) -> Dict:

        if funding_rate is None:
            return {"override": False}

        # Funding negatif (crowded short) + mayoritas BUY + OFI LONG
        if (funding_rate < -0.001 and       # crowded short threshold
            agg > 0.65 and                  # mayoritas BUY
            ofi_bias == "LONG" and
            ofi_strength > 0.5 and
            down_energy < 0.1 and
            rsi6_5m < 40):                  # tidak overbought di 5m
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Funding crowded short squeeze: funding={funding_rate:.4f} (semua orang short), agg={agg:.2f} (majority buy), OFI LONG {ofi_strength:.2f}, no sellers → short squeeze incoming",
                "priority": -1072
            }

        # Mirror: Funding positif (crowded long) + mayoritas SELL + OFI SHORT
        if (funding_rate > 0.001 and
            agg < 0.35 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.5 and
            up_energy < 0.1 and
            rsi6_5m > 60):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Funding crowded long dump: funding={funding_rate:.4f} (semua orang long), agg={agg:.2f} (majority sell), OFI SHORT {ofi_strength:.2f}, no buyers → long liquidation incoming",
                "priority": -1072
            }

        return {"override": False}


# ================= LECTURER'S SARAN: NEW DETECTORS (Priority -1080 and -165) =================

class ExtremeOversoldIgnoreLiquidity:
    """
    🔥 Memaksa LONG ketika RSI6 < 10, abaikan liquidity proximity.
    Priority: -1080 (setelah MasterSqueezeRule -1100)
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
    Priority: -1080 (setelah MasterSqueezeRule -1100)
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


class CrowdedLongDistribution:
    """
    🔥 Deteksi ketika semua orang sudah LONG (RSI > 70, volume rendah, OFI LONG/netral) → paksa SHORT.
    Priority: -165
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, change_5m: float) -> Dict:
        if (rsi6 > 70 and 
            volume_ratio < 0.9 and 
            (ofi_bias == "LONG" or ofi_bias == "NEUTRAL") and 
            change_5m > 1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Crowded long distribution: RSI {rsi6:.1f}, volume {volume_ratio:.2f}x, OFI {ofi_bias} → smart money distributing, forced SHORT",
                "priority": -165
            }
        return {"override": False}


class CrowdedShortAccumulation:
    """
    🔥 Deteksi ketika semua orang sudah SHORT (RSI < 30, volume rendah, OFI SHORT/netral) → paksa LONG.
    Priority: -165
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, change_5m: float) -> Dict:
        if (rsi6 < 30 and 
            volume_ratio < 0.9 and 
            (ofi_bias == "SHORT" or ofi_bias == "NEUTRAL") and 
            change_5m < -1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Crowded short accumulation: RSI {rsi6:.1f}, volume {volume_ratio:.2f}x, OFI {ofi_bias} → smart money accumulating, forced LONG",
                "priority": -165
            }
        return {"override": False}


class HFTAlgoConsensusOverride:
    """
    🔥 Memaksa mengikuti arah HFT dan Algo Type ketika mereka konsisten,
    volume rendah, dan harga bergerak searah dengan mereka.
    Priority -170 (antara crowded -165 dan OFI dominance -145)
    """
    @staticmethod
    def detect(algo_bias: str, hft_bias: str, volume_ratio: float,
               change_5m: float) -> Dict:
        # Cek konsistensi
        if algo_bias == hft_bias and algo_bias != "NEUTRAL":
            # Volume rendah
            if volume_ratio < 0.7:
                # Opsional: periksa apakah harga bergerak searah (memperkuat)
                if (algo_bias == "SHORT" and change_5m < 0) or \
                   (algo_bias == "LONG" and change_5m > 0):
                    return {
                        "override": True,
                        "bias": algo_bias,
                        "reason": f"HFT-Algo consensus: both {algo_bias}, volume {volume_ratio:.2f}x, price moving {change_5m:+.1f}% → forcing {algo_bias}",
                        "priority": -170
                    }
                # Jika harga tidak searah, tetap ikuti tapi dengan confidence lebih rendah
                else:
                    return {
                        "override": True,
                        "bias": algo_bias,
                        "reason": f"HFT-Algo consensus: both {algo_bias} with low volume ({volume_ratio:.2f}x) → forcing {algo_bias}",
                        "priority": -170
                    }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: TWO-PHASE HFT SWEEP DETECTOR =================

class TwoPhaseHFTSweepDetector:
    """
    Mendeteksi rencana HFT 2 fase:
    Fase 1: Pump untuk sweep short liq (jika short_dist < 0.5%)
    Fase 2: Dump untuk sweep long liq setelah short liq habis
    
    Kunci: Jika SHORT liq sangat dekat (<0.5%) tapi RSI sudah overbought
    ekstrem DAN long_liq jauh lebih besar, maka:
    - Jangka pendek (fase 1): LONG (sweep short dulu)
    - Jangka menengah (fase 2): SHORT (dump setelah sweep)
    
    Priority: -1095 (antara MasterSqueeze -1100 dan HFTExplicitDump -1090)
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float,
               down_energy: float, up_energy: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float) -> Dict:
        
        # Fase 1: Short liq SANGAT dekat, belum disapu → paksa LONG dulu
        if (short_dist < 0.5 and
            short_dist < long_dist * 0.1 and  # short liq << long liq
            rsi6 > 75 and                       # overbought (distribusi di atas)
            down_energy < 0.01 and              # tidak ada seller aktif
            change_5m > 2.0):                   # momentum masih naik
            return {
                "override": True,
                "bias": "LONG",
                "phase_type": "TWO_PHASE_SWEEP_PHASE1",
                "reason": f"Two-phase HFT sweep: short liq {short_dist:.2f}% << long liq {long_dist:.2f}%, RSI {rsi6:.1f} overbought, no sellers → HFT pump dulu untuk sapu short stop, baru dump",
                "priority": -1095
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: VOLUME SPIKE BOUNCE DETECTOR =================

class VolumeSpikeBounceDetector:
    """
    🔥 KUNCI EDGEUSDT: Volume spike 4x+ dari MA10 saat oversold
    = institutional accumulation, BUKAN distribusi.
    
    Rule: Jika latest_volume > volume_ma10 * 2.5 DAN oversold DAN
    ada buy pressure (up_energy > 0 DAN agg > 0.55) → paksa LONG.
    
    Ini menangkap: panic sell habis → institusi masuk besar-besaran → bounce
    Priority: -1092 (antara MasterSqueeze -1100 dan TwoPhaseHFT -1095)
    """
    @staticmethod
    def detect(latest_volume: float, volume_ma10: float, rsi6: float,
               up_energy: float, down_energy: float, agg: float,
               change_5m: float, long_liq: float, short_liq: float) -> Dict:
        
        if volume_ma10 <= 0:
            return {"override": False}
        
        volume_spike_ratio = latest_volume / volume_ma10
        
        # Volume spike + oversold + buy pressure = institutional accumulation
        if (volume_spike_ratio > 2.5 and
            rsi6 < 30 and
            up_energy > 0.5 and
            down_energy < 0.1 and
            agg > 0.55 and           # mayoritas trades adalah BUY
            change_5m < 0):          # harga masih turun (panic selling)
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Volume spike bounce: volume {volume_spike_ratio:.1f}x MA10 saat oversold RSI {rsi6:.1f}, up_energy={up_energy:.2f}, agg={agg:.2f} (majority buy) → institutional accumulation, bounce imminent",
                "priority": -1092
            }
        
        # Mirror: volume spike + overbought + sell pressure = distribution
        if (volume_spike_ratio > 2.5 and
            rsi6 > 70 and
            down_energy > 0.5 and
            up_energy < 0.1 and
            agg < 0.45 and           # mayoritas trades adalah SELL
            change_5m > 0):          # harga masih naik (FOMO buying)
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Volume spike distribution: volume {volume_spike_ratio:.1f}x MA10 saat overbought RSI {rsi6:.1f}, down_energy={down_energy:.2f}, agg={agg:.2f} (majority sell) → institutional distribution, dump imminent",
                "priority": -1092
            }
        
        return {"override": False}


class AggMajorityBounce:
    """
    🔥 Ketika agg > 0.55 (mayoritas BUY), RSI oversold, long_liq dekat,
    dan volume rendah → HFT sedang akumulasi, bounce imminent.
    Priority -1072 (antara VolumeSpikeBounce -1092 dan AggConfirmedBounce -1070)
    """
    @staticmethod
    def detect(agg: float, rsi6: float, long_liq: float, change_5m: float,
               up_energy: float, down_energy: float) -> Dict:
        if (agg > 0.55 and
            rsi6 < 35 and
            long_liq < 3.0 and
            change_5m < 0 and
            up_energy > down_energy and
            down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Agg majority bounce: agg={agg:.2f} (majority buy), RSI {rsi6:.1f} oversold, long liq {long_liq:.2f}%, up_energy={up_energy:.2f} → accumulation, bounce imminent",
                "priority": -1072
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: OFI DIVERGENCE TRAP =================

class OFIDivergenceTrap:
    """
    Ketika OFI SHORT kuat (strength > 0.4) tapi:
    - down_energy = 0 (tidak ada sell pressure nyata di book)
    - harga masih naik (change_5m > 0)
    - short liq dekat
    
    Artinya: order sell yang masuk LANGSUNG DISERAP oleh buyer besar.
    Ini adalah tanda squeeze kuat, bukan sinyal SHORT.
    
    Priority: -855 (sedikit di bawah EmptyBookMomentum -850)
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               change_5m: float, short_dist: float, volume_ratio: float) -> Dict:
        if (ofi_bias == "SHORT" and
            ofi_strength > 0.3 and
            down_energy < 0.01 and       # tidak ada seller di book
            change_5m > 0 and            # harga masih naik
            short_dist < 1.5 and         # short liq dekat
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OFI divergence trap: OFI SHORT {ofi_strength:.2f} tapi down_energy=0 dan harga naik {change_5m:.1f}% → sell orders diserap buyer, squeeze lanjut",
                "priority": -855
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: RSI-ENERGY DIVERGENCE =================

class RSIEnergyDivergence:
    """
    RSI overbought tapi energy = 0 di kedua sisi = pasar frozen.
    Pasar frozen dengan short liq dekat → harga akan bergerak ke short liq.
    
    Priority: -856
    """
    @staticmethod
    def detect(rsi6: float, up_energy: float, down_energy: float,
               short_dist: float, long_dist: float) -> Dict:
        if (rsi6 > 75 and
            up_energy == 0 and
            down_energy == 0 and
            short_dist < long_dist):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"RSI-Energy divergence: RSI {rsi6:.1f} overbought tapi energy nol di keduanya, short liq {short_dist:.2f}% lebih dekat → frozen market, harga ke short liq",
                "priority": -856
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: HFT EXPLICIT DUMP OVERRIDE =================

class HFTExplicitDumpOverride:
    """
    🚨 KRIMINAL DETECTOR: Ketika HFT 6% bias = SHORT dengan reason eksplisit
    (bukan hanya "liq lebih dekat") dan konfirmasi dari agg/flow rendah,
    paksa SHORT meski sinyal lain LONG.
    
    Ini menangkap kasus HFT dump yang disengaja:
    - HFT bilang SHORT (dengan reason "energi down murah / akan dump")
    - agg < 0.3 (79%+ trades adalah SELL)
    - OFI SHORT
    - Harga sudah pump 3%+ dalam 5m (distribusi di atas)
    
    Priority: -1090 (antara MasterSqueeze -1100 dan ExtremeOversold -1080)
    """
    @staticmethod
    def detect(hft_bias: str, hft_reason: str, agg: float, 
               ofi_bias: str, change_5m: float, 
               funding_rate: float, volume_ratio: float) -> Dict:
        
        # HFT eksplisit bilang dump
        hft_dump_signal = (
            hft_bias == "SHORT" and 
            hft_reason != "" and  # ada alasan spesifik, bukan kosong
            ("dump" in hft_reason.lower() or "energi down" in hft_reason.lower())
        )
        
        if not hft_dump_signal:
            return {"override": False}
        
        # Konfirmasi: agg sangat rendah (mayoritas sell)
        if agg > 0.35:
            return {"override": False}
        
        # Konfirmasi: OFI searah
        if ofi_bias != "SHORT":
            return {"override": False}
        
        # Konfirmasi: harga sudah pump (distribusi di atas setelah pump)
        if change_5m < 1.0:
            return {"override": False}
        
        return {
            "override": True,
            "bias": "SHORT",
            "reason": f"HFT explicit dump signal: HFT={hft_reason}, agg={agg:.2f} (79%+ sell), OFI SHORT, price pumped {change_5m:.1f}% → HFT distributing before dump",
            "priority": -1090
        }


# ================= LECTURER'S SARAN LOGIC: EXTREME FUNDING RATE TRAP =================

class ExtremeFundingRateTrap:
    """
    💰 FUNDING RATE EXTREME TRAP
    
    Funding rate normal: ±0.001 (0.1%)
    Funding rate ekstrem: < -0.005 atau > +0.005
    
    Logika:
    - Funding rate SANGAT NEGATIF (< -0.005): longs membayar shorts mahal.
      Artinya market sedang long trap besar. Kalau bersamaan dengan agg rendah
      dan HFT SHORT → ini adalah setup dump klasik.
      
    - Funding rate SANGAT POSITIF (> +0.005): shorts membayar longs mahal.
      Artinya market sedang short trap besar → setup squeeze klasik.
    
    Priority: -1085 (setelah MasterSqueeze, sebelum ExtremeOversold)
    """
    @staticmethod
    def detect(funding_rate: float, agg: float, hft_bias: str,
               ofi_bias: str, ofi_strength: float,
               change_5m: float, volume_ratio: float) -> Dict:
        
        if funding_rate is None:
            return {"override": False}
        
        # KASUS 1: Funding sangat negatif = long trap
        # Semua orang long, HFT akan dump untuk liquidate mereka
        if (funding_rate < -0.005 and        # ekstrem negatif
            agg < 0.35 and                    # mayoritas sell sudah masuk
            hft_bias == "SHORT" and           # HFT konfirmasi dump
            ofi_bias == "SHORT" and           # order flow confirm
            change_5m > 1.0):                 # harga sudah pump (distribusi)
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme funding trap SHORT: funding {funding_rate:.4f} (longs paying {abs(funding_rate)*100:.2f}%), agg={agg:.2f}, HFT SHORT → mass long liquidation incoming",
                "priority": -1085
            }
        
        # KASUS 2: Funding sangat positif = short trap  
        # Semua orang short, HFT akan pump untuk liquidate mereka
        if (funding_rate > 0.005 and          # ekstrem positif
            agg > 0.65 and                    # mayoritas buy masuk
            hft_bias == "LONG" and            # HFT konfirmasi pump
            ofi_bias == "LONG" and            # order flow confirm
            change_5m < -1.0):               # harga sudah dump (akumulasi)
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme funding trap LONG: funding {funding_rate:.4f} (shorts paying {funding_rate*100:.2f}%), agg={agg:.2f}, HFT LONG → mass short liquidation incoming",
                "priority": -1085
            }
        
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: EXTREME FUNDING LIQUIDITY OVERRIDE =================

class ExtremeFundingLiquidityOverride:
    """
    🔥 Ketika funding rate sangat negatif (< -0.005) tapi long liq lebih dekat dari short liq,
    maka HFT akan ambil long stops dulu → SHORT, bukan LONG.
    Priority -1086 (lebih tinggi dari ExtremeFundingRateTrap -1085)
    """
    @staticmethod
    def detect(funding_rate: float, long_liq: float, short_liq: float, 
               hft_bias: str, agg: float, change_5m: float) -> Dict:
        if funding_rate is None:
            return {"override": False}
        # Funding sangat negatif, long liq lebih dekat, HFT SHORT, agg tidak terlalu ekstrem beli
        if (funding_rate < -0.005 and 
            long_liq < short_liq and 
            hft_bias == "SHORT" and 
            agg < 0.8 and
            abs(change_5m) < 2.0):  # harga sideways, belum pump/dump
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme negative funding ({funding_rate:.4f}) but long liq closer ({long_liq:.2f}% < {short_liq:.2f}%) → HFT will take long stops first, forced SHORT",
                "priority": -1086
            }
        # Mirror untuk funding positif ekstrem dengan short liq lebih dekat
        if (funding_rate > 0.005 and 
            short_liq < long_liq and 
            hft_bias == "LONG" and 
            agg > 0.2 and
            abs(change_5m) < 2.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme positive funding ({funding_rate:.4f}) but short liq closer ({short_liq:.2f}% < {long_liq:.2f}%) → HFT will take short stops first, forced LONG",
                "priority": -1086
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: AGG/FLOW DIVERGENCE FILTER =================

class AggFlowDivergenceFilter:
    """
    🔥 Ketika agg/flow < 0.25 (75%+ trades adalah SELL) dan harga pump > 2%,
    itu adalah distribusi — smart money jual di atas ke retail buyer.
    Jangan LONG dalam kondisi ini.
    
    Ini menangkap: pump + distribusi = dump incoming
    Priority: -175 (setelah HFTAlgoConsensus -170)
    """
    @staticmethod
    def detect(agg: float, change_5m: float, ofi_bias: str, 
               hft_bias: str, volume_ratio: float) -> Dict:
        # Dead aggregation — hampir semua sell tapi harga naik = distribusi
        if (agg < 0.25 and
            change_5m > 2.0 and        # harga sudah naik
            ofi_bias == "SHORT" and     # OFI confirm sell pressure
            volume_ratio < 0.7):        # volume rendah = tidak ada demand asli
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Agg/Flow divergence: {agg:.2f} agg (75%+ sell trades) with price up {change_5m:.1f}% + low volume → distribution, dump incoming",
                "priority": -175
            }
        
        # Mirror: hampir semua buy tapi harga turun = akumulasi
        if (agg > 0.75 and
            change_5m < -2.0 and
            ofi_bias == "LONG" and
            volume_ratio < 0.7):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Agg/Flow divergence: {agg:.2f} agg (75%+ buy trades) with price down {change_5m:.1f}% + low volume → accumulation, bounce incoming",
                "priority": -175
            }
        
        return {"override": False}


# ================= NEW DETECTORS =================

class LiquidityProximityStrict:
    @staticmethod
    def detect(short_dist: float, long_dist: float, volume_ratio: float, rsi6_5m: float,
               ofi_bias: str, ofi_strength: float, rsi6: float, obv_trend: str, change_5m: float) -> Dict:
        # 🔥 Jika long liq sangat dekat (<1.0%) dan lebih dekat dari short liq → paksa SHORT mutlak
        if long_dist < 1.0 and long_dist < short_dist:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Ultra close long liq ({long_dist:.2f}%) → forced SHORT regardless",
                "priority": -1051
            }
        # 🔥 Jika short liq sangat dekat (<1.0%) dan lebih dekat dari long liq → paksa LONG mutlak
        if short_dist < 1.0 and short_dist < long_dist:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Ultra close short liq ({short_dist:.2f}%) → forced LONG regardless",
                "priority": -1051
            }
        if volume_ratio < 1.5:
            # 🔥 Block jika extreme overbought/oversold dengan volume rendah
            if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
                return {"override": False}
            # 🔥 Blokir jika arah bertentangan dengan OFI kuat
            if short_dist < 2.0 and short_dist < long_dist:   # akan paksa LONG
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
            if long_dist < 2.0 and long_dist < short_dist:   # akan paksa SHORT
                # 🔥 Filter DUSDT: Jangan paksa SHORT jika oversold dan OFI LONG kuat dengan volume rendah
                if rsi6 < 35 and ofi_bias == "LONG" and ofi_strength > 0.7 and volume_ratio < 0.7:
                    return {"override": False}
                # 🔥 Filter STOUSDT: Jangan paksa SHORT jika OFI netral, oversold, dan volume rendah
                if ofi_bias == "NEUTRAL" and rsi6_5m < 35 and volume_ratio < 0.6:
                    return {"override": False}
                # 🔥 Filter STOUSDT (lanjutan): Jangan paksa SHORT jika oversold, volume rendah, OFI tidak kuat, dan harga sudah turun
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
    Priority -1075, above OverboughtDistributionTrap (-261).
    
    Case studies:
    - NOMUSDT: short_dist = 2.38% (<3), volume_ratio = 0.59 (<0.7), short_dist < long_dist → override ke LONG (+8%)
    - ARIAUSDT: short_dist = 2.06% (<3), volume_ratio = 0.51 (<0.7), short_dist < long_dist → override ke LONG
    - BASUSDT: short_dist = 2.27% (<3), volume_ratio = 0.28 (<0.7), short_dist < long_dist → override ke LONG (+8%)
    
    Threshold lebih luas dari versi sebelumnya (2.5%→3%, 0.5x→0.7x) untuk menangkap lebih banyak squeeze plays.
    
    FILTER: Block jika extreme overbought/oversold dengan volume rendah
    """
    @staticmethod
    def detect(short_dist: float, long_dist: float, volume_ratio: float,
               rsi6_5m: float, change_5m: float) -> Dict:
        # 🔥 Block jika extreme overbought/oversold dengan volume rendah
        if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
            return {"override": False}
        # SHORT LIQ CLOSE (<3%) AND VOLUME LOW (<0.7) AND SHORT LIQ CLOSER → FORCE LONG
        if short_dist < 3.0 and volume_ratio < 0.7 and short_dist < long_dist:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity squeeze override: short liq {short_dist:.2f}% dekat dengan volume {volume_ratio:.2f}x, lebih dekat dari long liq → force LONG (HFT will sweep short stops)",
                "priority": -1075
            }
        # LONG LIQ CLOSE (<3%) AND VOLUME LOW (<0.7) AND LONG LIQ CLOSER → FORCE SHORT
        if long_dist < 3.0 and volume_ratio < 0.7 and long_dist < short_dist:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Liquidity squeeze override: long liq {long_dist:.2f}% dekat dengan volume {volume_ratio:.2f}x, lebih dekat dari short liq → force SHORT (HFT will dump to sweep long stops)",
                "priority": -1075
            }
        return {"override": False}


class FundingRateCrowdedShortOverride:
    """
    🔥 Memaksa LONG ketika funding rate sangat negatif (crowded short),
    RSI oversold, OFI LONG, dan short liq lebih dekat.
    Priority -1076 (antara LiquidityMagnetOverride -1075 dan ExhaustedLiquidityReversal -1060)
    """
    @staticmethod
    def detect(funding_rate: float, rsi6: float, ofi_bias: str, ofi_strength: float,
               long_dist: float, short_dist: float, volume_ratio: float) -> Dict:
        if (funding_rate is not None and 
            funding_rate < -0.001 and           # sangat negatif = crowded short
            rsi6 < 35 and                       # oversold
            ofi_bias == "LONG" and 
            ofi_strength > 0.4 and
            short_dist < long_dist and          # short liq lebih dekat (target untuk squeeze)
            volume_ratio < 0.8):                # volume rendah
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Crowded short trap: funding rate {funding_rate:.4f} sangat negatif, RSI {rsi6:.1f} oversold, OFI LONG {ofi_strength:.2f}, short liq {short_dist:.2f}% < long liq {long_dist:.2f}% → shorts trapped, squeeze up",
                "priority": -1076
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: AGG CONFIRMED BOUNCE =================

class AggConfirmedBounce:
    """
    🔥 Ketika agg > 0.55 (mayoritas BUY), RSI oversold, dan 
    long_liq dekat — ini adalah setup bounce yang sangat kuat.
    HFT akan pump untuk menyapu long liq lalu reverse.
    
    Priority: -1070 (antara ExhaustedLiquidityReversal dan LiquidityMagnetOverride)
    """
    @staticmethod
    def detect(agg: float, rsi6: float, long_liq: float, short_liq: float,
               up_energy: float, down_energy: float, change_5m: float) -> Dict:
        
        # Agg tinggi (banyak buyer) + oversold + long liq dekat = bounce kuat
        if (agg > 0.55 and
            rsi6 < 35 and
            long_liq < 2.0 and
            up_energy > down_energy and
            down_energy < 0.1 and
            change_5m < 0):          # harga masih turun = entry bagus
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Agg-confirmed bounce: agg={agg:.2f} (majority buy), RSI {rsi6:.1f} oversold, long liq {long_liq:.2f}% close, up_energy={up_energy:.2f} > down_energy={down_energy:.2f} → HFT sweep long liq then pump",
                "priority": -1070
            }
        
        # Mirror: Agg rendah + overbought + short liq dekat = dump kuat
        if (agg < 0.45 and
            rsi6 > 65 and
            short_liq < 2.0 and
            down_energy > up_energy and
            up_energy < 0.1 and
            change_5m > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Agg-confirmed dump: agg={agg:.2f} (majority sell), RSI {rsi6:.1f} overbought, short liq {short_liq:.2f}% close, down_energy={down_energy:.2f} > up_energy={up_energy:.2f} → HFT sweep short liq then dump",
                "priority": -1070
            }
        
        return {"override": False}


class FlushExhaustionReversal:
    """
    🚀 Detects sharp drop with oversold, low volume, and no sellers → bounce.
    Priority -250 (similar to EnergyGapTrap).
    """
    @staticmethod
    def detect(change_5m: float, rsi6: float, volume_ratio: float,
               down_energy: float, long_dist: float) -> Dict:
        if (change_5m < -5.0 and
            rsi6 < 30 and
            volume_ratio < 0.7 and
            down_energy < 0.05 and
            long_dist < 3.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Flush exhaustion reversal: dropped {change_5m:.1f}% with low volume, RSI {rsi6:.1f} oversold, no sellers, long liq {long_dist}% close → bounce likely",
                "priority": -250
            }
        return {"override": False}


# ================= NEW: EXTREME OVERBOUGHT/OVERSOLD CONTINUATION =================
class ExtremeOverboughtContinuation:
    """
    🔥 Memaksa LONG ketika overbought ekstrem (RSI5m > 80), volume sangat rendah (<0.5x),
    OFI LONG kuat (>0.5), dan up_energy > 0. Ini adalah squeeze continuation.
    Priority -200.
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               up_energy: float, short_liq: float) -> Dict:
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
    🔥 Memaksa SHORT ketika oversold ekstrem (RSI5m < 20), volume sangat rendah (<0.5x),
    OFI SHORT kuat (>0.5), dan down_energy > 0. Ini adalah dump continuation.
    Priority -200.
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, long_liq: float) -> Dict:
        if (rsi6_5m < 20 and
            volume_ratio < 0.5 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.5 and
            down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme oversold with strong OFI SHORT: RSI5m {rsi6_5m:.1f}, volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → dump continuation",
                "priority": -200
            }
        return {"override": False}


# ================= NEW: EXTREME OVERSOLD/SHORT CONTINUATION (LECTURER'S LOGIC) =================
class ExtremeOversoldShortContinuation:
    """
    🔥 Memaksa SHORT ketika oversold ekstrem (RSI6 < 20), volume sangat rendah (<0.6x),
    OFI SHORT kuat (>0.6), dan down_energy > 0.
    Priority -203 (lebih tinggi dari ExtremeOverboughtContinuation -200).
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, long_liq: float) -> Dict:
        if (rsi6 < 20 and
            volume_ratio < 0.6 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.6 and
            down_energy > 0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme oversold with strong OFI SHORT: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → dump continuation",
                "priority": -203
            }
        return {"override": False}

class ExtremeOverboughtLongContinuation:
    """
    🔥 Memaksa LONG ketika overbought ekstrem (RSI6 > 80), volume sangat rendah (<0.6x),
    OFI LONG kuat (>0.6), dan up_energy > 0.
    Priority -202.
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               up_energy: float, short_liq: float) -> Dict:
        if (rsi6 > 80 and
            volume_ratio < 0.6 and
            ofi_bias == "LONG" and
            ofi_strength > 0.6 and
            up_energy > 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme overbought with strong OFI LONG: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, OFI strength {ofi_strength:.2f} → squeeze continuation",
                "priority": -202
            }
        return {"override": False}


# ================= NEW: Oversold/Overbought False Bounce Trap =================
class OversoldFalseBounceTrap:
    """
    🔥 Mendeteksi false bounce pada oversold: OFI LONG kuat tetapi harga masih turun.
    Memaksa SHORT.
    Priority -201.
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               change_5m: float, long_liq: float) -> Dict:
        if (rsi6 < 25 and
            volume_ratio < 0.8 and
            ofi_bias == "LONG" and
            ofi_strength > 0.8 and
            change_5m < -2.0):
            # 🔥 Jika long liq sangat dekat, ini potensi short squeeze → jangan paksa SHORT
            if long_liq < 2.0:
                return {"override": False}
            # 🔥 Jika harga sudah turun sangat dalam (>5%), ini exhaustion → jangan paksa SHORT
            if change_5m < -5.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Oversold false bounce: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, strong OFI LONG {ofi_strength:.2f} but price still down {change_5m:.1f}% → dump continues",
                "priority": -201
            }
        return {"override": False}

class OverboughtFalseBounceTrap:
    """
    🔥 Mendeteksi false bounce pada overbought: OFI SHORT kuat tetapi harga masih naik.
    Memaksa LONG.
    Priority -201.
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, ofi_bias: str, ofi_strength: float,
               change_5m: float, short_liq: float) -> Dict:
        if (rsi6 > 75 and
            volume_ratio < 0.8 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.8 and
            change_5m > 2.0):
            # 🔥 Jika short liq sangat dekat, ini potensi short squeeze → jangan paksa LONG
            if short_liq < 2.0:
                return {"override": False}
            if change_5m > 5.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Overbought false bounce: RSI6 {rsi6:.1f}, volume {volume_ratio:.2f}x, strong OFI SHORT {ofi_strength:.2f} but price still up {change_5m:.1f}% → pump continues",
                "priority": -201
            }
        return {"override": False}


# ================= NEW: EXTREME OVERSOLD/OVERBOUGHT BOUNCE/DUMP OVERRIDE =================
class ExtremeOversoldBounceOverride:
    """
    🔥 Memaksa LONG pada oversold ekstrem dengan OFI LONG kuat dan harga sudah turun dalam.
    Priority -150 (cukup tinggi untuk mengalahkan voting/probabilistic).
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, long_liq: float) -> Dict:
        if (rsi6 < 25 and
            volume_ratio < 0.8 and
            change_5m < -5.0 and
            ofi_bias == "LONG" and
            ofi_strength > 0.7):
            # 🔥 Jangan paksa LONG jika long liq sangat dekat (potensi long squeeze / dump lanjut)
            if long_liq < 1.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme oversold with strong OFI LONG: price down {change_5m:.1f}%, RSI {rsi6:.1f}, volume {volume_ratio:.2f}x → bounce imminent",
                "priority": -150
            }
        return {"override": False}

class ExtremeOverboughtDumpOverride:
    """
    🔥 Memaksa SHORT pada overbought ekstrem dengan OFI SHORT kuat dan harga sudah naik dalam.
    Priority -150 (simetris).
    """
    @staticmethod
    def detect(rsi6: float, volume_ratio: float, change_5m: float,
               ofi_bias: str, ofi_strength: float, short_liq: float, up_energy: float) -> Dict:
        # 🔥 Jangan paksa SHORT jika short liq masih jauh (>2%) dan ada buy pressure (up_energy > 0.1)
        if short_liq > 2.0 and up_energy > 0.1:
            return {"override": False}
        
        if (rsi6 > 75 and
            volume_ratio < 0.8 and
            change_5m > 5.0 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.7):
            # 🔥 Jangan paksa SHORT jika short liq sangat dekat (potensi short squeeze)
            if short_liq < 1.5:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme overbought with strong OFI SHORT: price up {change_5m:.1f}%, RSI {rsi6:.1f}, volume {volume_ratio:.2f}x → dump imminent",
                "priority": -150
            }
        return {"override": False}


# ================= NEW: EXHAUSTION DUMP OVERRIDE (BLOW-OFF TOP) =================
class ExhaustionDumpOverride:
    """
    🔥 Mendeteksi blow-off top: harga naik tinggi, volume rendah, energy collapse.
    Memaksa SHORT.
    Priority -130.
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, change_5m: float,
               up_energy: float, short_liq: float) -> Dict:
        if (rsi6_5m > 85 and
            volume_ratio < 0.5 and
            change_5m > 5.0 and
            up_energy < 0.1):
            # Jangan paksa SHORT jika short liq sangat dekat (masih squeeze)
            if short_liq < 1.0:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Exhaustion dump: price up {change_5m:.1f}%, RSI5m {rsi6_5m:.1f}, volume {volume_ratio:.2f}x, energy collapsed → dump imminent",
                "priority": -130
            }
        return {"override": False}


# ================= NEW: ULTRA CLOSE SQUEEZE OVERRIDE =================
class UltraCloseSqueezeOverride:
    """
    🔥 Memaksa LONG ketika short liq sangat dekat (<0.5%), OFI SHORT kuat,
    down_energy=0, volume rendah, dan harga tidak turun signifikan.
    Priority -155 (lebih tinggi dari OFI dominance -145).
    """
    @staticmethod
    def detect(short_liq: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, volume_ratio: float, change_5m: float) -> Dict:
        if (short_liq < 0.5 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.7 and
            down_energy < 0.01 and
            volume_ratio < 0.7 and
            change_5m > -1.0):  # harga tidak turun drastis
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Ultra close squeeze: short liq {short_liq:.2f}%, strong OFI SHORT {ofi_strength:.2f}, no sellers → forced LONG",
                "priority": -155
            }
        return {"override": False}


# ================= NEW: ABSORPTION REVERSAL OVERRIDE (BEAR TRAP) =================
class AbsorptionReversalOverride:
    """
    🔥 Mendeteksi bear trap: OFI SHORT kuat tapi harga tidak turun (down_energy=0),
    volume rendah, dan short liq dekat. Memaksa LONG.
    Priority -135 (di atas OFI dominance).
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               change_5m: float, short_liq: float, volume_ratio: float) -> Dict:
        if (ofi_bias == "SHORT" and
            ofi_strength > 0.7 and
            down_energy < 0.01 and
            volume_ratio < 0.7 and
            short_liq < 3.0 and
            change_5m > -1.0):  # harga tidak turun signifikan
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Absorption reversal: strong OFI SHORT {ofi_strength:.2f} but no sellers (down_energy=0) and short liq {short_liq:.2f}% close → bear trap, squeeze up",
                "priority": -135
            }
        return {"override": False}


class OversoldLiquidityBounce:
    """
    🔥 Memaksa LONG pada oversold (RSI6_5m < 30), volume rendah (<0.6),
    long liq dekat (<5%), dan down_energy = 0.
    Priority -138 (antara absorption reversal dan OFI dominance).
    
    ⚠️ MODIFIED: Tambahkan filter agar tidak override ketika HFT/Algo konsisten SHORT (falling knife).
    """
    @staticmethod
    def detect(rsi6_5m: float, volume_ratio: float, long_liq: float, down_energy: float,
               algo_bias: str = None, hft_bias: str = None, change_5m: float = None) -> Dict:
        # 🔥 Filter: Jangan LONG jika HFT/Algo konsisten SHORT dan harga turun (falling knife)
        if (algo_bias is not None and hft_bias is not None and change_5m is not None):
            if algo_bias == "SHORT" and hft_bias == "SHORT" and change_5m < 0:
                return {"override": False}  # falling knife, jangan LONG
        
        if (rsi6_5m < 30 and
            volume_ratio < 0.6 and
            long_liq < 5.0 and
            down_energy < 0.01):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Oversold liquidity bounce (5m): RSI5m {rsi6_5m:.1f}, volume {volume_ratio:.2f}x, long liq {long_liq:.2f}%, no sellers → bounce likely",
                "priority": -138
            }
        return {"override": False}


class LiquidityAbsorptionReversal:
    """
    🔥 Memaksa LONG ketika long liq tidak terlalu dekat (>2%), RSI netral (>=30),
    OFI SHORT kuat, down_energy=0, volume rendah, dan harga turun sedikit.
    Priority -136 (lebih rendah dari -138 sehingga tidak menimpa bounce,
    tapi lebih tinggi dari -145 untuk mengalahkan OFI dominance jika perlu).
    """
    @staticmethod
    def detect(long_liq: float, rsi6: float, ofi_bias: str, ofi_strength: float,
               down_energy: float, volume_ratio: float, change_5m: float) -> Dict:
        if (long_liq > 2.0 and
            rsi6 >= 30 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.6 and
            down_energy < 0.01 and
            volume_ratio < 0.8 and
            change_5m < 0):  # harga turun sedikit (bisa -0.5 sampai -2%)
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity absorption reversal: long liq {long_liq:.2f}%, RSI {rsi6:.1f} netral, strong OFI SHORT {ofi_strength:.2f}, no sellers → bear trap, bounce up",
                "priority": -136
            }
        return {"override": False}


class OversoldLiquidityContinuation:
    """
    🔥 Memaksa SHORT pada oversold dengan long liq sangat dekat (<1.5%),
    OFI SHORT kuat atau netral, down_energy=0, harga turun.
    Priority -139 (lebih tinggi dari oversold liquidity bounce -138).
    
    ⚠️ PATCH EDGEUSDT: Jika ada buy pressure nyata, ini BUKAN dump continuation
    """
    @staticmethod
    def detect(volume_ratio: float, long_liq: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, change_5m: float, rsi6: float,
               up_energy: float = 0.0, agg: float = 0.5,
               latest_volume: float = 0.0, volume_ma10: float = 1.0) -> Dict:
        
        # 🔥 PATCH EDGEUSDT: Jika ada buy pressure nyata, ini BUKAN dump continuation
        # up_energy > 1.0 = ada buyer aktif di book
        if up_energy > 1.0:
            return {"override": False}  # ada buyer, jangan paksa SHORT
        
        # agg > 0.55 = mayoritas trades adalah BUY
        # Volume spike > 1.5x = institutional entry
        if agg > 0.55 and volume_ma10 > 0 and (latest_volume / volume_ma10) > 1.5:
            return {"override": False}  # volume spike dengan buy majority = bounce
        
        if (volume_ratio < 0.7 and
            long_liq < 1.5 and
            down_energy < 0.01 and
            (ofi_bias == "SHORT" or ofi_bias == "NEUTRAL") and
            change_5m < -1.0 and
            rsi6 < 35):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Oversold liquidity continuation: long liq {long_liq:.2f}%, RSI6 {rsi6:.1f}, down_energy=0 → dump continues",
                "priority": -139
            }
        return {"override": False}


class FallingKnifeOverride:
    """
    🔥 Mencegah LONG trap pada oversold dengan long liq dekat
    ketika HFT dan Algo Type konsisten SHORT dan volume rendah.
    Priority -139 (lebih tinggi dari OversoldLiquidityBounce -138)
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, long_liq: float,
               volume_ratio: float, up_energy: float, down_energy: float,
               algo_bias: str, hft_bias: str, change_5m: float) -> Dict:
        # Oversold + long liq dekat + volume rendah + HFT/Algo konsisten SHORT
        if (rsi6 < 25 and
            rsi6_5m < 35 and
            long_liq < 3.0 and
            volume_ratio < 0.7 and
            up_energy > 0 and
            down_energy == 0 and
            algo_bias == "SHORT" and
            hft_bias == "SHORT" and
            change_5m < 0):  # harga sedang turun
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Falling knife: oversold (RSI6 {rsi6:.1f}) with long liq {long_liq:.2f}%, low volume, but HFT+Algo SHORT, down_energy=0 → no support, continuing down",
                "priority": -139
            }
        return {"override": False}


class ExtremeOversoldCloseLiquidityBounce:
    """
    🔥 Memaksa LONG ketika long liq sangat dekat (<0.5%) dan oversold (RSI < 25)
    serta ada buy pressure (up_energy > 0). Ini mengalahkan oversold continuation.
    Priority -141 (lebih tinggi dari OversoldLiquidityContinuation -139)
    """
    @staticmethod
    def detect(rsi6: float, long_liq: float, up_energy: float, change_5m: float) -> Dict:
        if (long_liq < 0.5 and rsi6 < 25 and up_energy > 0 and change_5m < 0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme oversold with very close long liq ({long_liq:.2f}%), RSI {rsi6:.1f}, up_energy={up_energy:.2f} → bounce imminent",
                "priority": -141
            }
        return {"override": False}


class ExhaustionDropReversal:
    """
    🔥 Mendeteksi exhaustion drop: harga turun tajam dengan volume rendah,
    tidak ada seller, dan short liq tidak terlalu dekat → reversal LONG.
    Priority -141 (sama dengan ExtremeOversoldCloseLiquidityBounce)
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, down_energy: float,
               short_liq: float, rsi6_5m: float) -> Dict:
        if (change_5m < -3.5 and
            volume_ratio < 0.6 and
            down_energy < 0.01 and
            short_liq > 2.5 and          # Jangan override jika short liq terlalu dekat (bisa squeeze)
            rsi6_5m < 85):               # Hindari overbought ekstrem yang masih bisa lanjut dump
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Exhaustion drop reversal: dropped {change_5m:.1f}% with low volume ({volume_ratio:.2f}x), no sellers, short liq {short_liq:.2f}% → bounce likely",
                "priority": -141
            }
        return {"override": False}


# ================= LECTURER'S SARAN LOGIC: ENERGY AGG CONSENSUS =================

class EnergyAggConsensus:
    """
    🔥 Ketika energy dan agg bertentangan dengan sinyal final,
    batalkan sinyal tersebut.
    
    Kasus EDGEUSDT:
    - Sinyal: SHORT (dari OversoldLiquidityContinuation)
    - up_energy = 2.75, down_energy = 0 → energy bilang LONG
    - agg = 0.60 → 60% trades BUY → bilang LONG
    - Consensus: LONG, bukan SHORT!
    
    Priority: -143 (lebih tinggi dari OversoldLiquidityContinuation -139)
    """
    @staticmethod
    def detect(up_energy: float, down_energy: float, agg: float,
               current_bias: str, rsi6: float, change_5m: float) -> Dict:
        
        # Jika energy dan agg konsisten menunjuk LONG tapi sinyal SHORT
        if (current_bias == "SHORT" and
            up_energy > 1.0 and
            down_energy < 0.1 and
            agg > 0.55 and
            rsi6 < 40):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Energy-Agg consensus overrides SHORT: up_energy={up_energy:.2f}, down_energy={down_energy:.2f}, agg={agg:.2f} all pointing LONG with RSI {rsi6:.1f} oversold",
                "priority": -143
            }
        
        # Mirror: energy + agg konsisten SHORT tapi sinyal LONG
        if (current_bias == "LONG" and
            down_energy > 1.0 and
            up_energy < 0.1 and
            agg < 0.45 and
            rsi6 > 60):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Energy-Agg consensus overrides LONG: down_energy={down_energy:.2f}, up_energy={up_energy:.2f}, agg={agg:.2f} all pointing SHORT with RSI {rsi6:.1f} overbought",
                "priority": -143
            }
        
        return {"override": False}


class ExtremeOverboughtDistribution:
    """
    🔥 DETECTS EXTREME OVERBOUGHT DISTRIBUTION
    RSI5m > 90, volume low, OFI LONG strong, up_energy near zero
    → smart money distributing, dump imminent
    Priority -270 (higher than normal OverboughtDistributionTrap -261)
    """
    @staticmethod
    def detect(rsi6: float, rsi6_5m: float, volume_ratio: float, 
               ofi_bias: str, ofi_strength: float, up_energy: float,
               short_liq: float, change_5m: float) -> Dict:
        # Pastikan bukan short squeeze (short liq harus jauh > 1.5%)
        if short_liq < 1.5:
            return {"override": False}  # Bisa jadi short squeeze, jangan SHORT
        
        if (rsi6_5m > 90 and
            rsi6 > 70 and
            volume_ratio < 0.9 and
            ofi_bias == "LONG" and
            ofi_strength > 0.7 and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme overbought distribution: RSI5m {rsi6_5m:.1f} > 90, volume {volume_ratio:.2f}x, OFI LONG {ofi_strength:.2f}, up_energy={up_energy:.2f} → smart money distributing, dump incoming",
                "priority": -270
            }
        return {"override": False}


class TrappedShortSqueeze:
    """
    🔥 Mendeteksi short squeeze ketika OFI SHORT kuat tapi tidak ada sell wall.
    OFI SHORT dengan down_energy=0, volume rendah, short liq lebih dekat
    → short sellers trapped, harga akan naik untuk menyapu stop loss mereka.
    Priority -160 (lebih tinggi dari OFI dominance -145)
    
    ⚠️ MODIFIED: Tambahkan filter agar tidak override ketika overbought ekstrem (RSI > 75).
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, down_energy: float,
               up_energy: float, volume_ratio: float, short_liq: float,
               long_liq: float, change_5m: float, rsi6: float = None) -> Dict:
        # 🔥 Filter: Jangan paksa LONG jika overbought ekstrem
        if rsi6 is not None and rsi6 > 75:
            return {"override": False}
        
        # Syarat:
        # 1. OFI SHORT kuat (>0.6)
        # 2. Volume rendah (<0.7)
        # 3. Tidak ada sell wall (down_energy < 0.01)
        # 4. Ada buy pressure (up_energy > 0.1)
        # 5. Short liq lebih dekat dari long liq (short_liq < long_liq)
        # 6. Harga sudah naik dalam 5m (change_5m > 1.0) -> konfirmasi uptrend
        if (ofi_bias == "SHORT" and
            ofi_strength > 0.6 and
            volume_ratio < 0.7 and
            down_energy < 0.01 and
            up_energy > 0.1 and
            short_liq < long_liq and
            change_5m > 1.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Trapped short squeeze: OFI SHORT {ofi_strength:.2f} with low volume ({volume_ratio:.2f}x), no sellers (down_energy=0), short liq {short_liq:.2f}% < long liq {long_liq:.2f}% → short sellers trapped, squeeze up",
                "priority": -160
            }
        return {"override": False}


class TrappedLongSqueeze:
    """
    🔥 Mirror: OFI LONG kuat tapi tidak ada buy wall, long liq lebih dekat
    → long sellers trapped, harga akan turun.
    Priority -160.
    """
    @staticmethod
    def detect(ofi_bias: str, ofi_strength: float, up_energy: float,
               down_energy: float, volume_ratio: float, short_liq: float,
               long_liq: float, change_5m: float) -> Dict:
        if (ofi_bias == "LONG" and
            ofi_strength > 0.6 and
            volume_ratio < 0.7 and
            up_energy < 0.01 and
            down_energy > 0.1 and
            long_liq < short_liq and
            change_5m < -1.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Trapped long squeeze: OFI LONG {ofi_strength:.2f} with low volume, no buyers, long liq closer → long sellers trapped, dump down",
                "priority": -160
            }
        return {"override": False}


# ================= NEW: SQUEEZE CONTINUATION DETECTOR =================
class SqueezeContinuationDetector:
    @staticmethod
    def detect(rsi6_5m: float, change_5m: float, volume_ratio: float,
               short_dist: float, up_energy: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, bid_slope: float, ask_slope: float) -> Dict:
        """
        Mendeteksi squeeze continuation yang membatalkan sinyal SHORT palsu.
        Priority lebih tinggi dari Fake Energy Trap (-230) yaitu -265.
        """
        # Kasus 1: RSI 5m overbought, harga naik, volume rendah, tapi OFI SHORT (tekanan jual) -> squeeze
        if (rsi6_5m > 70 and
            change_5m > 2.0 and
            volume_ratio < 0.8 and
            ofi_bias == "SHORT" and
            ofi_strength > 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Squeeze continuation: RSI 5m {rsi6_5m:.1f} overbought, price up {change_5m:.1f}% low vol, but OFI SHORT {ofi_strength:.2f} → selling being absorbed, squeeze ongoing",
                "priority": -265
            }
        # Kasus 2: Ask slope >> bid slope (sell wall besar) namun harga naik -> wall being eaten
        if (ask_slope > bid_slope * 3 and
            change_5m > 3.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Squeeze continuation: large ask wall ({ask_slope:.0f}) but price rising {change_5m:.1f}% → wall absorbed, upside continuation",
                "priority": -265
            }
        # Kasus 3: Short liq masih dekat (<5%) dan up_energy positif -> squeeze berpotensi lanjut
        if (short_dist < 5.0 and
            up_energy > 0.1 and
            change_5m > 2.0 and
            volume_ratio < 0.8):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Squeeze continuation: short liq {short_dist}% still close, price up {change_5m:.1f}% low vol → short squeeze ongoing",
                "priority": -265
            }
        return {"override": False}

# ================= EXISTING DETECTOR MODULES (unchanged) =================
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
    def detect(bid_volume: float, ask_volume: float, up_energy: float, down_energy: float) -> Dict:
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
        if bid_volume < VACUUM_VOLUME_THRESHOLD and ask_volume > 0 and up_energy < down_energy * 3:
            return {
                "override": True,
                "bias": "LONG",
                "reason": "Vacuum: bid kosong, energi up murah → LONG",
                "priority": -245
            }
        if ask_volume < VACUUM_VOLUME_THRESHOLD and bid_volume > 0 and down_energy < up_energy * 3:
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
                "reason": f"Overbought trap: RSI {rsi:.1f} + OI naik {oi_delta:.2f}% tapi energi up murah → pump lanjut",
                "priority": -190
            }
        return {"override": False, "priority": 0}

class LiquidityFlushConfirmation:
    @staticmethod
    def detect(short_dist: float, long_dist: float, agg: float) -> Dict:
        if short_dist < FLUSH_ZONE_THRESHOLD and long_dist < FLUSH_ZONE_THRESHOLD:
            return {
                "wait": True,
                "reason": f"Double sweep zone: short liq +{short_dist}%, long liq -{long_dist}% → tunggu sweep",
                "priority": -255
            }
        if agg < FLUSH_AGG_THRESHOLD and (short_dist < FLUSH_ZONE_THRESHOLD or long_dist < FLUSH_ZONE_THRESHOLD):
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
                "reason": f"Energy Gap Trap: RSI {rsi14:.1f} overbought + down_energy {down_energy:.2f} << up_energy {up_energy:.2f} → HFT akan dump",
                "priority": -215
            }
        if rsi14 < 25 and up_energy < down_energy * 0.1:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Energy Gap Trap: RSI {rsi14:.1f} oversold + up_energy {up_energy:.2f} << down_energy {down_energy:.2f} → HFT akan pump",
                "priority": -215
            }
        return {"override": False, "priority": 0}

class ExtremeOversoldReversalFilter:
    @staticmethod
    def detect(rsi6: float, rsi14: float, stoch_k: float, obv_value: float, obv_trend: str,
               long_dist: float, down_energy: float, ofi_bias: str,
               ofi_strength: float, change_5m: float) -> Dict:
        obv_extreme_negative = obv_value < -30_000_000
        
        if (rsi6 < EXTREME_OVERSOLD_RSI and
            rsi14 < EXTREME_OVERSOLD_RSI and
            stoch_k < EXTREME_OVERSOLD_STOCH and
            obv_trend == "NEGATIVE_EXTREME" and
            obv_extreme_negative and
            change_5m < PANIC_DROP_THRESHOLD and
            down_energy < 0.01):
            if ofi_bias == "LONG" and ofi_strength > 0.3:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Extreme oversold reversal: OBV {obv_value:,.0f} (strong selling exhaustion) + panic drop → bounce imminent",
                    "priority": -225
                }
        return {"override": False, "priority": 0}

class PanicDropExhaustionDetector:
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, rsi6: float,
               down_energy: float, obv_trend: str) -> Dict:
        if (change_5m < -10.0 and
            volume_ratio < 1.0 and
            rsi6 < 15 and
            down_energy < 0.01 and
            obv_trend == "NEGATIVE_EXTREME"):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Panic exhaustion: drop {change_5m:.1f}% + volume drying + RSI {rsi6:.1f} + no sellers → reversal likely",
                "priority": -224
            }
        return {"override": False, "priority": 0}

class ShortSqueezeTrapDetector:
    @staticmethod
    def detect(long_dist: float, rsi6: float, ofi_bias: str,
               ofi_strength: float, down_energy: float,
               agg: float, flow: float) -> Dict:
        if (long_dist < 1.0 and
            rsi6 < 20 and
            ofi_bias == "LONG" and
            ofi_strength > 0.3 and
            down_energy < 0.05 and
            agg < 0.5):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Short squeeze trap: long liq {long_dist}% + oversold RSI {rsi6:.1f} + OFI LONG + no sellers → HFT will pump to trigger liq then bounce",
                "priority": -223
            }
        return {"override": False, "priority": 0}

class OFIExtremeOversoldConfirm:
    @staticmethod
    def detect(rsi6: float, ofi_bias: str, ofi_strength: float,
               long_dist: float, down_energy: float, up_energy: float,
               volume_ratio: float) -> Dict:
        min_ofi_strength = 0.60 if volume_ratio < 0.8 else 0.35
        
        if (rsi6 < 20 and
            ofi_bias == "LONG" and
            ofi_strength > min_ofi_strength and
            down_energy < 0.1):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OFI confirms oversold bounce: RSI {rsi6:.1f} + OFI LONG ({ofi_strength:.2f} > {min_ofi_strength:.2f}) + no sellers → smart money accumulating",
                "priority": -222
            }
        if (rsi6 > 80 and
            ofi_bias == "SHORT" and
            ofi_strength > min_ofi_strength and
            up_energy < 0.1):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OFI confirms overbought dump: RSI {rsi6:.1f} + OFI SHORT ({ofi_strength:.2f} > {min_ofi_strength:.2f}) + no buyers → smart money distributing",
                "priority": -222
            }
        return {"override": False, "priority": 0}

class OversoldContinuation:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, price: float, ma25: float, ma99: float,
               volume_ratio: float, down_energy: float, ofi_bias: str, ofi_strength: float,
               long_dist: float) -> Dict:
        # 🔥 Jangan paksa SHORT jika long liq sangat dekat dan volume rendah (potensi short squeeze / bounce)
        if long_dist < 1.5 and volume_ratio < 0.7:
            return {"override": False}
        if rsi6 < 25 and obv_trend == "NEGATIVE_EXTREME" and price < ma25 and price < ma99 and volume_ratio < 0.8:
            # Block jika OFI LONG kuat dan volume rendah (akan bounce)
            if ofi_bias == "LONG" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Oversold continuation: RSI {rsi6:.1f} deep oversold, price below MAs, low volume → falling knife likely",
                "priority": -223
            }
        return {"override": False, "priority": 0}

class OversoldBounce:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, down_energy: float, long_dist: float,
               price: float, recent_low: float, up_energy: float, ma25: float, ma99: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float) -> Dict:
        if rsi6 < 25 and obv_trend == "NEGATIVE_EXTREME" and down_energy < 0.01:
            # Block jika OFI SHORT kuat dan volume rendah (bounce akan gagal)
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.6:
                return {"override": False}
            if price < ma25 and price < ma99:
                return {"override": False}
            if long_dist < 3.0 or (price - recent_low) / recent_low < 0.02 or up_energy > 0.1:
                return {
                    "override": True,
                    "bias": "LONG",
                    "reason": f"Oversold bounce: RSI {rsi6:.1f} deep oversold, OBV extreme negative, no sellers → potential bounce",
                    "priority": -223
                }
        return {"override": False, "priority": 0}

class StrongBearishOverride:
    @staticmethod
    def detect(rsi6: float, obv_trend: str, price: float, ma25: float, ma99: float,
               volume_ratio: float, down_energy: float) -> Dict:
        if (price < ma25 and price < ma99 and
            obv_trend == "NEGATIVE_EXTREME" and
            volume_ratio < 0.8 and
            rsi6 < 40 and
            down_energy < ENERGY_ZERO_THRESHOLD):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Strong bearish override: price below MAs, OBV extreme, low volume, RSI {rsi6:.1f} < 40 → force SHORT",
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
                "reason": f"OFI conflict: OFI says LONG (strength {ofi_strength:.2f}) but down_energy is cheap and long liq close ({long_dist}%) → override to SHORT",
                "priority": -222
            }
        if ofi_bias == "SHORT" and up_energy < down_energy * 0.2 and short_dist < 1.5:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"OFI conflict: OFI says SHORT (strength {ofi_strength:.2f}) but up_energy is cheap and short liq close ({short_dist}%) → override to LONG",
                "priority": -222
            }
        if ofi_bias == "LONG" and rsi6 < 30 and long_dist < 2.0 and down_energy < 0.1:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"OFI conflict: OFI says LONG in oversold (RSI {rsi6:.1f}) but long liq close and no sellers → likely trap, override to SHORT",
                "priority": -222
            }
        return {"override": False, "priority": 0}

class LiquidityPriorityEnergyCheck:
    @staticmethod
    def detect(short_dist: float, long_dist: float,
               up_energy: float, down_energy: float,
               price_change_5m: float) -> Dict:
        CLOSE_LIQ = 1.5
        if (long_dist < CLOSE_LIQ and
            long_dist < short_dist and
            down_energy < 0.01 and
            price_change_5m < -5.0):
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity priority blocked: long liq dekat ({long_dist}%) tapi down_energy {down_energy:.2f} + harga sudah drop {price_change_5m:.1f}% → tidak ada fuel untuk dump, justru bounce",
                "priority": -221
            }
        if (short_dist < CLOSE_LIQ and
            short_dist < long_dist and
            up_energy < 0.01 and
            price_change_5m > 5.0):
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Liquidity priority blocked: short liq dekat ({short_dist}%) tapi up_energy {up_energy:.2f} + harga sudah pump {price_change_5m:.1f}% → tidak ada fuel untuk pump, justru dump",
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
        # 🔥 Block jika extreme overbought/oversold dengan volume rendah
        if (rsi6_5m > 70 and volume_ratio < 0.6) or (rsi6_5m < 30 and volume_ratio < 0.6):
            return {"override": False}
        
        CLOSE_LIQ_THRESHOLD = 1.5
        if short_dist < CLOSE_LIQ_THRESHOLD and short_dist < long_dist:
            # 🔥 Jika overbought dan OFI SHORT kuat, jangan paksa LONG (potensi dump)
            if rsi6 > 65 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity priority: short liq sangat dekat (+{short_dist}%) → ambil likuidasi short (LONG)",
                "priority": -220
            }
        if long_dist < CLOSE_LIQ_THRESHOLD and long_dist < short_dist:
            # 🔥 Jika oversold dan OFI LONG kuat, jangan paksa SHORT (potensi bounce)
            if rsi6 < 35 and ofi_bias == "LONG" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            # 🔥 Jika oversold ekstrem (rsi6 < 25) dan volume rendah, jangan paksa SHORT
            if rsi6 < 25 and volume_ratio < 0.6:
                return {"override": False}
            # 🔥 Jika long liq sangat dekat dan OFI SHORT kuat, jangan paksa SHORT (potensi squeeze)
            if long_dist < 2.5 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            # 🔥 Jika oversold (rsi6 < 35) dan OFI SHORT kuat, jangan paksa SHORT (potensi reversal)
            if rsi6 < 35 and ofi_bias == "SHORT" and ofi_strength > 0.7 and volume_ratio < 0.7:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Liquidity priority: long liq sangat dekat (-{long_dist}%) → ambil likuidasi long (SHORT)",
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
                "reason": f"Liquidity energy trap: short liq dekat (+{short_dist}%) tapi down_energy {down_energy:.2f} jauh lebih murah dari up_energy {up_energy:.2f} → HFT akan dump dulu",
                "priority": -220
            }
        if long_dist < CLOSE_LIQ and long_dist < short_dist and up_energy < down_energy * 0.2:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity energy trap: long liq dekat (-{long_dist}%) tapi up_energy {up_energy:.2f} jauh lebih murah dari down_energy {down_energy:.2f} → HFT akan pump dulu",
                "priority": -220
            }
        return {"override": False, "priority": 0}

class OverboughtLiquidityTrap:
    @staticmethod
    def detect(short_dist: float, long_dist: float, rsi6: float, up_energy: float, down_energy: float,
               ofi_bias: str, ofi_strength: float, volume_ratio: float, funding_rate: float) -> Dict:
        CLOSE_LIQ = 1.5

        if volume_ratio < 0.6:
            return {"override": False, "priority": 0}
        
        # 🔥 Jangan paksa LONG jika overbought ekstrem dan volume tidak terlalu tinggi (exhaustion)
        if rsi6 > 90 and volume_ratio < 0.9:
            return {"override": False, "priority": 0}
        
        # 🔥 Jika funding rate sangat negatif dan overbought ekstrem, jangan paksa LONG (exhaustion / short trap gagal)
        if rsi6 > 85 and funding_rate < -0.005 and volume_ratio < 1.0:
            return {"override": False}
        
        if (short_dist < CLOSE_LIQ and short_dist < long_dist and
            rsi6 > 70 and down_energy < ENERGY_ZERO_THRESHOLD):
            if ofi_bias == "SHORT" and ofi_strength > 0.6:
                return {"override": False, "priority": 0}

        if short_dist < CLOSE_LIQ and short_dist < long_dist and rsi6 > 70 and down_energy < ENERGY_ZERO_THRESHOLD:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Overbought liquidity trap: short liq sangat dekat (+{short_dist}%) tetapi RSI overbought ({rsi6:.1f}) dan tidak ada tekanan jual (down_energy {down_energy:.2f}) → HFT akan pump dulu untuk ambil short liq",
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
                "reason": f"Liquidity bait: long liq dekat (-{long_dist}%) + volume rendah {volume_ratio:.2f}x → HFT akan reverse",
                "priority": -216
            }
        if short_dist < 1.0 and volume_ratio < 0.7:
            return {
                "override": True,
                "bias": "SHORT" if down_energy < up_energy else "LONG",
                "reason": f"Liquidity bait: short liq dekat (+{short_dist}%) + volume rendah {volume_ratio:.2f}x → HFT akan reverse",
                "priority": -216
            }
        if short_dist < CLOSE_LIQ and long_dist < CLOSE_LIQ:
            return {"override": False, "priority": 0}
        if short_dist < CLOSE_LIQ and down_energy < up_energy * 0.3 and agg < 0.3:
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Liquidity bait: short liq dekat (+{short_dist}%) tetapi down_energy {down_energy:.2f} lebih murah → HFT akan dump dulu",
                "priority": -216
            }
        if long_dist < CLOSE_LIQ and up_energy < down_energy * 0.3 and agg < 0.3:
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Liquidity bait: long liq dekat (-{long_dist}%) tetapi up_energy {up_energy:.2f} lebih murah → HFT akan pump dulu",
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
        
        # 🔥 Kasus: down_energy=0, up_energy > 0 -> seharusnya SHORT (tidak ada buyer)
        if down_energy < ENERGY_ZERO_THRESHOLD and up_energy > MIN_ENERGY_TO_MOVE:
            # Jika OFI LONG kuat, bisa jadi pembelian masih berlangsung, tidak di-block
            if price_change_5m > 1.5 and ofi_bias == "LONG" and ofi_strength > 0.3:
                return {"override": False, "priority": 0}
            # 🔥 BLOCK jika OFI SHORT kuat dan volume rendah (bertentangan dengan sinyal SHORT)
            if ofi_bias == "SHORT" and ofi_strength > 0.6 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            # 🔥 NEW: Jangan paksa SHORT jika oversold (rsi6 < 35) dan volume tidak tinggi, apalagi harga sudah turun
            if rsi6 < 35 and volume_ratio < 0.8 and price_change_5m < 0:
                return {"override": False, "priority": 0}
            # 🔥 NEW: Jangan paksa SHORT jika rsi6_5m rendah (<40) dan volume_ratio < 0.8 (potensi bounce)
            if rsi6_5m < 40 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Extreme energy imbalance: down_energy {down_energy:.2f} << up_energy {up_energy:.2f} → tidak ada buyer support, bearish",
                "priority": -218
            }
        
        # 🔥 Kasus: up_energy=0, down_energy > 0 -> seharusnya LONG (tidak ada seller)
        if up_energy < ENERGY_ZERO_THRESHOLD and down_energy > MIN_ENERGY_TO_MOVE:
            # BLOCK jika OFI LONG kuat dan volume rendah (bertentangan dengan sinyal LONG)
            if ofi_bias == "LONG" and ofi_strength > 0.6 and volume_ratio < 0.8:
                return {"override": False, "priority": 0}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Extreme energy imbalance: up_energy {up_energy:.2f} << down_energy {down_energy:.2f} → tidak ada seller pressure, bullish",
                "priority": -218
            }
        return {"override": False, "priority": 0}

class EnergyTrapFilter:
    @staticmethod
    def detect(up_energy: float, down_energy: float, price_change_5m: float,
               volume_ratio: float, rsi14: float, short_liq: float, rsi6_5m: float) -> Dict:
        if down_energy < ENERGY_ZERO_THRESHOLD and up_energy < 1.0 and price_change_5m > 2.0 and volume_ratio < 1.0 and rsi14 > 60:
            # 🔥 Jangan override jika short liq sangat dekat dan overbought ekstrem (masih squeeze)
            if short_liq < 1.5 and volume_ratio < 0.6 and rsi6_5m > 70:
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Energy Trap: down_energy {down_energy:.2f} seolah habis, tapi harga naik {price_change_5m:.1f}% dengan volume turun {volume_ratio:.2f}x → HFT akan dump",
                "priority": -217
            }
        return {"override": False, "priority": 0}

class ThinOrderBookPump:
    @staticmethod
    def detect(up_energy: float, down_energy: float, price_change_5m: float,
               volume_ratio: float, ofi_bias: str, ofi_strength: float,
               short_liq: float) -> Dict:
        # Modified: only trigger if short liq is relatively close (<5%) to confirm valid squeeze
        if (down_energy < ENERGY_ZERO_THRESHOLD and up_energy > 0 and
            price_change_5m > 1.0 and volume_ratio < 1.0 and
            ofi_bias == "LONG" and ofi_strength > 0 and
            short_liq < 5.0):   # tambahan: hanya jika short liq relatif dekat
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Thin order book pump: down_energy {down_energy:.2f} but price rising and OFI bullish → no sellers to stop the pump, short liq {short_liq}% close",
                "priority": -217
            }
        return {"override": False, "priority": 0}

class PumpExhaustionTrap:
    """
    🔥 NEW: Detects thin pumps that are likely reversal traps.
    Based on lecturer's feedback: pump with low volume, no sellers (down_energy=0),
    but long liq is closer than short liq → HFT will reverse to grab long liquidations.
    Priority -216: between ThinOrderBookPump (-217) and EnergyTrap (-217)
    """
    @staticmethod
    def detect(change_5m: float, volume_ratio: float, down_energy: float,
               long_liq: float, short_liq: float, rsi6: float) -> Dict:
        # Pump dengan volume rendah, no sellers, dan long liq lebih dekat dari short liq
        if (change_5m > 1.0 and
            volume_ratio < 0.7 and
            down_energy < 0.01 and
            long_liq < short_liq and
            rsi6 < 75):  # tidak overbought ekstrim, tapi sudah naik
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Pump exhaustion trap: naik {change_5m:.1f}% dengan volume {volume_ratio:.2f}x, down_energy=0 tapi long liq {long_liq}% < short liq {short_liq}% → reversal untuk ambil long liq",
                "priority": -216   # di antara ThinOrderBookPump (-217) dan EnergyTrap (-217) dll
            }
        return {"override": False}

class HFTTrapDetector:
    @staticmethod
    def detect_fake_energy(down_energy: float, up_energy: float, price_change_5m: float,
                           volume_ratio: float, rsi14: float, short_liq: float, long_liq: float,
                           rsi6_5m: float, rsi6: float) -> Dict:
        # SHORT trap (down_energy=0, price naik)
        if down_energy < ENERGY_ZERO_THRESHOLD and price_change_5m > 3.0 and volume_ratio < 0.7 and rsi14 > 60:
            # 🔥 Jangan override jika short liq sangat dekat dan overbought ekstrem (masih squeeze)
            if short_liq < 1.5 and volume_ratio < 0.6 and (rsi6_5m > 70 or rsi6 > 80):
                return {"override": False}
            return {
                "override": True,
                "bias": "SHORT",
                "reason": f"Fake Energy: down_energy=0 tetapi harga naik {price_change_5m:.1f}% dengan volume turun {volume_ratio:.2f}x → HFT trap, akan dump",
                "priority": -230
            }
        # LONG trap (up_energy=0, price turun)
        if up_energy < ENERGY_ZERO_THRESHOLD and price_change_5m < -3.0 and volume_ratio < 0.7 and rsi14 < 40:
            if long_liq < 1.5 and volume_ratio < 0.6 and (rsi6_5m < 30 or rsi6 < 20):
                return {"override": False}
            return {
                "override": True,
                "bias": "LONG",
                "reason": f"Fake Energy: up_energy=0 tetapi harga turun {price_change_5m:.1f}% dengan volume turun {volume_ratio:.2f}x → HFT trap, akan pump",
                "priority": -230
            }
        return {"override": False, "priority": 0}

class VolumeConfidenceFilter:
    @staticmethod
    def apply(volume_ratio: float, current_confidence: str, current_reason: str) -> Tuple[str, str]:
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
    def check(rsi6_1m: float, rsi6_5m: float, current_confidence: str, current_reason: str) -> Tuple[str, str]:
        if (rsi6_1m > 50 and rsi6_5m < 40) or (rsi6_1m < 50 and rsi6_5m > 60):
            if current_confidence == "ABSOLUTE":
                new_conf = "MEDIUM"
            else:
                new_conf = current_confidence
            reason_suffix = f" | Multi-TF divergence: 1m RSI {rsi6_1m:.1f} vs 5m RSI {rsi6_5m:.1f}"
            return new_conf, current_reason + reason_suffix
        return current_confidence, current_reason

class OBVStochasticReversal:
    @staticmethod
    def apply(obv_trend: str, obv_value: float, stoch_k: float, stoch_d: float, 
              current_bias: str, current_reason: str, volume_ratio: float,
              rsi6: float, rsi6_5m: float) -> Tuple[str, str]:
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

class VolumeTrapDetector:
    @staticmethod
    def detect(volume_ratio: float, change_5m: float, bias: str) -> Dict:
        if volume_ratio < 0.4 and abs(change_5m) > 2.0:
            return {
                "warning": True,
                "reason": f"Low Volume Trap: Volume {volume_ratio:.2f}x but price moved {change_5m:.1f}%. Possible HFT spoof.",
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

class AlgoTypeAnalyzer:
    @staticmethod
    def analyze(order_book: Dict, trades: List[Dict], price: float, short_dist: float, long_dist: float,
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
            impact_bias = "LONG" if buy_ratio > 0.55 else "SHORT" if buy_ratio < 0.45 else "NEUTRAL"
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
            opp_bias = "LONG" if cost_to_up < cost_to_down else "SHORT" if cost_to_down < cost_to_up else "NEUTRAL"

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

# ================= FIXED HFT6PercentDirection =================
class HFT6PercentDirection:
    @staticmethod
    def determine(price: float, short_dist: float, long_dist: float,
                  up_energy: float, down_energy: float, oi_delta: float,
                  agg: float, flow: float) -> Dict:
        # 🔥 PATCH DUSDT: Jika kedua liquidity sangat jauh (>5%),
        # jangan gunakan liquidity sebagai primary driver.
        # Pakai energy dan agg sebagai penentu.
        if short_dist > 5.0 and long_dist > 5.0:
            if up_energy > 0 and down_energy < 0.01 and agg > 0.6:
                return {
                    "bias": "LONG",
                    "reason": f"Both liq too far (short={short_dist:.1f}%, long={long_dist:.1f}%), energy+agg: up_energy={up_energy:.2f}, agg={agg:.2f} → LONG",
                    "confidence": "MEDIUM"
                }
            if down_energy > 0 and up_energy < 0.01 and agg < 0.4:
                return {
                    "bias": "SHORT",
                    "reason": f"Both liq too far (short={short_dist:.1f}%, long={long_dist:.1f}%), energy+agg: down_energy={down_energy:.2f}, agg={agg:.2f} → SHORT",
                    "confidence": "MEDIUM"
                }
            # Kalau tidak ada signal kuat, kembalikan NEUTRAL
            return {"bias": "NEUTRAL", "reason": f"Both liq too far ({short_dist:.1f}%/{long_dist:.1f}%), no strong signal", "confidence": "LOW"}

        # Prioritas Utama: Jarak Likuiditas (Magnet)
        if short_dist < 1.0 and short_dist < long_dist:
            primary = "LONG"
            reason = f"Short liq sangat dekat (+{short_dist}%) → Priority Squeeze"
            # Jangan biarkan energy murah membatalkan squeeze kecuali ada sell wall raksasa
            if down_energy > up_energy * 5: # Hanya batal jika ada tembok jual tebal
                primary = "SHORT"
                reason += " (Blocked by massive sell wall)"
        elif long_dist < 1.0 and long_dist < short_dist:
            primary = "SHORT"
            reason = f"Long liq sangat dekat (-{long_dist}%) → Priority Squeeze"
            if up_energy > down_energy * 5:
                primary = "LONG"
                reason += " (Blocked by massive buy wall)"
        else:
            # Logic lama untuk kondisi normal
            if short_dist < long_dist:
                primary = "LONG"
                reason = f"Short liq lebih dekat (+{short_dist}%)"
            else:
                primary = "SHORT"
                reason = f"Long liq lebih dekat (-{long_dist}%)"
            
            # Energy override hanya jika jarak liq jauh
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
            weight = self.dynamic_weights.get(strategy, self.strategies.get(strategy, 1.0))
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
            change = closes[-i] - closes[-i-1]
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
            if closes[i] > closes[i-1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
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
        small_trades = [t for t in trades if abs(float(t.get('q', t.get('qty', 0)))) < median_size]
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
        else:  # SHORT
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

    def __del__(self):
        if hasattr(self, 'ws') and self.ws is not None:
            self.ws.stop()

    def _is_strong_signal(self, ofi, up_energy, down_energy, change_5m, rsi6) -> bool:
        # OFI harus memiliki bias yang jelas (bukan NEUTRAL)
        if ofi is not None and ofi.get("bias") != "NEUTRAL" and ofi.get("strength", 0) > 0.7:
            return True
        if (up_energy < ENERGY_ZERO_THRESHOLD and down_energy > MIN_ENERGY_TO_MOVE) or \
           (down_energy < ENERGY_ZERO_THRESHOLD and up_energy > MIN_ENERGY_TO_MOVE):
            return True
        if abs(change_5m) > 8.0:
            return True
        # Hapus kondisi rsi6 > 80 or rsi6 < 20 karena terlalu agresif
        # if rsi6 > 80 or rsi6 < 20:
        #     return True
        return False

    def analyze(self) -> Optional[Dict]:
        try:
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

            # ========== MACD DUEL LOGIC ==========
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
            obv_magnitude = "HIGH" if abs(obv_value) > 50_000_000 else "MEDIUM" if abs(obv_value) > 10_000_000 else "LOW"
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

            # Calculate 30s change for velocity decay detection
            if len(closes_1m) >= 2:
                change_30s = ((closes_1m[-1] - closes_1m[-2]) / closes_1m[-2]) * 100 * 0.5
            else:
                change_30s = 0.0

            rsi6_5m = 50.0
            if k5m and len(k5m["closes"]) >= 6:
                rsi6_5m = IndicatorCalculator.calculate_rsi(k5m["closes"], 6)

            up_energy, down_energy = IndicatorCalculator.calculate_energy(order_book if order_book else {})
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
                flow = agg
            else:
                agg, flow = 0.5, 0.5

            oi_delta = 0.0
            if oi_history and len(oi_history) >= 2:
                oi_delta = ((oi_history[0] - oi_history[1]) / oi_history[1]) * 100 if oi_history[1] != 0 else 0

            volatility = (max(highs_1m[-20:]) - min(lows_1m[-20:])) / price if price > 0 else 0

            ofi_raw = OrderFlowImbalance.calculate(trades, window_ms=2000)
            current_time = time.time()
            if ofi_raw["bias"] == self.prev_ofi_bias and (current_time - self.prev_ofi_timestamp) >= self.ofi_consistency_required:
                ofi = ofi_raw
            else:
                if ofi_raw["bias"] != self.prev_ofi_bias:
                    self.prev_ofi_bias = ofi_raw["bias"]
                    self.prev_ofi_timestamp = current_time
                ofi = {"bias": self.prev_ofi_bias, "strength": ofi_raw["strength"]}

            iceberg = IcebergDetector.detect(trades, price) if trades else {"detected": False}
            cross_lead = CrossExchangeLeader.check_leader(self.symbol)
            funding_trap = FundingRateTrap.detect(funding_rate or 0, oi or 0)
            liq_heat = LiquidationHeatMap.fetch_real_liq(self.symbol)

            # ========== NEW: OrderBook Slope ==========
            bid_slope, ask_slope = OrderBookSlope.calculate(order_book)
            slope_signal = OrderBookSlope.signal(bid_slope, ask_slope)

            # ========== NEW: Latency Arbitrage Predictor ==========
            predicted_price = LatencyArbitragePredictor.predict_next_price(
                price, change_5m, up_energy, down_energy, LATENCY_MS_ESTIMATE
            )

            # ========== NEW: Overbought / Oversold Distribution Traps ==========
            overbought_trap = OverboughtDistributionTrap.detect(
                rsi6, liq["short_dist"], liq["long_dist"], volume_ratio,
                down_energy, up_energy, ofi["bias"], ofi["strength"], change_5m
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
                        ofi["bias"], ofi["strength"]
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
                        # ========== NEW: Probabilistic Engine ==========
                        prob_engine = ProbabilisticEngine()
                        algo_type = {"bias": "NEUTRAL", "confidence": "MEDIUM"}
                        hft_6pct = {"bias": "NEUTRAL", "reason": ""}

                        # Initialize all variables to prevent UnboundLocalError
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

                        # ========== HIGH PRIORITY OVERRIDES ==========
                        # 1. MASTER SQUEEZE RULE (Priority -1100)
                        master_squeeze = MasterSqueezeRule.detect(
                            liq["short_dist"], liq["long_dist"], change_5m,
                            down_energy, up_energy, volume_ratio
                        )
                        if master_squeeze["override"]:
                            final_bias = master_squeeze["bias"]
                            final_reason = master_squeeze["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "MASTER_SQUEEZE_RULE"
                            priority = master_squeeze["priority"]
                            prob_engine.add(master_squeeze["bias"], 10.0)
                        else:
                            # 1.01. ABSOLUTE AGG OVERRIDE (Priority -1098)
                            absolute_agg = AbsoluteAggOverride.detect(
                                agg, down_energy, ofi["bias"], ofi["strength"],
                                up_energy, funding_rate or 0
                            )
                            if absolute_agg["override"]:
                                final_bias = absolute_agg["bias"]
                                final_reason = absolute_agg["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "ABSOLUTE_AGG_OVERRIDE"
                                priority = absolute_agg["priority"]
                                prob_engine.add(absolute_agg["bias"], 9.995)
                            else:
                                # 1.025. VOLUME SPIKE BOUNCE DETECTOR (Priority -1092)
                            volume_spike_bounce = VolumeSpikeBounceDetector.detect(
                                latest_volume, volume_ma10, rsi6,
                                up_energy, down_energy, agg,
                                change_5m, liq["long_dist"], liq["short_dist"]
                            )
                            if volume_spike_bounce["override"]:
                                final_bias = volume_spike_bounce["bias"]
                                final_reason = volume_spike_bounce["reason"]
                                final_confidence = "ABSOLUTE"
                                final_phase = "VOLUME_SPIKE_BOUNCE"
                                priority = volume_spike_bounce["priority"]
                                prob_engine.add(volume_spike_bounce["bias"], 9.99)
                            else:
                                # 1.03. TWO-PHASE HFT SWEEP DETECTOR (Priority -1095)
                                two_phase_hft = TwoPhaseHFTSweepDetector.detect(
                                    liq["short_dist"], liq["long_dist"], rsi6,
                                    down_energy, up_energy, change_5m,
                                    ofi["bias"], ofi["strength"], volume_ratio
                                )
                                if two_phase_hft["override"]:
                                    final_bias = two_phase_hft["bias"]
                                    final_reason = two_phase_hft["reason"]
                                    final_confidence = "ABSOLUTE"
                                    final_phase = "TWO_PHASE_HFT_SWEEP"
                                    priority = two_phase_hft["priority"]
                                    prob_engine.add(two_phase_hft["bias"], 9.98)
                                else:
                                    # 1.05. HFT EXPLICIT DUMP OVERRIDE (Priority -1090)
                                    hft_dump = HFTExplicitDumpOverride.detect(
                                        hft_6pct["bias"], hft_6pct["reason"],
                                        agg, ofi["bias"], change_5m,
                                        funding_rate or 0, volume_ratio
                                    )
                                    if hft_dump["override"]:
                                        final_bias = hft_dump["bias"]
                                        final_reason = hft_dump["reason"]
                                        final_confidence = "ABSOLUTE"
                                        final_phase = "HFT_EXPLICIT_DUMP"
                                        priority = hft_dump["priority"]
                                        prob_engine.add(hft_dump["bias"], 9.95)
                                    else:
                                        # 1.06. EXTREME FUNDING RATE TRAP (Priority -1085)
                                        extreme_funding = ExtremeFundingRateTrap.detect(
                                            funding_rate or 0, agg, hft_6pct["bias"],
                                            ofi["bias"], ofi["strength"],
                                            change_5m, volume_ratio
                                        )
                                        if extreme_funding["override"]:
                                            final_bias = extreme_funding["bias"]
                                            final_reason = extreme_funding["reason"]
                                            final_confidence = "ABSOLUTE"
                                            final_phase = "EXTREME_FUNDING_TRAP"
                                            priority = extreme_funding["priority"]
                                            prob_engine.add(extreme_funding["bias"], 9.9)
                                        else:
                                            # 1.07. EXTREME FUNDING LIQUIDITY OVERRIDE (Priority -1086)
                                            extreme_funding_liq = ExtremeFundingLiquidityOverride.detect(
                                                funding_rate or 0, liq["long_dist"], liq["short_dist"],
                                                hft_6pct["bias"], agg, change_5m
                                            )
                                            if extreme_funding_liq["override"]:
                                                final_bias = extreme_funding_liq["bias"]
                                                final_reason = extreme_funding_liq["reason"]
                                                final_confidence = "ABSOLUTE"
                                                final_phase = "EXTREME_FUNDING_LIQUIDITY"
                                                priority = extreme_funding_liq["priority"]
                                                prob_engine.add(extreme_funding_liq["bias"], 9.95)
                                            else:
                                                # 1.1. EXTREME OVERSOLD IGNORE LIQUIDITY (Priority -1080)
                                                extreme_oversold_ignore = ExtremeOversoldIgnoreLiquidity.detect(rsi6, volume_ratio)
                                                if extreme_oversold_ignore["override"]:
                                                    final_bias = extreme_oversold_ignore["bias"]
                                                    final_reason = extreme_oversold_ignore["reason"]
                                                    final_confidence = "ABSOLUTE"
                                                    final_phase = "EXTREME_OVERSOLD_IGNORE_LIQUIDITY"
                                                    priority = extreme_oversold_ignore["priority"]
                                                    prob_engine.add(extreme_oversold_ignore["bias"], 9.9)
                                                else:
                                                    # 1.2. EXTREME OVERBOUGHT IGNORE LIQUIDITY (Priority -1080)
                                                    extreme_overbought_ignore = ExtremeOverboughtIgnoreLiquidity.detect(rsi6, volume_ratio)
                                                    if extreme_overbought_ignore["override"]:
                                                        final_bias = extreme_overbought_ignore["bias"]
                                                        final_reason = extreme_overbought_ignore["reason"]
                                                        final_confidence = "ABSOLUTE"
                                                        final_phase = "EXTREME_OVERBOUGHT_IGNORE_LIQUIDITY"
                                                        priority = extreme_overbought_ignore["priority"]
                                                        prob_engine.add(extreme_overbought_ignore["bias"], 9.9)
                                                    else:
                                                        # 1.3. CROWDED LONG DISTRIBUTION (Priority -165)
                                                        crowded_long = CrowdedLongDistribution.detect(rsi6, volume_ratio, ofi["bias"], change_5m)
                                                        if crowded_long["override"]:
                                                            final_bias = crowded_long["bias"]
                                                            final_reason = crowded_long["reason"]
                                                            final_confidence = "ABSOLUTE"
                                                            final_phase = "CROWDED_LONG_DISTRIBUTION"
                                                            priority = crowded_long["priority"]
                                                            prob_engine.add(crowded_long["bias"], 4.5)
                                                        else:
                                                            # 1.4. CROWDED SHORT ACCUMULATION (Priority -165)
                                                            crowded_short = CrowdedShortAccumulation.detect(rsi6, volume_ratio, ofi["bias"], change_5m)
                                                            if crowded_short["override"]:
                                                                final_bias = crowded_short["bias"]
                                                                final_reason = crowded_short["reason"]
                                                                final_confidence = "ABSOLUTE"
                                                                final_phase = "CROWDED_SHORT_ACCUMULATION"
                                                                priority = crowded_short["priority"]
                                                                prob_engine.add(crowded_short["bias"], 4.5)
                                                            else:
                                                                # 1.5. HFT-ALGO CONSENSUS (Priority -170)
                                                                hft_algo_consensus = HFTAlgoConsensusOverride.detect(
                                                                    algo_type["bias"], hft_6pct["bias"], volume_ratio, change_5m
                                                                )
                                                                if hft_algo_consensus["override"]:
                                                                    final_bias = hft_algo_consensus["bias"]
                                                                    final_reason = hft_algo_consensus["reason"]
                                                                    final_confidence = "ABSOLUTE"
                                                                    final_phase = "HFT_ALGO_CONSENSUS"
                                                                    priority = hft_algo_consensus["priority"]
                                                                    prob_engine.add(hft_algo_consensus["bias"], 9.0)
                                                                else:
                                                                    # 1.55. AGG/FLOW DIVERGENCE FILTER (Priority -175)
                                                                    agg_divergence = AggFlowDivergenceFilter.detect(
                                                                        agg, change_5m, ofi["bias"], hft_6pct["bias"], volume_ratio
                                                                    )
                                                                    if agg_divergence["override"]:
                                                                        final_bias = agg_divergence["bias"]
                                                                        final_reason = agg_divergence["reason"]
                                                                        final_confidence = "ABSOLUTE"
                                                                        final_phase = "AGG_FLOW_DIVERGENCE"
                                                                        priority = agg_divergence["priority"]
                                                                        prob_engine.add(agg_divergence["bias"], 9.0)
                                                                    else:
                                                                        # 1.6. EXHAUSTED LIQUIDITY REVERSAL (Priority -1060)
                                                                        exhausted_liquidity = ExhaustedLiquidityReversal.detect(
                                                                            liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                                                            ofi["bias"], ofi["strength"]
                                                                        )
                                                                        if exhausted_liquidity["override"]:
                                                                            final_bias = exhausted_liquidity["bias"]
                                                                            final_reason = exhausted_liquidity["reason"]
                                                                            final_confidence = "ABSOLUTE"
                                                                            final_phase = "EXHAUSTED_LIQUIDITY_REVERSAL"
                                                                            priority = exhausted_liquidity["priority"]
                                                                            prob_engine.add(exhausted_liquidity["bias"], 9.6)
                                                                        else:
                                                                            # 1.65. AGG CONFIRMED BOUNCE (Priority -1070)
                                                                            agg_confirmed = AggConfirmedBounce.detect(
                                                                                agg, rsi6, liq["long_dist"], liq["short_dist"],
                                                                                up_energy, down_energy, change_5m
                                                                            )
                                                                            if agg_confirmed["override"]:
                                                                                final_bias = agg_confirmed["bias"]
                                                                                final_reason = agg_confirmed["reason"]
                                                                                final_confidence = "ABSOLUTE"
                                                                                final_phase = "AGG_CONFIRMED_BOUNCE"
                                                                                priority = agg_confirmed["priority"]
                                                                                prob_engine.add(agg_confirmed["bias"], 9.75)
                                                                            else:
                                                                                # 1.655. RSI 5m VS 1m DIVERGENCE (Priority -1068)
                                                                                rsi_divergence = RSI5mVs1mDivergence.detect(
                                                                                    rsi6, rsi6_5m, down_energy, up_energy,
                                                                                    agg, funding_rate or 0
                                                                                )
                                                                                if rsi_divergence["override"]:
                                                                                    final_bias = rsi_divergence["bias"]
                                                                                    final_reason = rsi_divergence["reason"]
                                                                                    final_confidence = "ABSOLUTE"
                                                                                    final_phase = "RSI_5m_VS_1m_DIVERGENCE"
                                                                                    priority = rsi_divergence["priority"]
                                                                                    prob_engine.add(rsi_divergence["bias"], 9.73)
                                                                                else:
                                                                                    # 1.66. AGG MAJORITY BOUNCE (Priority -1072)
                                                                                agg_majority = AggMajorityBounce.detect(
                                                                                    agg, rsi6, liq["long_dist"], change_5m,
                                                                                    up_energy, down_energy
                                                                                )
                                                                                if agg_majority["override"]:
                                                                                    final_bias = agg_majority["bias"]
                                                                                    final_reason = agg_majority["reason"]
                                                                                    final_confidence = "ABSOLUTE"
                                                                                    final_phase = "AGG_MAJORITY_BOUNCE"
                                                                                    priority = agg_majority["priority"]
                                                                                    prob_engine.add(agg_majority["bias"], 9.7)
                                                                                else:
                                                                                    # 1.7. FUNDING RATE CROWDED SHORT OVERRIDE (Priority -1076)
                                                                                    funding_crowded = FundingRateCrowdedShortOverride.detect(
                                                                                        funding_rate, rsi6, ofi["bias"], ofi["strength"],
                                                                                        liq["long_dist"], liq["short_dist"], volume_ratio
                                                                                    )
                                                                                    if funding_crowded["override"]:
                                                                                        final_bias = funding_crowded["bias"]
                                                                                        final_reason = funding_crowded["reason"]
                                                                                        final_confidence = "ABSOLUTE"
                                                                                        final_phase = "FUNDING_CROWDED_SHORT"
                                                                                        priority = funding_crowded["priority"]
                                                                                        prob_engine.add(funding_crowded["bias"], 9.8)
                                                                                    else:
                                                                                        # 1.55. SHORT SQUEEZE TRAP OVERRIDE (Priority -1060)
                                                                                        squeeze_trap = ShortSqueezeTrapOverride.detect(
                                                                                            liq["short_dist"], liq["long_dist"], up_energy, down_energy,
                                                                                            volume_ratio, rsi6_5m, ofi["bias"], ofi["strength"], change_5m
                                                                                        )
                                                                                        if squeeze_trap["override"]:
                                                                                            final_bias = squeeze_trap["bias"]
                                                                                            final_reason = squeeze_trap["reason"]
                                                                                            final_confidence = "ABSOLUTE"
                                                                                            final_phase = "SHORT_SQUEEZE_TRAP_OVERRIDE"
                                                                                            priority = squeeze_trap["priority"]
                                                                                            prob_engine.add(squeeze_trap["bias"], 9.6)
                                                                                        else:
                                                                                            # 1.6. NEAR EXHAUSTED LIQUIDITY REVERSAL (Priority -1055)
                                                                                            near_exhausted = NearExhaustedLiquidityReversal.detect(
                                                                                                liq["short_dist"], liq["long_dist"], rsi6, volume_ratio, rsi6_5m,
                                                                                                ofi["bias"], ofi["strength"]
                                                                                            )
                                                                                            if near_exhausted["override"]:
                                                                                                final_bias = near_exhausted["bias"]
                                                                                                final_reason = near_exhausted["reason"]
                                                                                                final_confidence = "ABSOLUTE"
                                                                                                final_phase = "NEAR_EXHAUSTED_LIQUIDITY_REVERSAL"
                                                                                                priority = near_exhausted["priority"]
                                                                                                prob_engine.add(near_exhausted["bias"], 9.7)
                                                                                            else:
                                                                                                # 1.7. STRICT LIQUIDITY PROXIMITY (Priority -1050)
                                                                                                strict_liq = LiquidityProximityStrict.detect(
                                                                                                    liq["short_dist"], liq["long_dist"], volume_ratio, rsi6_5m,
                                                                                                    ofi["bias"], ofi["strength"], rsi6, obv_trend, change_5m
                                                                                                )
                                                                                                if strict_liq["override"]:
                                                                                                    final_bias = strict_liq["bias"]
                                                                                                    final_reason = strict_liq["reason"]
                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                    final_phase = "STRICT_LIQUIDITY"
                                                                                                    priority = strict_liq["priority"]
                                                                                                    prob_engine.add(strict_liq["bias"], 9.5)
                                                                                                else:
                                                                                                    # 1.7. LIQUIDITY MAGNET OVERRIDE (Priority -1075)
                                                                                                    liq_magnet_override = LiquidityMagnetOverride.detect(
                                                                                                        liq["short_dist"], liq["long_dist"], volume_ratio,
                                                                                                        rsi6_5m, change_5m
                                                                                                    )
                                                                                                    if liq_magnet_override["override"]:
                                                                                                        final_bias = liq_magnet_override["bias"]
                                                                                                        final_reason = liq_magnet_override["reason"]
                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                        final_phase = "LIQUIDITY_MAGNET_OVERRIDE"
                                                                                                        priority = liq_magnet_override["priority"]
                                                                                                        prob_engine.add(liq_magnet_override["bias"], 9.8)
                                                                                                    else:
                                                                                                        # 2. LIQUIDITY MAGNET CONTINUATION (Priority -1000)
                                                                                                        liq_magnet = LiquidityMagnetContinuation.detect(
                                                                                                            liq["short_dist"], liq["long_dist"], change_5m,
                                                                                                            up_energy, down_energy, volume_ratio
                                                                                                        )
                                                                                                        if liq_magnet["override"]:
                                                                                                            final_bias = liq_magnet["bias"]
                                                                                                            final_reason = liq_magnet["reason"]
                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                            final_phase = "LIQUIDITY_MAGNET_CONTINUATION"
                                                                                                            priority = liq_magnet["priority"]
                                                                                                            prob_engine.add(liq_magnet["bias"], 9.0)
                                                                                                        else:
                                                                                                            # 3. OFI ABSORPTION SQUEEZE (Priority -950)
                                                                                                            ofi_absorption = OFIAbsorptionSqueeze.detect(
                                                                                                                ofi["bias"], ofi["strength"], change_5m,
                                                                                                                liq["short_dist"], liq["long_dist"]
                                                                                                            )
                                                                                                            if ofi_absorption["override"]:
                                                                                                                final_bias = ofi_absorption["bias"]
                                                                                                                final_reason = ofi_absorption["reason"]
                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                final_phase = "OFI_ABSORPTION_SQUEEZE"
                                                                                                                priority = ofi_absorption["priority"]
                                                                                                                prob_engine.add(ofi_absorption["bias"], 8.5)
                                                                                                            else:
                                                                                                                # 4. VELOCITY DECAY REVERSAL (Priority -900)
                                                                                                                velocity_decay = VelocityDecayReversal.detect(
                                                                                                                    change_5m, change_30s,
                                                                                                                    liq["short_dist"], liq["long_dist"]
                                                                                                                )
                                                                                                                if velocity_decay["override"]:
                                                                                                                    final_bias = velocity_decay["bias"]
                                                                                                                    final_reason = velocity_decay["reason"]
                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                    final_phase = "VELOCITY_DECAY_REVERSAL"
                                                                                                                    priority = velocity_decay["priority"]
                                                                                                                    prob_engine.add(velocity_decay["bias"], 8.0)
                                                                                                                else:
                                                                                                                    # 4.5. RSI-ENERGY DIVERGENCE (Priority -856)
                                                                                                                    rsi_energy_div = RSIEnergyDivergence.detect(
                                                                                                                        rsi6, up_energy, down_energy,
                                                                                                                        liq["short_dist"], liq["long_dist"]
                                                                                                                    )
                                                                                                                    if rsi_energy_div["override"]:
                                                                                                                        final_bias = rsi_energy_div["bias"]
                                                                                                                        final_reason = rsi_energy_div["reason"]
                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                        final_phase = "RSI_ENERGY_DIVERGENCE"
                                                                                                                        priority = rsi_energy_div["priority"]
                                                                                                                        prob_engine.add(rsi_energy_div["bias"], 7.6)
                                                                                                                    else:
                                                                                                                        # 4.6. OFI DIVERGENCE TRAP (Priority -855)
                                                                                                                        ofi_div_trap = OFIDivergenceTrap.detect(
                                                                                                                            ofi["bias"], ofi["strength"], down_energy,
                                                                                                                            change_5m, liq["short_dist"], volume_ratio
                                                                                                                        )
                                                                                                                        if ofi_div_trap["override"]:
                                                                                                                            final_bias = ofi_div_trap["bias"]
                                                                                                                            final_reason = ofi_div_trap["reason"]
                                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                                            final_phase = "OFI_DIVERGENCE_TRAP"
                                                                                                                            priority = ofi_div_trap["priority"]
                                                                                                                            prob_engine.add(ofi_div_trap["bias"], 7.55)
                                                                                                                        else:
                                                                                                                            # 5. EMPTY BOOK MOMENTUM (Priority -850)
                                                                                                                            empty_book_mom = EmptyBookMomentum.detect(
                                                                                                                                down_energy, up_energy, change_5m,
                                                                                                                                liq["short_dist"], liq["long_dist"]
                                                                                                                            )
                                                                                                                            if empty_book_mom["override"]:
                                                                                                                                final_bias = empty_book_mom["bias"]
                                                                                                                                final_reason = empty_book_mom["reason"]
                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                final_phase = "EMPTY_BOOK_MOMENTUM"
                                                                                                                                priority = empty_book_mom["priority"]
                                                                                                                                prob_engine.add(empty_book_mom["bias"], 7.5)
                                                                                                                            else:
                                                                                                                                # 6. Squeeze Continuation Detector (Priority -265)
                                                                                                                                squeeze_cont = SqueezeContinuationDetector.detect(
                                                                                                                                    rsi6_5m, change_5m, volume_ratio,
                                                                                                                                    liq["short_dist"], up_energy, down_energy,
                                                                                                                                    ofi["bias"], ofi["strength"], bid_slope, ask_slope
                                                                                                                                )
                                                                                                                                if squeeze_cont["override"]:
                                                                                                                                    final_bias = squeeze_cont["bias"]
                                                                                                                                    final_reason = squeeze_cont["reason"]
                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                    final_phase = "SQUEEZE_CONTINUATION"
                                                                                                                                    priority = squeeze_cont["priority"]
                                                                                                                                    prob_engine.add(squeeze_cont["bias"], 5.0)
                                                                                                                                else:
                                                                                                                                    # 6.5. FLUSH EXHAUSTION REVERSAL (Priority -250)
                                                                                                                                    flush_exhaust = FlushExhaustionReversal.detect(
                                                                                                                                        change_5m, rsi6, volume_ratio,
                                                                                                                                        down_energy, liq["long_dist"]
                                                                                                                                    )
                                                                                                                                    if flush_exhaust["override"]:
                                                                                                                                        final_bias = flush_exhaust["bias"]
                                                                                                                                        final_reason = flush_exhaust["reason"]
                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                        final_phase = "FLUSH_EXHAUSTION"
                                                                                                                                        priority = flush_exhaust["priority"]
                                                                                                                                        prob_engine.add(flush_exhaust["bias"], 4.0)
                                                                                                                                    else:
                                                                                                                                        # 7. Cascade Dump Detector
                                                                                                                                        cascade = CascadeDumpDetector.detect(
                                                                                                                                            change_5m, liq["short_dist"], down_energy, volume_ratio
                                                                                                                                        )
                                                                                                                                        if cascade["override"]:
                                                                                                                                            final_bias = cascade["bias"]
                                                                                                                                            final_reason = cascade["reason"]
                                                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                                                            final_phase = "CASCADE_DUMP"
                                                                                                                                            priority = cascade["priority"]
                                                                                                                                            prob_engine.add(cascade["bias"], 5.0)
                                                                                                                                        else:
                                                                                                                                            # 8. Low Volume Continuation
                                                                                                                                            low_vol_cont = LowVolumeContinuation.detect(
                                                                                                                                                volume_ratio, obv_trend, price, ma25, ma99, down_energy
                                                                                                                                            )
                                                                                                                                            if low_vol_cont["override"]:
                                                                                                                                                final_bias = low_vol_cont["bias"]
                                                                                                                                                final_reason = low_vol_cont["reason"]
                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                final_phase = "LOW_VOL_CONT"
                                                                                                                                                priority = low_vol_cont["priority"]
                                                                                                                                                prob_engine.add(low_vol_cont["bias"], 4.0)
                                                                                                                                            else:
                                                                                                                                                # 9. Fake Bounce Trap
                                                                                                                                                fake_bounce = FakeBounceTrap.detect(
                                                                                                                                                    rsi6, change_5m, volume_ratio,
                                                                                                                                                    liq["short_dist"], liq["long_dist"],
                                                                                                                                                    up_energy, down_energy,
                                                                                                                                                    ofi["bias"], ofi["strength"]
                                                                                                                                                )
                                                                                                                                                if fake_bounce["override"]:
                                                                                                                                                    final_bias = fake_bounce["bias"]
                                                                                                                                                    final_reason = fake_bounce["reason"]
                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                    final_phase = "FAKE_BOUNCE"
                                                                                                                                                    priority = fake_bounce["priority"]
                                                                                                                                                    prob_engine.add(fake_bounce["bias"], 4.0)
                                                                                                                                                else:
                                                                                                                                                    # 10. PostDropBounceOverride (Priority -140)
                                                                                                                                                    post_drop_bounce = PostDropBounceOverride.detect(
                                                                                                                                                        change_5m, volume_ratio, ofi["bias"], ofi["strength"], liq["short_dist"]
                                                                                                                                                    )
                                                                                                                                                    if post_drop_bounce["override"]:
                                                                                                                                                        final_bias = post_drop_bounce["bias"]
                                                                                                                                                        final_reason = post_drop_bounce["reason"]
                                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                                        final_phase = "POST_DROP_BOUNCE"
                                                                                                                                                        priority = post_drop_bounce["priority"]
                                                                                                                                                        prob_engine.add(post_drop_bounce["bias"], 3.5)
                                                                                                                                                    else:
                                                                                                                                                        # Continue with other overrides
                                                                                                                                                        flush = LiquidityFlushConfirmation.detect(liq["short_dist"], liq["long_dist"], agg)
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
                                                                                                                                                        else:
                                                                                                                                                            extreme_oversold = ExtremeOversoldReversalFilter.detect(
                                                                                                                                                                rsi6, rsi14, stoch_k, obv_value, obv_trend,
                                                                                                                                                                liq["long_dist"], down_energy,
                                                                                                                                                                ofi["bias"], ofi["strength"], change_5m
                                                                                                                                                            )
                                                                                                                                                            if extreme_oversold["override"]:
                                                                                                                                                                final_bias = extreme_oversold["bias"]
                                                                                                                                                                final_reason = extreme_oversold["reason"]
                                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                                final_phase = "EXTREME_OVERSOLD_REVERSAL"
                                                                                                                                                                priority = extreme_oversold["priority"]
                                                                                                                                                                prob_engine.add(extreme_oversold["bias"], 3.0)
                                                                                                                                                            else:
                                                                                                                                                                panic_exhaustion = PanicDropExhaustionDetector.detect(
                                                                                                                                                                    change_5m, volume_ratio, rsi6, down_energy, obv_trend
                                                                                                                                                                )
                                                                                                                                                                if panic_exhaustion["override"]:
                                                                                                                                                                    final_bias = panic_exhaustion["bias"]
                                                                                                                                                                    final_reason = panic_exhaustion["reason"]
                                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                                    final_phase = "PANIC_EXHAUSTION"
                                                                                                                                                                    priority = panic_exhaustion["priority"]
                                                                                                                                                                    prob_engine.add(panic_exhaustion["bias"], 3.0)
                                                                                                                                                                else:
                                                                                                                                                                    short_squeeze = ShortSqueezeTrapDetector.detect(
                                                                                                                                                                        liq["long_dist"], rsi6, ofi["bias"], ofi["strength"], down_energy, agg, flow
                                                                                                                                                                    )
                                                                                                                                                                    if short_squeeze["override"]:
                                                                                                                                                                        final_bias = short_squeeze["bias"]
                                                                                                                                                                        final_reason = short_squeeze["reason"]
                                                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                                                        final_phase = "SHORT_SQUEEZE_TRAP"
                                                                                                                                                                        priority = short_squeeze["priority"]
                                                                                                                                                                        prob_engine.add(short_squeeze["bias"], 3.0)
                                                                                                                                                                    else:
                                                                                                                                                                        fake_energy = HFTTrapDetector.detect_fake_energy(
                                                                                                                                                                            down_energy, up_energy, change_5m, volume_ratio, rsi14,
                                                                                                                                                                            liq["short_dist"], liq["long_dist"], rsi6_5m, rsi6
                                                                                                                                                                        )
                                                                                                                                                                        if fake_energy["override"]:
                                                                                                                                                                            final_bias = fake_energy["bias"]
                                                                                                                                                                            final_reason = fake_energy["reason"]
                                                                                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                                                                                            final_phase = "FAKE_ENERGY_TRAP"
                                                                                                                                                                            priority = fake_energy["priority"]
                                                                                                                                                                            prob_engine.add(fake_energy["bias"], 4.0)
                                                                                                                                                                        else:
                                                                                                                                                                            oversold_cont = OversoldContinuation.detect(
                                                                                                                                                                                rsi6, obv_trend, price, ma25, ma99, volume_ratio,
                                                                                                                                                                                down_energy, ofi["bias"], ofi["strength"], liq["long_dist"]
                                                                                                                                                                            )
                                                                                                                                                                            if oversold_cont["override"]:
                                                                                                                                                                                final_bias = oversold_cont["bias"]
                                                                                                                                                                                final_reason = oversold_cont["reason"]
                                                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                                                final_phase = "OVERSOLD_CONT"
                                                                                                                                                                                priority = oversold_cont["priority"]
                                                                                                                                                                                prob_engine.add(oversold_cont["bias"], 3.0)
                                                                                                                                                                            else:
                                                                                                                                                                                oversold_bounce = OversoldBounce.detect(
                                                                                                                                                                                    rsi6, obv_trend, down_energy, liq["long_dist"],
                                                                                                                                                                                    price, liq["recent_low"], up_energy, ma25, ma99,
                                                                                                                                                                                    ofi["bias"], ofi["strength"], volume_ratio
                                                                                                                                                                                )
                                                                                                                                                                                if oversold_bounce["override"]:
                                                                                                                                                                                    final_bias = oversold_bounce["bias"]
                                                                                                                                                                                    final_reason = oversold_bounce["reason"]
                                                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                                                    final_phase = "OVERSOLD_BOUNCE"
                                                                                                                                                                                    priority = oversold_bounce["priority"]
                                                                                                                                                                                    prob_engine.add(oversold_bounce["bias"], 3.0)
                                                                                                                                                                                else:
                                                                                                                                                                                    ofi_extreme = OFIExtremeOversoldConfirm.detect(
                                                                                                                                                                                        rsi6, ofi["bias"], ofi["strength"],
                                                                                                                                                                                        liq["long_dist"], down_energy, up_energy,
                                                                                                                                                                                        volume_ratio
                                                                                                                                                                                    )
                                                                                                                                                                                    if ofi_extreme["override"]:
                                                                                                                                                                                        final_bias = ofi_extreme["bias"]
                                                                                                                                                                                        final_reason = ofi_extreme["reason"]
                                                                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                                                                        final_phase = "OFI_EXTREME_CONFIRM"
                                                                                                                                                                                        priority = ofi_extreme["priority"]
                                                                                                                                                                                        prob_engine.add(ofi_extreme["bias"], 3.0)
                                                                                                                                                                                    else:
                                                                                                                                                                                        strong_bearish = StrongBearishOverride.detect(
                                                                                                                                                                                            rsi6, obv_trend, price, ma25, ma99, volume_ratio, down_energy
                                                                                                                                                                                        )
                                                                                                                                                                                        if strong_bearish["override"]:
                                                                                                                                                                                            final_bias = strong_bearish["bias"]
                                                                                                                                                                                            final_reason = strong_bearish["reason"]
                                                                                                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                                                                                                            final_phase = "STRONG_BEARISH"
                                                                                                                                                                                            priority = strong_bearish["priority"]
                                                                                                                                                                                            prob_engine.add(strong_bearish["bias"], 3.0)
                                                                                                                                                                                        else:
                                                                                                                                                                                            ofi_conflict = OFIConflictFilter.detect(
                                                                                                                                                                                                ofi["bias"], ofi["strength"],
                                                                                                                                                                                                liq["short_dist"], liq["long_dist"],
                                                                                                                                                                                                up_energy, down_energy, rsi6, change_5m
                                                                                                                                                                                            )
                                                                                                                                                                                            if ofi_conflict["override"]:
                                                                                                                                                                                                final_bias = ofi_conflict["bias"]
                                                                                                                                                                                                final_reason = ofi_conflict["reason"]
                                                                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                                                                final_phase = "OFI_CONFLICT"
                                                                                                                                                                                                priority = ofi_conflict["priority"]
                                                                                                                                                                                                prob_engine.add(ofi_conflict["bias"], 3.0)
                                                                                                                                                                                            else:
                                                                                                                                                                                                liq_priority_energy = LiquidityPriorityEnergyCheck.detect(
                                                                                                                                                                                                    liq["short_dist"], liq["long_dist"],
                                                                                                                                                                                                    up_energy, down_energy, change_5m
                                                                                                                                                                                                )
                                                                                                                                                                                                if liq_priority_energy["override"]:
                                                                                                                                                                                                    final_bias = liq_priority_energy["bias"]
                                                                                                                                                                                                    final_reason = liq_priority_energy["reason"]
                                                                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                                                                    final_phase = "LIQUIDITY_PRIORITY_ENERGY_CHECK"
                                                                                                                                                                                                    priority = liq_priority_energy["priority"]
                                                                                                                                                                                                    prob_engine.add(liq_priority_energy["bias"], 3.0)
                                                                                                                                                                                                else:
                                                                                                                                                                                                    overbought_trap_old = OverboughtLiquidityTrap.detect(
                                                                                                                                                                                                        liq["short_dist"], liq["long_dist"],
                                                                                                                                                                                                        rsi6, up_energy, down_energy,
                                                                                                                                                                                                        ofi["bias"], ofi["strength"], volume_ratio, funding_rate or 0
                                                                                                                                                                                                    )
                                                                                                                                                                                                    if overbought_trap_old["override"]:
                                                                                                                                                                                                        final_bias = overbought_trap_old["bias"]
                                                                                                                                                                                                        final_reason = overbought_trap_old["reason"]
                                                                                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                                                                                        final_phase = "OVERBOUGHT_LIQ_TRAP"
                                                                                                                                                                                                        priority = overbought_trap_old["priority"]
                                                                                                                                                                                                        prob_engine.add(overbought_trap_old["bias"], 3.0)
                                                                                                                                                                                                    else:
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
                                                                                                                                                                                                        else:
                                                                                                                                                                                                            liq_energy = LiquidityEnergyCheck.detect(
                                                                                                                                                                                                                liq["short_dist"], liq["long_dist"],
                                                                                                                                                                                                                up_energy, down_energy,
                                                                                                                                                                                                                volume_ratio, ofi["bias"], ofi["strength"], rsi6_5m,
                                                                                                                                                                                                                obv_magnitude
                                                                                                                                                                                                            )
                                                                                                                                                                                                            if liq_energy["override"]:
                                                                                                                                                                                                                final_bias = liq_energy["bias"]
                                                                                                                                                                                                                final_reason = liq_energy["reason"]
                                                                                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                final_phase = "LIQUIDITY_ENERGY_TRAP"
                                                                                                                                                                                                                priority = liq_energy["priority"]
                                                                                                                                                                                                                prob_engine.add(liq_energy["bias"], 3.0)
                                                                                                                                                                                                            else:
                                                                                                                                                                                                                energy_imbalance = ExtremeEnergyImbalance.detect(
                                                                                                                                                                                                                    up_energy, down_energy, volume_ratio, rsi14,
                                                                                                                                                                                                                    change_5m, ofi["bias"], ofi["strength"],
                                                                                                                                                                                                                    rsi6, rsi6_5m
                                                                                                                                                                                                                )
                                                                                                                                                                                                                if energy_imbalance["override"]:
                                                                                                                                                                                                                    final_bias = energy_imbalance["bias"]
                                                                                                                                                                                                                    final_reason = energy_imbalance["reason"]
                                                                                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                    final_phase = "ENERGY_IMBALANCE"
                                                                                                                                                                                                                    priority = energy_imbalance["priority"]
                                                                                                                                                                                                                    prob_engine.add(energy_imbalance["bias"], 3.0)
                                                                                                                                                                                                                else:
                                                                                                                                                                                                                    thin_pump = ThinOrderBookPump.detect(
                                                                                                                                                                                                                        up_energy, down_energy, change_5m, volume_ratio,
                                                                                                                                                                                                                        ofi["bias"], ofi["strength"], liq["short_dist"]
                                                                                                                                                                                                                    )
                                                                                                                                                                                                                    if thin_pump["override"]:
                                                                                                                                                                                                                        final_bias = thin_pump["bias"]
                                                                                                                                                                                                                        final_reason = thin_pump["reason"]
                                                                                                                                                                                                                        final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                        final_phase = "THIN_ORDER_BOOK_PUMP"
                                                                                                                                                                                                                        priority = thin_pump["priority"]
                                                                                                                                                                                                                        prob_engine.add(thin_pump["bias"], 3.0)
                                                                                                                                                                                                                    else:
                                                                                                                                                                                                                        # NEW: PumpExhaustionTrap
                                                                                                                                                                                                                        pump_exhaust = PumpExhaustionTrap.detect(
                                                                                                                                                                                                                            change_5m, volume_ratio, down_energy,
                                                                                                                                                                                                                            liq["long_dist"], liq["short_dist"], rsi6
                                                                                                                                                                                                                        )
                                                                                                                                                                                                                        if pump_exhaust["override"]:
                                                                                                                                                                                                                            final_bias = pump_exhaust["bias"]
                                                                                                                                                                                                                            final_reason = pump_exhaust["reason"]
                                                                                                                                                                                                                            final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                            final_phase = "PUMP_EXHAUSTION_TRAP"
                                                                                                                                                                                                                            priority = pump_exhaust["priority"]
                                                                                                                                                                                                                            prob_engine.add(pump_exhaust["bias"], 3.0)
                                                                                                                                                                                                                        else:
                                                                                                                                                                                                                            energy_trap = EnergyTrapFilter.detect(
                                                                                                                                                                                                                                up_energy, down_energy, change_5m, volume_ratio, rsi14,
                                                                                                                                                                                                                                liq["short_dist"], rsi6_5m
                                                                                                                                                                                                                            )
                                                                                                                                                                                                                            if energy_trap["override"]:
                                                                                                                                                                                                                                final_bias = energy_trap["bias"]
                                                                                                                                                                                                                                final_reason = energy_trap["reason"]
                                                                                                                                                                                                                                final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                                final_phase = "ENERGY_TRAP"
                                                                                                                                                                                                                                priority = energy_trap["priority"]
                                                                                                                                                                                                                                prob_engine.add(energy_trap["bias"], 3.0)
                                                                                                                                                                                                                            else:
                                                                                                                                                                                                                                energy_gap = EnergyGapTrapDetector.detect(rsi14, up_energy, down_energy)
                                                                                                                                                                                                                                if energy_gap["override"]:
                                                                                                                                                                                                                                    final_bias = energy_gap["bias"]
                                                                                                                                                                                                                                    final_reason = energy_gap["reason"]
                                                                                                                                                                                                                                    final_confidence = "ABSOLUTE"
                                                                                                                                                                                                                                    final_phase = "ENERGY_GAP_TRAP"
                                                                                                                                                                                                                                    priority = energy_gap["priority"]
                                                                                                                                                                                                                                    prob_engine.add(energy_gap["bias"], 3.0)
                                                                                                                                                                                                                                else:
                                                                                                                                                                                                                                    # If no override, fallback to voting
                                                                                                                                                                                                                                    # 🔥 VotingWeightAggBoost: boost weight OFI & energy saat agg ekstrem
                                                                                                                                                                                                                                    agg_boost = 1.0
                                                                                                                                                                                                                                    if agg > 0.85 or agg < 0.15:
                                                                                                                                                                                                                                        agg_boost = 3.0  # agg ekstrem = signal 3x lebih kuat
                                                                                                                                                                                                                                    
                                                                                                                                                                                                                                    prob_engine.add(ofi["bias"], ofi["strength"] * 2.0 * agg_boost)
                                                                                                                                                                                                                                    
                                                                                                                                                                                                                                    # Boost energy weight saat agg ekstrem
                                                                                                                                                                                                                                    energy_bias = "LONG" if up_energy < down_energy else "SHORT"
                                                                                                                                                                                                                                    energy_weight = 1.0 * agg_boost
                                                                                                                                                                                                                                    prob_engine.add(energy_bias, energy_weight)
                                                                                                                                                                                                                                    if liq["short_dist"] < liq["long_dist"]:
                                                                                                                                                                                                                                        prob_engine.add("LONG", 1.0)
                                                                                                                                                                                                                                    else:
                                                                                                                                                                                                                                        prob_engine.add("SHORT", 1.0)
                                                                                                                                                                                                                                    wmi = self.fetcher.calculate_wmi(liq["short_dist"], liq["long_dist"])
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
                                                                                                                                                                                                                                                final_reason = f"Probabilistic override ({prob_conf:.1%}) over voting ({vote_result['confidence']:.1%})"
                                                                                                                                                                                                                                            else:
                                                                                                                                                                                                                                                final_bias = vote_result["bias"]
                                                                                                                                                                                                                                                final_reason = f"Voting override ({vote_result['confidence']:.1%}) over probabilistic ({prob_conf:.1%})"
                                                                                                                                                                                                                                            final_confidence = "ABSOLUTE" if max(prob_conf, vote_result["confidence"]) > 0.7 else "HIGH"
                                                                                                                                                                                                                                    elif prob_bias != "NEUTRAL":
                                                                                                                                                                                                                                        final_bias = prob_bias
                                                                                                                                                                                                                                        final_reason = f"Probabilistic engine: {prob_conf:.1%}"
                                                                                                                                                                                                                                        final_confidence = "ABSOLUTE" if prob_conf > 0.7 else "HIGH"
                                                                                                                                                                                                                                    elif vote_result["bias"] != "NEUTRAL":
                                                                                                                                                                                                                                        final_bias = vote_result["bias"]
                                                                                                                                                                                                                                        final_reason = f"Voting: {vote_result['confidence']:.1%}"
                                                                                                                                                                                                                                        final_confidence = "ABSOLUTE" if vote_result["confidence"] > 0.7 else "HIGH"
                                                                                                                                                                                                                                    else:
                                                                                                                                                                                                                                        final_bias = "LONG" if liq["short_dist"] < liq["long_dist"] else "SHORT"
                                                                                                                                                                                                                                        final_reason = "Fallback to liquidity proximity"
                                                                                                                                                                                                                                        final_confidence = "MEDIUM"
                                                                                                                                                                                                                                    final_phase = "PROBABILISTIC_VOTING"
                                                                                                                                                                                                                                    priority = 0

            # ========== NEW: EXTREME OVERBOUGHT/OVERSOLD CONTINUATION ==========
            # First check new lecturer's extreme continuation detectors (higher priority)
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
                    # Fallback to original extreme continuation detectors
                    extreme_overbought_cont = ExtremeOverboughtContinuation.detect(
                        rsi6_5m, volume_ratio, ofi["bias"], ofi["strength"], up_energy, liq["short_dist"]
                    )
                    if extreme_overbought_cont["override"]:
                        final_bias = extreme_overbought_cont["bias"]
                        final_reason = extreme_overbought_cont["reason"]
                        final_confidence = "ABSOLUTE"
                        final_phase = "EXTREME_OVERBOUGHT_CONT"
                        priority = extreme_overbought_cont["priority"]
                    else:
                        extreme_oversold_cont = ExtremeOversoldContinuation.detect(
                            rsi6_5m, volume_ratio, ofi["bias"], ofi["strength"], down_energy, liq["long_dist"]
                        )
                        if extreme_oversold_cont["override"]:
                            final_bias = extreme_oversold_cont["bias"]
                            final_reason = extreme_oversold_cont["reason"]
                            final_confidence = "ABSOLUTE"
                            final_phase = "EXTREME_OVERSOLD_CONT"
                            priority = extreme_oversold_cont["priority"]

            # ========== NEW: EXTREME OVERSOLD/OVERBOUGHT BOUNCE/DUMP OVERRIDE ==========
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
                    rsi6, volume_ratio, change_5m, ofi["bias"], ofi["strength"], liq["short_dist"], up_energy
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
                    volume_ratio, rsi6, rsi6_5m
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
                "position_multiplier": round(position_multiplier, 2)
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
                volume_ratio, rsi6, rsi6_5m
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
