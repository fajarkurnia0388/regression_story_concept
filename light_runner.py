#!/usr/bin/env python3
"""
light_runner_with_settings.py

- On first run (or with --configure) opens a small Tk GUI to choose target MP3.
- Saves config to a per-user config file (JSON).
- Plays the configured MP3 on startup, then runs an ultra-low-CPU heartbeat loop.
- Handles Ctrl+C / SIGTERM to stop audio and exit cleanly.

Usage:
  python light_runner_with_settings.py [--interval 2.0] [--message "heartbeat"] [--silent] [--count N] [--configure]

If --mp3 PATH is provided, it overrides config and is saved.
"""
from __future__ import annotations
import argparse
import json
import platform
import signal
import shutil
import sys
import time
from threading import Event, Thread
from datetime import datetime
from pathlib import Path
import subprocess

# GUI
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except Exception:
    tk = None  # we'll check later

stop_event = Event()
heartbeat_event = Event()


def sigterm_handler(signum, frame):
    stop_event.set()
    heartbeat_event.set()


def alarm_handler(signum, frame):
    heartbeat_event.set()


# ---------------------------
# Config helpers
# ---------------------------
def get_config_path() -> Path:
    """
    Return path to per-user config file for this tool.
    Windows: %APPDATA%\\light_runner\\config.json
    POSIX: $XDG_CONFIG_HOME/light_runner/config.json or ~/.config/light_runner/config.json
    """
    name = "light_runner"
    filename = "config.json"
    try:
        if platform.system() == "Windows":
            appdata = Path(os_getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
            cfg_dir = appdata / name
        else:
            xdg = Path(os_getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
            cfg_dir = xdg / name
        cfg_dir.mkdir(parents=True, exist_ok=True)
        return cfg_dir / filename
    except Exception as e:
        print(f"[!] Error creating config path: {e}")
        return Path.home() / ".light_runner_config.json"  # Fallback


def os_getenv(key: str):
    import os

    return os.environ.get(key)


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                print("[!] Config file corrupted, using empty config.")
                return {}
            return data
    except json.JSONDecodeError as e:
        print(f"[!] JSON decode error in config: {e}, using empty config.")
        return {}
    except Exception as e:
        print(f"[!] Error loading config: {e}, using empty config.")
        return {}


def save_config(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------------------
# Audio player (same abstraction)
# ---------------------------
class AudioPlayer:
    def __init__(self, path: str, loop: bool = False):
        from pathlib import Path

        self.path = str(path)
        self.loop = bool(loop)
        self.backend = None
        self.proc = None
        self._thread = None
        self._use_pygame = False
        self.pygame = None
        # try pygame
        try:
            import pygame

            pygame.mixer.init()
            self.pygame = pygame
            self._use_pygame = True
            self.backend = "pygame"
        except Exception:
            self.pygame = None
        if not self._use_pygame:
            for cmd in ("mpg123", "mpv", "ffplay", "afplay"):
                if shutil.which(cmd):
                    self.backend = cmd
                    break
            if self.backend is None:
                if platform.system() == "Windows":
                    self.backend = "start"
                else:
                    if shutil.which("xdg-open"):
                        self.backend = "xdg-open"
                    elif shutil.which("open"):
                        self.backend = "open"
                    else:
                        self.backend = None

    def play(self):
        from pathlib import Path

        p = Path(self.path)
        if not p.is_file():
            print(f"[!] Audio file not found: {self.path}")
            return False

        if self._use_pygame and self.pygame:
            try:
                self.pygame.mixer.music.load(self.path)
                # loop: -1 means forever, 0 means play once
                loop_flag = -1 if self.loop else 0
                self.pygame.mixer.music.play(loops=loop_flag)
                print("[*] Playing via pygame.mixer (in-process).")
                return True
            except Exception as e:
                print(f"[!] pygame playback failed: {e}")

        if self.backend in ("mpg123", "mpv", "ffplay", "afplay"):
            try:
                if self.backend == "ffplay":
                    args = [
                        self.backend,
                        "-nodisp",
                        "-autoexit",
                        "-loglevel",
                        "quiet",
                        self.path,
                    ]
                    if self.loop:
                        args = [
                            self.backend,
                            "-nodisp",
                            "-loop",
                            "0",
                            "-loglevel",
                            "quiet",
                            self.path,
                        ]
                elif self.backend == "mpv":
                    args = [self.backend, "--no-terminal", "--really-quiet"]
                    if self.loop:
                        args += ["--loop-file=inf"]
                    args += [self.path]
                else:  # mpg123, afplay
                    args = [self.backend, self.path]
                    if self.loop and self.backend == "mpg123":
                        args = [
                            self.backend,
                            "-z",
                            "-q",
                            self.path,
                        ]  # mpg123 has -z (shuffle) but no simple loop; leaving as best-effort
                self.proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                print(f"[*] Playing via {self.backend} (pid={self.proc.pid})")
                return True
            except Exception as e:
                print(f"[!] {self.backend} playback failed: {e}")

        if self.backend in ("xdg-open", "open", "start"):
            try:
                if self.backend == "start":
                    self.proc = subprocess.Popen(f'start "" "{self.path}"', shell=True)
                else:
                    self.proc = subprocess.Popen(
                        [self.backend, self.path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                print(f"[*] Opened with {self.backend} (external app).")
                return True
            except Exception as e:
                print(f"[!] fallback open failed: {e}")

        # playsound fallback
        try:
            from playsound import playsound

            def _ps():
                try:
                    if self.loop:
                        # playsound has no loop; naive loop implementation
                        while not stop_event.is_set():
                            playsound(self.path)
                    else:
                        playsound(self.path)
                except Exception:
                    pass

            self._thread = Thread(target=_ps, daemon=True)
            self._thread.start()
            self.backend = "playsound-thread"
            print("[*] Playing via playsound in background thread.")
            return True
        except Exception:
            pass

        print("[!] No audio backend available.")
        return False

    def stop(self):
        stopped = False
        if self._use_pygame and self.pygame:
            try:
                self.pygame.mixer.music.stop()
                try:
                    self.pygame.mixer.quit()
                except Exception:
                    pass
                stopped = True
                print("[*] Stopped pygame playback.")
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
                stopped = True
                print("[*] Terminated external player process.")
            except Exception:
                try:
                    self.proc.kill()
                    stopped = True
                except Exception:
                    pass
        if not stopped and self._thread and self._thread.is_alive():
            print(
                "[*] Playback thread cannot be reliably stopped programmatically; it will finish naturally."
            )
            stopped = True
        return stopped


# ---------------------------
# GUI for selecting MP3
# ---------------------------
def open_config_gui(initial_path: str | None = None) -> dict | None:
    """
    Open tkinter GUI to choose MP3. Returns config dict or None if cancelled.
    """
    if tk is None:
        print("[!] tkinter not available; cannot open GUI.")
        return None

    root = tk.Tk()
    root.title("Light Runner — Select MP3")
    root.geometry("520x150")
    root.resizable(False, False)

    # Data holder
    selected = {"path": initial_path or "", "loop": False}

    def browse():
        p = filedialog.askopenfilename(
            title="Select MP3 file",
            filetypes=[
                ("Audio files", "*.mp3;*.wav;*.ogg;*.flac"),
                ("All files", "*.*"),
            ],
        )
        if p:
            entry_var.set(p)

    def test_play():
        path = entry_var.get().strip()
        if not path:
            messagebox.showwarning("No file", "Please choose a file first.")
            return
        ap = AudioPlayer(path, loop=False)
        ok = ap.play()
        if not ok:
            messagebox.showerror(
                "Play failed", "Unable to play selected file (no backend)."
            )
            return
        # stop after 3 seconds
        root.after(3000, lambda: ap.stop())

    def do_save():
        p = entry_var.get().strip()
        if not p:
            messagebox.showwarning("Missing", "Path cannot be empty.")
            return
        # basic validation: file exists
        from pathlib import Path

        if not Path(p).is_file():
            if not messagebox.askyesno(
                "File not found", "File does not exist. Save anyway?"
            ):
                return
        selected["path"] = p
        selected["loop"] = bool(loop_var.get())
        root.destroy()

    def do_cancel():
        # set to None sentinel by clearing path
        selected["path"] = selected.get("path", "")
        root.destroy()

    # Widgets
    frame = tk.Frame(root, padx=10, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="MP3 / Audio file to play on startup:").grid(
        row=0, column=0, columnspan=3, sticky="w"
    )

    entry_var = tk.StringVar(value=initial_path or "")
    entry = tk.Entry(frame, textvariable=entry_var, width=56)
    entry.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 6))

    btn_browse = tk.Button(frame, text="Browse...", command=browse, width=10)
    btn_browse.grid(row=1, column=2, padx=(6, 0))

    loop_var = tk.IntVar(value=0)
    chk_loop = tk.Checkbutton(
        frame, text="Loop playback until program stops", variable=loop_var
    )
    chk_loop.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 4))

    btn_test = tk.Button(frame, text="Test Play (3s)", command=test_play, width=12)
    btn_test.grid(row=3, column=0, pady=(6, 0), sticky="w")

    btn_save = tk.Button(frame, text="Save", command=do_save, width=10)
    btn_save.grid(row=3, column=1, pady=(6, 0))

    btn_cancel = tk.Button(frame, text="Cancel", command=do_cancel, width=10)
    btn_cancel.grid(row=3, column=2, pady=(6, 0))

    # Make Enter key do save
    root.bind("<Return>", lambda e: do_save())
    root.bind("<Escape>", lambda e: do_cancel())

    root.mainloop()

    if selected.get("path"):
        return {"mp3": selected["path"], "loop": bool(selected["loop"])}
    return None


# ---------------------------
# Main program
# ---------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Light runner with settings GUI for MP3 target."
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=2.0,
        help="Interval between heartbeats (sec).",
    )
    parser.add_argument(
        "-m", "--message", type=str, default="heartbeat", help="Message each iteration."
    )
    parser.add_argument(
        "--silent", action="store_true", help="Do not print heartbeat messages."
    )
    parser.add_argument(
        "--count", type=int, default=0, help="Stop after N heartbeats (0 = forever)."
    )
    parser.add_argument(
        "--mp3",
        type=str,
        default=None,
        help="Path to MP3 (overrides config and will be saved).",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Open GUI to configure MP3 target and exit (or continue).",
    )
    args = parser.parse_args()

    # signal handlers
    signal.signal(signal.SIGINT, sigterm_handler)
    try:
        signal.signal(signal.SIGTERM, sigterm_handler)
    except Exception:
        pass

    cfg_path = get_config_path()
    cfg = load_config(cfg_path)

    # If --mp3 passed, override and save
    if args.mp3:
        cfg["mp3"] = args.mp3
        # keep existing loop flag if present
        cfg.setdefault("loop", False)
        try:
            save_config(cfg_path, cfg)
            print(f"[*] Saved MP3 to config: {cfg_path}")
        except Exception as e:
            print(f"[!] Failed to save config: {e}")

    # If no mp3 in config or user asked to configure -> open GUI
    if args.configure or not cfg.get("mp3"):
        gui_result = open_config_gui(initial_path=cfg.get("mp3"))
        if gui_result:
            cfg.update(gui_result)
            try:
                save_config(cfg_path, cfg)
                print(f"[*] Config saved to {cfg_path}")
            except Exception as e:
                print(f"[!] Failed to save config: {e}")
        else:
            # GUI cancelled. If still no mp3 configured, exit.
            if not cfg.get("mp3"):
                print("[*] No MP3 configured. Exiting.")
                return
            else:
                print("[*] Using existing configuration.")

    mp3_path = cfg.get("mp3")
    loop_flag = cfg.get("loop", False)

    player = None
    if mp3_path:
        player = AudioPlayer(mp3_path, loop=loop_flag)
        ok = player.play()
        if not ok:
            print(
                "[!] Audio playback failed (no available backend). Continuing without audio."
            )

    # Choose efficient waiting mode
    is_posix = (platform.system() != "Windows") and hasattr(signal, "setitimer")
    if is_posix:
        print("[*] Mode: POSIX timer + pause (ultra-hemat)")
    else:
        print("[*] Mode: Fallback Event.wait (hemat)")

    if not args.silent:
        print(
            f"[*] Interval: {args.interval}s | Message: {args.message} | Silent: {args.silent}"
        )
        if args.count > 0:
            print(f"[*] Will stop after {args.count} heartbeats.")

    heartbeat_count = 0
    try:
        if is_posix:
            signal.signal(signal.SIGALRM, alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, args.interval, args.interval)
            while not stop_event.is_set():
                signal.pause()
                if stop_event.is_set():
                    break
                if heartbeat_event.is_set():
                    heartbeat_event.clear()
                    heartbeat_count += 1
                    if not args.silent:
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{now}] {args.message} ({heartbeat_count})")
                    if args.count > 0 and heartbeat_count >= args.count:
                        break
            signal.setitimer(signal.ITIMER_REAL, 0)
        else:
            while not stop_event.is_set():
                heartbeat_event.wait(args.interval)
                heartbeat_event.clear()
                if stop_event.is_set():
                    break
                heartbeat_count += 1
                if not args.silent:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{now}] {args.message} ({heartbeat_count})")
                if args.count > 0 and heartbeat_count >= args.count:
                    break
    except Exception as e:
        print(f"[!] Error: {e}", file=sys.stderr)
    finally:
        print("[*] Terminating. Stopping audio if possible...")
        if player:
            player.stop()
        print("[*] Bye!")


if __name__ == "__main__":
    main()
