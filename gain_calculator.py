#!/usr/bin/env python3
"""
Gain Staging Calculator — VU Meter Mode + Studio One Integration

Measurement principle
---------------------
0 VU  = -18 dBFS  (EBU R68)
Noise gate: 300 ms block RMS threshold
Level: 99th percentile of gated blocks  (Needle Hold 2000ms 기준 VU 동작 일치)

Studio One integration
----------------------
.song 파일(ZIP)의 audiomixer.xml에서 트랙 목록과 현재 gain을 읽어,
분석 결과를 직접 적용한다. 적용 전 자동으로 .song.bak 백업 생성.

Dependencies: numpy, soundfile, scipy  (pip install numpy soundfile scipy)
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
VU_TAU_MS       = 300    # ms — noise gate block size
HOLD_BLOCKS     = 2      # gate hold (blocks)
MIN_VALID_MS    = 600    # minimum valid duration
DEFAULT_GATE_DB = -35.0  # dBFS
DEFAULT_TARGET  = -18.0  # dBFS  (0 VU)
DEFAULT_PCT     = 99     # percentile — matches VU Needle Hold ~2000 ms


# ── Core analysis ──────────────────────────────────────────────────────────────
def analyze_file(
    filepath: str,
    gate_thresh_db: float = DEFAULT_GATE_DB,
    target_db: float = DEFAULT_TARGET,
    percentile: int = DEFAULT_PCT,
) -> dict:
    name = Path(filepath).name
    try:
        audio, sr = sf.read(filepath, always_2d=True)
    except Exception as exc:
        return {"file": name, "error": str(exc)}

    mono      = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    total_dur = len(mono) / sr

    block_size   = max(1, int(sr * VU_TAU_MS / 1000))
    n_blocks     = len(mono) // block_size
    if n_blocks == 0:
        return _no_signal(name, total_dur, 0.0)

    blocks       = mono[: n_blocks * block_size].reshape(n_blocks, block_size)
    block_power  = np.mean(blocks ** 2, axis=1)
    block_rms_db = 10.0 * np.log10(np.maximum(block_power, 1e-10))

    active = block_rms_db >= gate_thresh_db
    held   = active.copy()
    for lag in range(1, HOLD_BLOCKS + 1):
        held[lag:] |= active[: n_blocks - lag]

    valid_dur = held.sum() * VU_TAU_MS / 1000
    if valid_dur < MIN_VALID_MS / 1000.0:
        return _no_signal(name, total_dur, valid_dur)

    level_db  = float(np.percentile(block_rms_db[held], percentile))
    offset_db = target_db - level_db

    peak_db = 20.0 * np.log10(max(float(np.max(np.abs(mono))), 1e-10))
    warning = (peak_db + offset_db) > 0.0

    return {
        "file":           name,
        "duration":       total_dur,
        "valid_duration": valid_dur,
        "level_db":       level_db,
        "offset_db":      offset_db,
        "status":         "Peak Clip Risk" if warning else "OK",
        "warning":        warning,
    }


def _no_signal(name: str, total_dur: float, valid_dur: float) -> dict:
    return {
        "file": name, "duration": total_dur, "valid_duration": valid_dur,
        "level_db": None, "offset_db": 0.0, "status": "No Signal", "warning": False,
    }


# ── Studio One integration ─────────────────────────────────────────────────────
def parse_song_file(song_path: str) -> dict:
    """
    .song(ZIP) 파일을 파싱해 트랙 정보를 반환한다.

    Returns:
        {label: {"current_gain": float, "wav_path": str|None}}
    """
    with zipfile.ZipFile(song_path) as z:
        mixer_xml = z.read("Devices/audiomixer.xml").decode("utf-8")
        pool_xml  = z.read("Song/mediapool.xml").decode("utf-8", errors="replace")

    # audiomixer.xml → 트랙 label + InputFX gain (dB)
    tracks: dict[str, dict] = {}
    for m in re.finditer(r"<AudioTrackChannel\b[^>]+>.*?</AudioTrackChannel>",
                         mixer_xml, re.DOTALL):
        block = m.group(0)
        label = re.search(r'label="([^"]+)"', block)
        inputfx = re.search(r'<Attributes\s+x:id="InputFX"\s+gain="([^"]+)"', block)
        if label:
            tracks[label.group(1)] = {
                "current_inputfx_gain_db": float(inputfx.group(1)) if inputfx else 0.0,
                "wav_path":                None,
            }

    # mediapool.xml → WAV 절대 경로, 파일명(stem)으로 트랙에 매칭
    for url in re.findall(r'url="file:///([^"]+\.(?:wav|WAV))"', pool_xml):
        decoded = urllib.parse.unquote(url).replace("/", os.sep)
        stem    = Path(decoded).stem
        if stem in tracks:
            tracks[stem]["wav_path"] = decoded

    return tracks


def apply_gains_to_song(song_path: str, gain_map: dict[str, float]) -> str:
    """
    .song 파일의 audiomixer.xml에서 해당 트랙의 gain 속성을 갱신한다.
    수정 전 .song.bak 백업을 생성하고, 백업 경로를 반환한다.

    gain_map: {track_label: new_gain_linear}
    """
    backup = song_path + ".bak"
    shutil.copy2(song_path, backup)

    # ZIP 전체 읽기
    files: dict[str, bytes] = {}
    ctypes: dict[str, int]  = {}
    with zipfile.ZipFile(song_path) as z:
        for info in z.infolist():
            ctypes[info.filename] = info.compress_type
            files[info.filename]  = z.read(info.filename)

    # audiomixer.xml 수정
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

    # 재패키징
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
        self.geometry("1100x640")
        self.minsize(800, 450)
        self._results:      list[dict]       = []
        self._sort_reverse: dict[str, bool]  = {}
        self._song_path:    str | None       = None
        self._song_tracks:  dict             = {}   # label → {current_gain, wav_path}
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
        ttk.Button(top, text="Browse…", command=self._browse_folder).grid(
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

        # ── Row 2: 파라미터 ───────────────────────────────────────────────────
        ttk.Label(top, text="Gate:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._gate = tk.DoubleVar(value=DEFAULT_GATE_DB)
        ttk.Spinbox(top, from_=-70, to=-10, increment=1,
                    textvariable=self._gate, width=6).grid(
            row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(top, text="dBFS", foreground="gray").grid(
            row=2, column=1, sticky="e", pady=(6, 0))

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

        # ── Analyze / Write 버튼 ──────────────────────────────────────────────
        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=0, column=5, rowspan=3, padx=(16, 0), sticky="ns")
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
            ("track",  "S1 Track",        180, "w"),
            ("file",   "WAV File",         200, "w"),
            ("dur",    "Total (s)",          70, "center"),
            ("valid",  "Valid (s)",          70, "center"),
            ("level",  "Level (dBFS)",      100, "center"),
            ("offset", "Offset (dB)",       100, "center"),
            ("gain",   "New Gain",           90, "center"),
            ("status", "Status",            140, "center"),
        )
        self._tree = ttk.Treeview(
            tf, columns=[c[0] for c in COLS], show="headings", selectmode="browse")
        for col, head, w, anchor in COLS:
            self._tree.heading(col, text=head,
                               command=lambda c=col: self._sort(c))
            self._tree.column(col, width=w, anchor=anchor, minwidth=50)

        sb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.tag_configure("ok",      foreground="#1a6b1a")
        self._tree.tag_configure("warn",    foreground="#b84000")
        self._tree.tag_configure("none",    foreground="#888888")
        self._tree.tag_configure("err",     foreground="#cc0000")
        self._tree.tag_configure("nomatch", foreground="#555555")

        # ── Bottom ────────────────────────────────────────────────────────────
        bot = ttk.Frame(self, padding=(10, 4))
        bot.pack(fill="x")
        ttk.Button(bot, text="Export CSV", command=self._export).pack(side="left")
        ttk.Button(bot, text="Clear",      command=self._clear ).pack(side="left", padx=8)
        ttk.Label(bot, text="⚠ = clipping risk  |  Write to .song 은 Studio One 닫은 후 실행",
                  foreground="gray").pack(side="right")

    # ── Sort ──────────────────────────────────────────────────────────────────
    def _sort(self, col: str) -> None:
        items = [(self._tree.set(k, col), k) for k in self._tree.get_children()]
        rev   = self._sort_reverse.get(col, False)
        try:
            items.sort(key=lambda t: float(t[0].replace("+", "")), reverse=rev)
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

        # WAV 폴더를 첫 번째 매칭 파일에서 자동 설정
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
        # .song 모드: song에서 발견된 WAV 경로 사용
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
            # 폴더 모드
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

        # 매칭 안 된 트랙 표시
        for lbl in missing:
            self._tree.insert("", "end",
                values=(lbl, "— WAV not found —", "", "", "", "", "", ""),
                tags=("nomatch",))

        threading.Thread(
            target=self._worker, args=(files,), daemon=True
        ).start()

    def _worker(self, files: list[tuple]) -> None:
        gate   = self._gate.get()
        target = self._target.get()
        pct    = self._percentile.get()
        n      = len(files)

        for i, (label, fp) in enumerate(files):
            self.after(0, lambda i=i, n=n: self._stat.config(text=f"{i+1} / {n}"))
            result             = analyze_file(str(fp), gate, target, pct)
            result["label"]    = label          # S1 트랙 레이블 (None이면 폴더 모드)
            result["wav_path"] = str(fp)
            self._results.append(result)
            self.after(0, self._add_row, result)
            self.after(0, self._pct.set, (i + 1) / n * 100)

        self.after(0, self._done)

    def _done(self) -> None:
        self._run_btn.config(state="normal")
        n = len(self._results)
        self._stat.config(text=f"Done  ({n} files)")
        # .song 모드에서 유효 결과 있으면 Write 버튼 활성화
        if self._song_path and any(
            r.get("label") and r.get("status") not in ("No Signal",) and "error" not in r
            for r in self._results
        ):
            self._write_btn.config(state="normal")

    def _add_row(self, r: dict) -> None:
        label      = r.get("label") or "—"
        wav_name   = Path(r.get("wav_path", r.get("file", "?"))).name

        if "error" in r:
            self._tree.insert("", "end",
                values=(label, wav_name, "—", "—", "ERROR", "—", "—", r["error"]),
                tags=("err",))
            return

        level  = f"{r['level_db']:.1f}"  if r["level_db"] is not None else "N/A"
        offset = f"{r['offset_db']:+.1f}" if r["status"] != "No Signal" else "—"

        # new_gain: 현재 InputFX gain(dB)에 offset 합산한 값 표시
        if self._song_tracks and r.get("label") and r["status"] != "No Signal":
            cur_db   = self._song_tracks.get(r["label"], {}).get("current_inputfx_gain_db", 0.0)
            new_db   = cur_db + r["offset_db"]
            gain_str = f"{new_db:+.2f} dB"
        else:
            gain_str = "—"

        tag = ("warn" if r["warning"]
               else "none" if r["status"] == "No Signal"
               else "ok")

        self._tree.insert("", "end",
            values=(
                label,
                wav_name,
                f"{r['duration']:.1f}",
                f"{r['valid_duration']:.1f}",
                level,
                offset,
                gain_str,
                r["status"],
            ),
            tags=(tag,))

    # ── Write to .song ────────────────────────────────────────────────────────
    def _write_song(self) -> None:
        if not self._song_path:
            return

        # gain_map 구성
        gain_map: dict[str, float] = {}
        for r in self._results:
            if "error" in r or r["status"] == "No Signal":
                continue
            label = r.get("label")
            if not label:
                continue
            cur_db = self._song_tracks.get(label, {}).get("current_inputfx_gain_db", 0.0)
            gain_map[label] = cur_db + r["offset_db"]

        if not gain_map:
            messagebox.showinfo("없음", "적용할 트랙이 없습니다.")
            return

        # 미리보기
        lines = "\n".join(
            f"  {lbl:25s}  {g:+.2f} dB"
            for lbl, g in gain_map.items()
        )
        if not messagebox.askyesno(
            "확인",
            f"아래 게인을 .song 파일에 적용합니다:\n\n{lines}\n\n"
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
            w.writerow(["S1 Track", "WAV File", "Total (s)", "Valid (s)",
                        "Level (dBFS)", "Offset (dB)", "Status"])
            for r in self._results:
                label = r.get("label") or "—"
                wav   = Path(r.get("wav_path", r.get("file", "?"))).name
                if "error" in r:
                    w.writerow([label, wav, "—", "—", "ERROR", "—", r["error"]])
                else:
                    lv  = f"{r['level_db']:.2f}" if r["level_db"] is not None else "N/A"
                    off = f"{r['offset_db']:+.2f}" if r["status"] != "No Signal" else "0.00"
                    w.writerow([label, wav, f"{r['duration']:.2f}",
                                f"{r['valid_duration']:.2f}", lv, off, r["status"]])
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
