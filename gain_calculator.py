#!/usr/bin/env python3
"""
Gain Staging Calculator — VU Meter Mode + Studio One Integration

Measurement principle
---------------------
0 VU  = -18 dBFS  (EBU R68)
Noise gate: 300 ms block RMS threshold
Level: 99th percentile of gated blocks  (Needle Hold 2000ms 기준 VU 동작 일치)

Stereo handling
---------------
채널 수 및 L/R 상관계수로 Mono / Dual Mono / Stereo 구분.
스테레오 파일은 RMS 기준 더 큰 채널만 사용해 분석.
(한쪽이 조용한 오버헤드 트랙 등에서 평균 내면 레벨이 과소 측정되는 문제 방지)

Track type detection
--------------------
파일명이 아닌 오디오 특성 (크레스트 팩터, 스펙트럴 센트로이드, 저주파 비율, 트랜지언트 레이트)
으로 트랙 종류를 자동 추론한다. 결과 테이블에 표시 용도. 타깃 dBFS에는 영향 없음.
모든 트랙의 목표 레벨은 동일하게 사용자가 설정한 Target (기본 -18 dBFS, 0 VU).

Gate
----
2-pass Otsu: 1차로 무음/신호 경계, 2차로 신호 구간 안에서 bleed/hit 경계를 자동 탐색.
OH/심벌/탐처럼 bleed와 실제 어택 사이에 명확한 gap이 있는 트랙에서 bleed를 자동 배제.

Peak-cap
--------
VU 기준 게인이 피크를 Peak Headroom 이상으로 올리면 게인을 자동으로 제한한다.
해당 트랙은 Status = "Peak-capped" 로 표시.

Studio One integration
----------------------
.song 파일(ZIP)의 audiomixer.xml에서 트랙 목록과 현재 gain을 읽어,
분석 결과를 직접 적용한다. 적용 전 자동으로 .song.bak 백업 생성.

Dependencies: numpy, soundfile  (pip install numpy soundfile)
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import threading
import urllib.parse
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ── Constants ──────────────────────────────────────────────────────────────────
VU_TAU_MS        = 300    # ms — noise gate block size
HOLD_BLOCKS      = 2      # gate hold (blocks)
MIN_VALID_MS     = 600    # minimum valid duration
DEFAULT_GATE_DB  = -50.0  # dBFS  (auto gate 비활성 시 사용)
DEFAULT_TARGET   = -18.0  # dBFS  (0 VU, EBU R68)
DEFAULT_PCT      = 99     # percentile — VU Needle Hold ~2000 ms 기준
DEFAULT_HEADROOM = -1.0   # dBFS  (peak-cap 헤드룸; 피크가 이 값을 넘지 않도록 게인 제한)


def _otsu_1d(vals: np.ndarray, lo: float, hi: float) -> float:
    """Otsu's method: 두 그룹 간 분산을 최대화하는 임계값 반환."""
    step = 0.5
    bins = np.arange(lo, hi + step, step)
    if len(bins) < 3:
        return lo
    hist, edges = np.histogram(vals, bins=bins)
    centers     = (edges[:-1] + edges[1:]) / 2.0
    total       = float(hist.sum())
    if total < 2:
        return lo

    cum     = np.cumsum(hist).astype(float)
    cum_sum = np.cumsum(hist * centers)

    best_thresh = lo
    best_var    = -1.0
    for i in range(1, len(centers)):
        w0 = cum[i - 1] / total
        w1 = 1.0 - w0
        if w0 < 1e-6 or w1 < 1e-6:
            continue
        m0    = cum_sum[i - 1] / (cum[i - 1] + 1e-10)
        m1    = (cum_sum[-1] - cum_sum[i - 1]) / (total - cum[i - 1] + 1e-10)
        var_b = w0 * w1 * (m0 - m1) ** 2
        if var_b > best_var:
            best_var    = var_b
            best_thresh = float(centers[i])
    return best_thresh


def _auto_gate_threshold(block_rms_db: np.ndarray) -> float:
    """
    2-pass Otsu gate threshold.

    1차: 전체 범위에서 무음/신호 경계 탐색 (T1).
    2차: 신호 구간(>= T1) 안에서 bleed/hit 경계 재탐색 (T2).
         OH/심벌/탐처럼 bleed와 실제 어택 사이에 명확한 gap이 있는 트랙에서
         bleed 구간을 자동으로 배제한다.
         gap < 5 dB 이거나 어느 한쪽 그룹이 5블록 미만이면 T1 사용.

    결과는 [-70, -15] dBFS 로 클리핑.
    """
    vals = np.clip(block_rms_db, -90.0, 0.0)

    # 1차: 무음 vs. 신호
    t1 = float(np.clip(_otsu_1d(vals, -90.0, 0.0), -70.0, -15.0))

    # 2차: 신호 구간 안에서 bleed vs. hit
    signal = vals[vals >= t1]
    if len(signal) >= 10:
        t2_raw  = _otsu_1d(signal, float(t1), 0.0)
        t2      = float(np.clip(t2_raw, -70.0, -15.0))
        gap     = t2 - t1
        n_bleed = int(np.sum((signal >= t1) & (signal < t2)))
        n_hit   = int(np.sum(signal >= t2))
        if gap >= 5.0 and n_bleed >= 5 and n_hit >= 5:
            return t2

    return t1


# ── Track type detection (audio-based) ─────────────────────────────────────────
TYPE_LABEL: dict[str, str] = {
    "Kick": "Kick", "Snare": "Snare", "Tom": "Tom",
    "HiHat": "Hi-Hat", "Overhead": "OH", "Cymbal": "Cym",
    "Bass": "Bass", "Guitar": "Gtr", "Vocal": "Vox", "Other": "—",
}


def _audio_features(mono: np.ndarray, sr: int) -> dict:
    """
    크레스트 팩터 · 스펙트럴 센트로이드 · 저주파 비율 · 트랜지언트 레이트 계산.

    스펙트럴 특성: 전체 트랙을 4등분 후 가장 큰 2초 구간 사용.
    (첫 구간에 무음이 길어 오분류되는 트랙 방지)
    트랜지언트 레이트: 전체 트랙 기준.
    """
    rms      = float(np.sqrt(np.mean(mono ** 2)))
    peak     = float(np.max(np.abs(mono)))
    crest_db = 20.0 * np.log10(peak / max(rms, 1e-10))

    n_fft  = min(len(mono), sr * 2)
    seg    = mono[:n_fft]
    mag    = np.abs(np.fft.rfft(seg))
    freqs  = np.fft.rfftfreq(n_fft, 1.0 / sr)
    total  = max(float(mag.sum()), 1e-10)

    spectral_centroid = float((freqs * mag).sum() / total)
    lf_ratio          = float(mag[freqs < 200].sum() / total)

    block = max(1, int(sr * 0.01))
    nb    = len(mono) // block
    if nb > 1:
        brms           = np.sqrt(
            np.mean(mono[: nb * block].reshape(nb, block) ** 2, axis=1)
        )
        thresh         = 0.15 * float(brms.max())
        rises          = int(np.sum(np.diff(brms) > thresh))
        transient_rate = rises / (len(mono) / sr)
    else:
        transient_rate = 0.0

    return dict(
        crest_db=crest_db,
        spectral_centroid_hz=spectral_centroid,
        lf_ratio=lf_ratio,
        transient_rate=transient_rate,
    )


def infer_track_type(feats: dict, n_ch: int) -> str:
    """
    오디오 특성으로 트랙 종류 추론. 표시 목적이며 게인 계산에는 영향 없음.

    임계값 설계 근거:
      drum_like  cf > 17  — cf 15-17 대는 vocal/guitar (cf<17) 와 drum (cf>17) 경계
      lf_bass    lf > 0.25 + cf < 15  — bass (lf 0.27-0.30, cf 13-14)
      lf_kick    lf > 0.28 + drum_like — kick (lf 0.31+)
      Tom        tr < 0.7  — 탐은 히트 빈도가 매우 낮음
      Snare      tr > 4  or (tr > 1 and sc > 5000)  — 스네어 바텀/탑 구분
      Cymbal     sc > 2500  — 라이드/심벌 고주파
      Overhead   나머지 타악기 (OH, HH 포함)
    """
    cf = feats["crest_db"]
    sc = feats["spectral_centroid_hz"]
    lf = feats["lf_ratio"]
    tr = feats["transient_rate"]

    drum_like = cf > 17

    # Bass: 저주파 중심 + 타격성 낮음
    if lf > 0.25 and cf < 15:
        return "Bass"

    # Kick: 저주파 지배 + 타격성
    if lf > 0.28 and drum_like:
        return "Kick"

    # 지속음 (Guitar / Vocal)
    if not drum_like:
        if sc > 4000:
            return "Vocal"
        if sc > 1000:
            return "Guitar"
        return "Other"

    # 타악기 (drum_like, lf <= 0.28)
    if tr < 0.7:                          # 히트 빈도 매우 낮음 → Tom
        return "Tom"
    if tr > 8:                            # 빠른 반복 → HiHat
        return "HiHat"
    if tr > 4 or (tr > 1.0 and sc > 5000):  # 중간-높은 tr, 또는 매우 밝은 타악기 → Snare
        return "Snare"
    if sc > 2500:                         # 밝은 심벌류 → Cymbal
        return "Cymbal"
    return "Overhead"                     # OH / HH (저-중 sc, 낮은 tr)


# ── Core analysis ──────────────────────────────────────────────────────────────
def analyze_file(
    filepath: str,
    gate_thresh_db: float = DEFAULT_GATE_DB,
    target_db: float = DEFAULT_TARGET,
    percentile: int = DEFAULT_PCT,
    auto_gate: bool = True,
) -> dict:
    """
    WAV 파일을 읽고 VU 레벨을 분석한다.

    반환값에 peak_db 포함 — peak-cap 계산에 사용.
    """
    name = Path(filepath).name
    try:
        audio, sr = sf.read(filepath, always_2d=True)
    except Exception as exc:
        return {
            "file": name, "error": str(exc),
            "track_type": "Other", "stereo_info": "?",
        }

    n_ch = audio.shape[1]

    # ── Stereo 판별 ──────────────────────────────────────────────────────────
    if n_ch == 1:
        stereo_info = "Mono"
    else:
        n_s = min(len(audio), sr * 5)
        corr = (
            float(np.corrcoef(audio[:n_s, 0], audio[:n_s, 1])[0, 1])
            if n_s > 1 else 1.0
        )
        stereo_info = "Dual Mono" if corr > 0.99 else "Stereo"

    # ── 모노 변환: 스테레오는 큰 채널 사용 ───────────────────────────────────
    if n_ch == 1:
        mono = audio[:, 0]
    else:
        rms_ch = np.sqrt(np.mean(audio ** 2, axis=0))
        mono   = audio[:, int(np.argmax(rms_ch))]

    total_dur = len(mono) / sr

    # ── 트랙 타입 추론 (표시용) ──────────────────────────────────────────────
    feats      = _audio_features(mono, sr)
    track_type = infer_track_type(feats, n_ch)

    # ── RMS 분석 ──────────────────────────────────────────────────────────────
    block_size   = max(1, int(sr * VU_TAU_MS / 1000))
    n_blocks     = len(mono) // block_size
    if n_blocks == 0:
        return _no_signal(name, total_dur, 0.0, track_type, stereo_info)

    blocks       = mono[: n_blocks * block_size].reshape(n_blocks, block_size)
    block_power  = np.mean(blocks ** 2, axis=1)
    block_rms_db = 10.0 * np.log10(np.maximum(block_power, 1e-10))

    if auto_gate:
        gate_thresh_db = _auto_gate_threshold(block_rms_db)

    active = block_rms_db >= gate_thresh_db
    held   = active.copy()
    for lag in range(1, HOLD_BLOCKS + 1):
        held[lag:] |= active[: n_blocks - lag]

    valid_dur = held.sum() * VU_TAU_MS / 1000
    if valid_dur < MIN_VALID_MS / 1000.0:
        return _no_signal(name, total_dur, valid_dur, track_type, stereo_info)

    level_db  = float(np.percentile(block_rms_db[held], percentile))
    offset_db = target_db - level_db

    peak_db = 20.0 * np.log10(max(float(np.max(np.abs(mono))), 1e-10))
    warning = (peak_db + offset_db) > 0.0

    return {
        "file":           name,
        "track_type":     track_type,
        "stereo_info":    stereo_info,
        "gate_used":      gate_thresh_db,
        "duration":       total_dur,
        "valid_duration": valid_dur,
        "level_db":       level_db,
        "peak_db":        peak_db,
        "offset_db":      offset_db,
        "status":         "Peak Clip Risk" if warning else "OK",
        "warning":        warning,
    }


def _no_signal(
    name: str, total_dur: float, valid_dur: float,
    track_type: str = "Other", stereo_info: str = "?",
) -> dict:
    return {
        "file": name, "track_type": track_type, "stereo_info": stereo_info,
        "duration": total_dur, "valid_duration": valid_dur,
        "level_db": None, "peak_db": None, "offset_db": 0.0,
        "status": "No Signal", "warning": False,
    }


# ── Studio One integration ─────────────────────────────────────────────────────
def parse_song_file(song_path: str) -> dict:
    """
    .song(ZIP) 파일을 파싱해 트랙 정보를 반환한다.

    Returns:
        {label: {"current_inputfx_gain_db": float, "wav_path": str|None}}
    """
    with zipfile.ZipFile(song_path) as z:
        mixer_xml = z.read("Devices/audiomixer.xml").decode("utf-8")
        pool_xml  = z.read("Song/mediapool.xml").decode("utf-8", errors="replace")

    tracks: dict[str, dict] = {}
    for m in re.finditer(r"<AudioTrackChannel\b[^>]+>.*?</AudioTrackChannel>",
                         mixer_xml, re.DOTALL):
        block   = m.group(0)
        label   = re.search(r'label="([^"]+)"', block)
        inputfx = re.search(r'<Attributes\s+x:id="InputFX"\s+gain="([^"]+)"', block)
        if label:
            tracks[label.group(1)] = {
                "current_inputfx_gain_db": float(inputfx.group(1)) if inputfx else 0.0,
                "wav_path":                None,
            }

    def _strip_prefix(lbl: str) -> str:
        return re.sub(r'^\d+\s*-?\s*', '', lbl).lower()

    def _strip_prefix_suffix(lbl: str) -> str:
        core = re.sub(r'^\d+\s*-?\s*', '', lbl)
        return re.sub(r'\s+\d+$', '', core).lower()

    unmatched   = set(tracks.keys())
    label_order = list(tracks.keys())

    def _trailing_num(s: str) -> str | None:
        m = re.search(r'\s+(\d+)$', s)
        return m.group(1) if m else None

    def _find_match_prefix(stem_lower: str) -> str | None:
        norm_stem = _strip_prefix(stem_lower)
        for lbl in label_order:
            if lbl in unmatched and _strip_prefix(lbl) == norm_stem:
                return lbl
        return None

    def _find_match_prefix_suffix(stem_lower: str) -> str | None:
        stem_core = _strip_prefix(stem_lower)
        stem_num  = _trailing_num(stem_core)
        norm_stem = re.sub(r'\s+\d+$', '', stem_core)
        for lbl in label_order:
            if lbl not in unmatched:
                continue
            lbl_core = _strip_prefix(lbl)
            lbl_num  = _trailing_num(lbl_core)
            lbl_base = re.sub(r'\s+\d+$', '', lbl_core)
            if lbl_base != norm_stem:
                continue
            if stem_num and lbl_num and stem_num != lbl_num:
                continue
            return lbl
        return None

    for url in re.findall(r'url="file:///([^"]+\.(?:wav|WAV))"', pool_xml):
        decoded    = urllib.parse.unquote(url).replace("/", os.sep)
        stem       = Path(decoded).stem
        stem_lower = stem.lower()

        if stem in unmatched:
            tracks[stem]["wav_path"] = decoded
            unmatched.discard(stem)
            continue

        matched = _find_match_prefix(stem_lower)
        if not matched:
            matched = _find_match_prefix_suffix(stem_lower)
        if matched:
            tracks[matched]["wav_path"] = decoded
            unmatched.discard(matched)

    for label, v in tracks.items():
        if v["wav_path"] and not Path(v["wav_path"]).exists():
            v["wav_path"] = None
            unmatched.add(label)

    if unmatched:
        wav_dirs = {
            Path(v["wav_path"]).parent
            for v in tracks.values()
            if v["wav_path"] and Path(v["wav_path"]).exists()
        }
        assigned = {v["wav_path"] for v in tracks.values() if v["wav_path"]}

        for wav_dir in wav_dirs:
            for wav_file in sorted(wav_dir.glob("*.wav")):
                decoded = str(wav_file)
                if decoded in assigned:
                    continue
                stem_lower = wav_file.stem.lower()

                if wav_file.stem in unmatched:
                    matched = wav_file.stem
                else:
                    matched = _find_match_prefix(stem_lower)
                    if not matched:
                        matched = _find_match_prefix_suffix(stem_lower)

                if matched:
                    tracks[matched]["wav_path"] = decoded
                    unmatched.discard(matched)
                    assigned.add(decoded)

    return tracks


def apply_gains_to_song(song_path: str, gain_map: dict[str, float]) -> str:
    """
    .song 파일의 audiomixer.xml에서 해당 트랙의 gain 속성을 갱신한다.
    수정 전 .song.bak 백업을 생성하고, 백업 경로를 반환한다.

    gain_map: {track_label: new_gain_db}
    """
    backup = song_path + ".bak"
    shutil.copy2(song_path, backup)

    files:  dict[str, bytes] = {}
    ctypes: dict[str, int]   = {}
    with zipfile.ZipFile(song_path) as z:
        for info in z.infolist():
            ctypes[info.filename] = info.compress_type
            files[info.filename]  = z.read(info.filename)

    mixer = files["Devices/audiomixer.xml"].decode("utf-8")

    def _replace(m: re.Match) -> str:
        block     = m.group(0)
        lbl_match = re.search(r'label="([^"]+)"', block)
        if lbl_match and lbl_match.group(1) in gain_map:
            new_g_db = gain_map[lbl_match.group(1)]
            block = re.sub(
                r'(<Attributes\s+x:id="InputFX"\s+gain=")[^"]+(")',
                rf'\g<1>{new_g_db:.6f}\g<2>',
                block,
            )
        return block

    mixer = re.sub(r"<AudioTrackChannel\b[^>]+>.*?</AudioTrackChannel>",
                   _replace, mixer, flags=re.DOTALL)
    files["Devices/audiomixer.xml"] = mixer.encode("utf-8")

    with zipfile.ZipFile(song_path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data,
                       compress_type=ctypes.get(name, zipfile.ZIP_DEFLATED))

    return backup


# ── GUI ────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Gain Staging Calculator  ·  VU Meter Mode")
        self.geometry("1300x680")
        self.minsize(960, 500)
        self._results:      list[dict]      = []
        self._sort_reverse: dict[str, bool] = {}
        self._song_path:    str | None      = None
        self._song_tracks:  dict            = {}
        self._auto_gate = tk.BooleanVar(value=True)
        self._build_ui()

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        # ── Row 0: WAV 폴더 ───────────────────────────────────────────────────
        ttk.Label(top, text="WAV Folder:").grid(row=0, column=0, sticky="w")
        self._folder = tk.StringVar()
        ttk.Entry(top, textvariable=self._folder, width=45).grid(
            row=0, column=1, columnspan=2, padx=4, sticky="ew")
        ttk.Button(top, text="Browse...", command=self._browse_folder).grid(
            row=0, column=3, padx=(4, 0))
        self._recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Subfolders", variable=self._recursive).grid(
            row=0, column=4, padx=8)

        # ── Row 1: .song 파일 ─────────────────────────────────────────────────
        ttk.Label(top, text=".song File:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._song_var = tk.StringVar()
        ttk.Entry(top, textvariable=self._song_var, width=45, state="readonly").grid(
            row=1, column=1, columnspan=2, padx=4, sticky="ew", pady=(6, 0))
        ttk.Button(top, text="Load .song", command=self._load_song).grid(
            row=1, column=3, padx=(4, 0), pady=(6, 0))
        self._song_info = ttk.Label(top, text="", foreground="gray")
        self._song_info.grid(row=1, column=4, padx=8, sticky="w", pady=(6, 0))

        # ── Row 2: 기본 파라미터 ──────────────────────────────────────────────
        ttk.Label(top, text="Gate:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._gate = tk.DoubleVar(value=DEFAULT_GATE_DB)
        self._gate_spin = ttk.Spinbox(top, from_=-70, to=-10, increment=1,
                                      textvariable=self._gate, width=6)
        self._gate_spin.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        self._auto_gate_cb = ttk.Checkbutton(
            top, text="Auto", variable=self._auto_gate,
            command=self._on_auto_gate_toggle)
        self._auto_gate_cb.grid(row=2, column=1, sticky="e", pady=(6, 0))
        self._on_auto_gate_toggle()

        ttk.Label(top, text="Target:").grid(row=2, column=2, sticky="e",
                                            padx=(12, 4), pady=(6, 0))
        self._target = tk.DoubleVar(value=DEFAULT_TARGET)
        ttk.Spinbox(top, from_=-30, to=-6, increment=0.5,
                    textvariable=self._target, width=6).grid(
            row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Label(top, text="dBFS  |  Needle Hold pct:", foreground="gray").grid(
            row=2, column=4, sticky="w", padx=8, pady=(6, 0))
        self._percentile = tk.IntVar(value=DEFAULT_PCT)
        ttk.Spinbox(top, from_=50, to=100, increment=1,
                    textvariable=self._percentile, width=5).grid(
            row=2, column=4, sticky="e", pady=(6, 0))

        # ── Row 3: Peak-cap 헤드룸 ────────────────────────────────────────────
        ttk.Label(top, text="Peak Headroom:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._headroom = tk.DoubleVar(value=DEFAULT_HEADROOM)
        ttk.Spinbox(top, from_=-12, to=0, increment=0.5,
                    textvariable=self._headroom, width=6).grid(
            row=3, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(
            top,
            text="dBFS  —  Peak Clip Risk 트랙에 한해 피크가 이 값을 넘지 않도록 게인을 제한",
            foreground="gray",
        ).grid(row=3, column=2, columnspan=3, sticky="w", padx=4, pady=(6, 0))

        # ── Analyze / Write 버튼 ──────────────────────────────────────────────
        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=0, column=5, rowspan=4, padx=(16, 0), sticky="ns")
        self._run_btn = ttk.Button(btn_frame, text="  Analyze  ", command=self._start)
        self._run_btn.pack(fill="x", pady=(0, 4))
        self._write_btn = ttk.Button(btn_frame, text="Write to .song",
                                     command=self._write_song, state="disabled")
        self._write_btn.pack(fill="x")

        top.columnconfigure(1, weight=1)

        # ── Progress ──────────────────────────────────────────────────────────
        prog = ttk.Frame(self, padding=(10, 2, 10, 2))
        prog.pack(fill="x")
        self._pct = tk.DoubleVar()
        ttk.Progressbar(prog, variable=self._pct, maximum=100).pack(
            side="left", fill="x", expand=True)
        self._stat = ttk.Label(prog, text="", width=20, anchor="e")
        self._stat.pack(side="right")

        # ── Results table ─────────────────────────────────────────────────────
        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, padx=10, pady=4)

        COLS = (
            ("track",  "S1 Track",     155, "w"),
            ("ch",     "Ch",            65, "center"),
            ("type",   "Type",          65, "center"),
            ("file",   "WAV File",     165, "w"),
            ("dur",    "Total (s)",     65, "center"),
            ("valid",  "Valid (s)",     65, "center"),
            ("gate",   "Gate (dBFS)",   85, "center"),
            ("level",  "Level (dBFS)",  95, "center"),
            ("peak",   "Peak (dBFS)",   90, "center"),
            ("offset", "VU Offset",     85, "center"),
            ("gain",   "New Gain",      95, "center"),
            ("status", "Status",       140, "center"),
        )
        self._tree = ttk.Treeview(
            tf, columns=[c[0] for c in COLS], show="headings", selectmode="browse")
        for col, head, w, anchor in COLS:
            self._tree.heading(col, text=head,
                               command=lambda c=col: self._sort(c))
            self._tree.column(col, width=w, anchor=anchor, minwidth=40)

        sb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.tag_configure("ok",      foreground="#1a6b1a")
        self._tree.tag_configure("capped",  foreground="#8b6914")
        self._tree.tag_configure("warn",    foreground="#b84000")
        self._tree.tag_configure("none",    foreground="#888888")
        self._tree.tag_configure("err",     foreground="#cc0000")
        self._tree.tag_configure("nomatch", foreground="#555555")

        # ── Bottom ────────────────────────────────────────────────────────────
        bot = ttk.Frame(self, padding=(10, 4))
        bot.pack(fill="x")
        ttk.Button(bot, text="Export CSV", command=self._export).pack(side="left")
        ttk.Button(bot, text="Clear",      command=self._clear ).pack(side="left", padx=8)
        ttk.Label(
            bot,
            text="Green = OK  |  Gold = Peak-capped (VU 제한됨)  |"
                 "  Write to .song 은 Studio One 닫은 후 실행",
            foreground="gray",
        ).pack(side="right")

    # ── Auto Gate toggle ──────────────────────────────────────────────────────
    def _on_auto_gate_toggle(self) -> None:
        state = "disabled" if self._auto_gate.get() else "normal"
        self._gate_spin.config(state=state)

    # ── Sort ──────────────────────────────────────────────────────────────────
    def _sort(self, col: str) -> None:
        items = [(self._tree.set(k, col), k) for k in self._tree.get_children()]
        rev   = self._sort_reverse.get(col, False)
        try:
            items.sort(key=lambda t: float(t[0].replace("+", "").split()[0]), reverse=rev)
        except ValueError:
            items.sort(reverse=rev)
        for idx, (_, k) in enumerate(items):
            self._tree.move(k, "", idx)
        self._sort_reverse[col] = not rev

    # ── .song 로드 ────────────────────────────────────────────────────────────
    def _load_song(self) -> None:
        path = filedialog.askopenfilename(
            title="Studio One 프로젝트 선택",
            filetypes=[("Studio One Song", "*.song"), ("All files", "*.*")])
        if not path:
            return
        try:
            tracks = parse_song_file(path)
        except Exception as exc:
            messagebox.showerror("파싱 오류", str(exc))
            return

        self._song_path   = path
        self._song_tracks = tracks
        self._song_var.set(path)

        n_total   = len(tracks)
        n_matched = sum(1 for v in tracks.values() if v["wav_path"])

        wav_paths = [v["wav_path"] for v in tracks.values() if v["wav_path"]]
        if wav_paths:
            self._folder.set(str(Path(wav_paths[0]).parent))

        self._song_info.config(
            text=f"{n_total} tracks  |  {n_matched} WAV matched")
        self._write_btn.config(state="disabled")
        self._clear()

    # ── Analyze ───────────────────────────────────────────────────────────────
    def _browse_folder(self) -> None:
        d = filedialog.askdirectory(title="WAV 폴더 선택")
        if d:
            self._folder.set(d)

    def _start(self) -> None:
        if self._song_tracks:
            files = [
                (label, Path(v["wav_path"]))
                for label, v in self._song_tracks.items()
                if v["wav_path"] and Path(v["wav_path"]).exists()
            ]
            missing = [
                label for label, v in self._song_tracks.items()
                if not v["wav_path"] or not Path(v["wav_path"]).exists()
            ]
        else:
            folder = self._folder.get().strip()
            if not folder or not Path(folder).is_dir():
                messagebox.showerror("Error", "폴더를 선택하세요.")
                return
            pattern = "**/*.wav" if self._recursive.get() else "*.wav"
            files   = [(None, fp) for fp in sorted(Path(folder).glob(pattern))]
            missing = []

        if not files:
            messagebox.showinfo("파일 없음", "분석할 WAV 파일이 없습니다.")
            return

        self._clear()
        self._run_btn.config(state="disabled")

        for lbl in missing:
            self._tree.insert("", "end",
                values=(lbl, "—", "—", "— WAV not found —",
                        "", "", "", "", "", "", "", ""),
                tags=("nomatch",))

        threading.Thread(
            target=self._worker, args=(files,), daemon=True
        ).start()

    def _worker(self, files: list[tuple]) -> None:
        gate      = self._gate.get()
        target    = self._target.get()
        pct       = self._percentile.get()
        auto_gate = self._auto_gate.get()
        headroom  = self._headroom.get()
        n         = len(files)

        for i, (label, fp) in enumerate(files):
            self.after(0, lambda i=i, n=n: self._stat.config(text=f"{i+1} / {n}"))
            result             = analyze_file(str(fp), gate, target, pct, auto_gate=auto_gate)
            result["label"]    = label
            result["wav_path"] = str(fp)
            result["headroom"] = headroom
            self._results.append(result)
            self.after(0, self._add_row, result)
            self.after(0, self._pct.set, (i + 1) / n * 100)

        self.after(0, self._done)

    def _done(self) -> None:
        self._run_btn.config(state="normal")
        n = len(self._results)
        self._stat.config(text=f"Done  ({n} files)")
        if self._song_path and any(
            r.get("label") and r.get("status") not in ("No Signal",) and "error" not in r
            for r in self._results
        ):
            self._write_btn.config(state="normal")

    # ── Peak-cap helper ───────────────────────────────────────────────────────
    @staticmethod
    def _calc_effective_gain(r: dict) -> tuple[float, bool]:
        """(effective_gain_db, was_peak_capped) 반환.

        VU 게인을 그대로 적용하면 peak_db + vu_gain > headroom 이 되는 경우,
        peak_db + effective_gain == headroom 이 되도록 게인을 제한한다.
        """
        vu_gain  = r["offset_db"]
        peak_db  = r.get("peak_db")
        headroom = r.get("headroom", DEFAULT_HEADROOM)
        if peak_db is not None and r.get("warning"):
            safe = headroom - peak_db
            if safe < vu_gain:
                return safe, True
        return vu_gain, False

    # ── Table row ─────────────────────────────────────────────────────────────
    def _add_row(self, r: dict) -> None:
        label    = r.get("label") or "—"
        wav_name = Path(r.get("wav_path", r.get("file", "?"))).name
        ch_str   = r.get("stereo_info", "?")
        type_str = TYPE_LABEL.get(r.get("track_type", "Other"), "—")

        if "error" in r:
            self._tree.insert("", "end",
                values=(label, ch_str, type_str, wav_name,
                        "—", "—", "—", "ERROR", "—", "—", "—", r["error"]),
                tags=("err",))
            return

        level    = f"{r['level_db']:.1f}"  if r["level_db"] is not None else "N/A"
        peak_str = f"{r['peak_db']:.1f}"   if r.get("peak_db") is not None else "—"
        offset   = f"{r['offset_db']:+.1f}" if r["status"] != "No Signal" else "—"
        gate_str = f"{r['gate_used']:.1f}" if "gate_used" in r else "—"

        if self._song_tracks and r.get("label") and r["status"] != "No Signal":
            eff_gain, capped = self._calc_effective_gain(r)
            if capped:
                gain_str   = f"{eff_gain:+.2f} dB  (cap)"
                status_str = "Peak-capped"
                tag        = "capped"
            else:
                gain_str   = f"{eff_gain:+.2f} dB"
                status_str = r["status"]
                tag        = "ok"
        else:
            gain_str   = "—"
            status_str = r["status"]
            tag        = "none" if r["status"] == "No Signal" else "ok"

        self._tree.insert("", "end",
            values=(
                label, ch_str, type_str, wav_name,
                f"{r['duration']:.1f}",
                f"{r['valid_duration']:.1f}",
                gate_str, level, peak_str, offset,
                gain_str, status_str,
            ),
            tags=(tag,))

    # ── Write to .song ────────────────────────────────────────────────────────
    def _write_song(self) -> None:
        if not self._song_path:
            return

        gain_map:    dict[str, float] = {}
        capped_list: list[str]        = []

        for r in self._results:
            if "error" in r or r["status"] == "No Signal":
                continue
            label = r.get("label")
            if not label:
                continue
            eff_gain, capped = self._calc_effective_gain(r)
            gain_map[label] = eff_gain
            if capped:
                capped_list.append(label)

        if not gain_map:
            messagebox.showinfo("없음", "적용할 트랙이 없습니다.")
            return

        lines = "\n".join(
            f"  {'[cap] ' if lbl in capped_list else '      '}{lbl:25s}  {g:+.2f} dB"
            for lbl, g in gain_map.items()
        )
        cap_note = (
            f"\n\n[cap] 표시 {len(capped_list)}개 트랙: VU 게인이 Peak Headroom 초과 → 자동 제한됨"
            if capped_list else ""
        )
        if not messagebox.askyesno(
            "확인",
            f"아래 게인을 .song 파일에 적용합니다:\n\n{lines}{cap_note}\n\n"
            f".song.bak 백업이 자동 생성됩니다.\n\n계속하시겠습니까?"
        ):
            return

        try:
            backup = apply_gains_to_song(self._song_path, gain_map)
            messagebox.showinfo("완료",
                f"게인 적용 완료!\n\n"
                f"수정 파일: {self._song_path}\n"
                f"백업:      {backup}")
        except Exception as exc:
            messagebox.showerror("오류", str(exc))

    # ── Export CSV ────────────────────────────────────────────────────────────
    def _export(self) -> None:
        if not self._results:
            messagebox.showinfo("없음", "먼저 분석을 실행하세요.")
            return
        folder = self._folder.get().strip() or str(Path.home())
        out    = Path(folder) / "gain_staging_report.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["S1 Track", "Ch", "Type", "WAV File",
                        "Total (s)", "Valid (s)", "Gate (dBFS)",
                        "Level (dBFS)", "Peak (dBFS)", "VU Offset (dB)",
                        "New Gain (dB)", "Status"])
            for r in self._results:
                label = r.get("label") or "—"
                wav   = Path(r.get("wav_path", r.get("file", "?"))).name
                ch    = r.get("stereo_info", "?")
                typ   = r.get("track_type", "Other")
                gate  = f"{r['gate_used']:.1f}" if "gate_used" in r else "—"
                pk    = f"{r['peak_db']:.2f}" if r.get("peak_db") is not None else "—"
                if "error" in r:
                    w.writerow([label, ch, typ, wav,
                                "—", "—", "—", "ERROR", "—", "—", "—", r["error"]])
                else:
                    lv       = f"{r['level_db']:.2f}" if r["level_db"] is not None else "N/A"
                    off      = f"{r['offset_db']:+.2f}" if r["status"] != "No Signal" else "0.00"
                    eff, cap = self._calc_effective_gain(r)
                    eff_str  = f"{eff:+.2f}" if r["status"] != "No Signal" else "0.00"
                    status   = "Peak-capped" if cap else r["status"]
                    w.writerow([label, ch, typ, wav,
                                f"{r['duration']:.2f}", f"{r['valid_duration']:.2f}",
                                gate, lv, pk, off, eff_str, status])
        messagebox.showinfo("저장", f"CSV 저장:\n{out}")

    # ── Clear ─────────────────────────────────────────────────────────────────
    def _clear(self) -> None:
        self._results.clear()
        self._tree.delete(*self._tree.get_children())
        self._pct.set(0)
        self._stat.config(text="")
        self._write_btn.config(state="disabled")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    App().mainloop()
