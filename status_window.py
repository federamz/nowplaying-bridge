"""
Status window for NowPlaying Bridge.

Tkinter only (Python standard library), so the packaged exe stays small and the
build needs no extra wheels. Styled to match the Now Playing OS overlay: dark
surface, one accent, no chrome it doesn't need.

The window is passive — it shows what the bridge sees and lets you copy the URL.
Closing it quits the bridge, which is what a streamer expects from a window.
"""

import tkinter as tk
import webbrowser

BG = "#0e0e12"
CARD = "#16161c"
LINE = "#26262e"
TEXT = "#f2f2f5"
MUTED = "#8b8b97"
GREEN = "#2ecc71"
AMBER = "#ffd479"
RED = "#ff6b6b"


class StatusWindow:
    def __init__(self, app_name, version, url, get_state, on_quit):
        self.get_state = get_state
        self.on_quit = on_quit
        self.url = url

        self.root = tk.Tk()
        self.root.title(app_name)
        self.root.configure(bg=BG)
        self.root.geometry("420x290")
        self.root.minsize(420, 290)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._set_window_icon()

        wrap = tk.Frame(self.root, bg=BG, padx=22, pady=20)
        wrap.pack(fill="both", expand=True)

        header = tk.Frame(wrap, bg=BG)
        header.pack(fill="x")
        tk.Label(
            header, text=app_name, bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 13), anchor="w",
        ).pack(side="left")
        tk.Label(
            header, text=f"v{version}", bg=BG, fg=MUTED,
            font=("Consolas", 9), anchor="e",
        ).pack(side="right", pady=(4, 0))

        # --- status line -------------------------------------------------
        status = tk.Frame(wrap, bg=BG)
        status.pack(fill="x", pady=(14, 0))
        self.dot = tk.Canvas(
            status, width=10, height=10, bg=BG, highlightthickness=0, bd=0
        )
        self.dot.pack(side="left", pady=(4, 0))
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        self.status_text = tk.Label(
            status, text="Starting…", bg=BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )
        self.status_text.pack(side="left", padx=(9, 0))

        # --- now playing card -------------------------------------------
        card = tk.Frame(
            wrap, bg=CARD, highlightbackground=LINE, highlightthickness=1,
            padx=16, pady=14,
        )
        card.pack(fill="x", pady=(14, 0))
        self.title_lbl = tk.Label(
            card, text="Nothing playing", bg=CARD, fg=TEXT,
            font=("Segoe UI Semibold", 11), anchor="w", justify="left",
            wraplength=340,
        )
        self.title_lbl.pack(fill="x")
        self.artist_lbl = tk.Label(
            card, text="Press play in any music app", bg=CARD, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=340,
        )
        self.artist_lbl.pack(fill="x", pady=(3, 0))
        self.app_lbl = tk.Label(
            card, text="", bg=CARD, fg=MUTED, font=("Consolas", 8),
            anchor="w", justify="left",
        )
        self.app_lbl.pack(fill="x", pady=(8, 0))

        # --- url ---------------------------------------------------------
        tk.Label(
            wrap, text="WIDGET READS FROM", bg=BG, fg=MUTED,
            font=("Segoe UI", 7), anchor="w",
        ).pack(fill="x", pady=(16, 4))
        url_row = tk.Frame(wrap, bg=BG)
        url_row.pack(fill="x")
        url_lbl = tk.Label(
            url_row, text=url, bg=BG, fg=AMBER, font=("Consolas", 9),
            anchor="w", cursor="hand2",
        )
        url_lbl.pack(side="left")
        url_lbl.bind("<Button-1>", lambda _e: webbrowser.open(url))
        self.copy_btn = tk.Label(
            url_row, text="copy", bg=BG, fg=MUTED, font=("Segoe UI", 8),
            cursor="hand2",
        )
        self.copy_btn.pack(side="left", padx=(10, 0))
        self.copy_btn.bind("<Button-1>", self.copy_url)

        tk.Label(
            wrap,
            text="Leave this window open while you stream.\nClosing it stops the bridge.",
            bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w", justify="left",
        ).pack(fill="x", pady=(18, 0))

        self.refresh()

    def _set_window_icon(self):
        """Use the app icon in the title bar and taskbar when it was bundled."""
        import os
        import sys

        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "nowplaying-bridge.ico")
        if os.path.exists(path):
            try:
                self.root.iconbitmap(path)
            except Exception:
                pass  # a missing or odd icon must never stop the window opening

    def copy_url(self, _event=None):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.copy_btn.configure(text="copied", fg=GREEN)
        self.root.after(1400, lambda: self.copy_btn.configure(text="copy", fg=MUTED))

    def _set_dot(self, color):
        self.dot.itemconfigure(self._dot_id, fill=color)

    def refresh(self):
        state = self.get_state()
        session = state.get("current")
        error = state.get("error")

        if error:
            self._set_dot(RED)
            self.status_text.configure(text="Cannot read Windows media info", fg=RED)
            self.title_lbl.configure(text="Nothing playing")
            self.artist_lbl.configure(text=str(error)[:120])
            self.app_lbl.configure(text="")
        elif session and session.get("title"):
            playing = session.get("is_playing")
            self._set_dot(GREEN if playing else AMBER)
            self.status_text.configure(
                text="Playing" if playing else "Paused", fg=TEXT if playing else AMBER
            )
            self.title_lbl.configure(text=session["title"])
            self.artist_lbl.configure(text=session.get("artist") or "—")
            self.app_lbl.configure(
                text=(session.get("source_name") or session.get("source") or "").upper()
            )
        else:
            self._set_dot(AMBER)
            self.status_text.configure(text="Waiting for music", fg=MUTED)
            self.title_lbl.configure(text="Nothing playing")
            self.artist_lbl.configure(text="Press play in any music app")
            self.app_lbl.configure(text="")

        self.root.after(500, self.refresh)

    def quit(self):
        try:
            self.on_quit()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()
