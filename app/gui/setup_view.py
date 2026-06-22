"""Setup screen: file/folder pickers, start date, OCR cache toggle, and the
approved-users allow-list.

Mirrors progress_view.py/review_view.py: this frame owns its own widgets and
backing tk.Variables, and exposes plain getter methods (get_html_path() etc.)
for the values App needs to start a run, rather than App reaching into this
screen's internals directly.
"""

import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable, Optional

from .. import config, state
from . import theme


class SetupFrame(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        on_start: Callable[[], None],
        initial_image_folder: Optional[str] = None,
    ):
        super().__init__(master)

        def _most_recent(field: str) -> str:
            recent = state.load_recent_paths(field)
            return recent[0] if recent else ""

        self._html_path = tk.StringVar(value=_most_recent("html_path"))
        self._image_folder = tk.StringVar(value=initial_image_folder or _most_recent("image_folder"))
        self._output_path = tk.StringVar(value=_most_recent("output_path"))
        self._start_date = tk.StringVar(value=state.read_last_run_date() or "")
        self._use_cache = tk.BooleanVar(value=True)

        approved_users_state = state.read_approved_users_state()
        if approved_users_state is None:
            initial_approved_users_text = "\n".join(config.DEFAULT_APPROVED_USERS)
            self._use_all_users = tk.BooleanVar(value=False)
        else:
            initial_approved_users_text = approved_users_state["text"]
            self._use_all_users = tk.BooleanVar(value=approved_users_state["use_all_users"])
        self._known_user_pick = tk.StringVar()

        ttk.Label(self, text="Chatlog HTML file:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            self, textvariable=self._html_path, width=58,
            values=state.load_recent_paths("html_path"),
        ).grid(row=0, column=1, pady=4)
        ttk.Button(self, text="Browse...", command=self._pick_html).grid(row=0, column=2, padx=4)

        ttk.Label(self, text="Image folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            self, textvariable=self._image_folder, width=58,
            values=state.load_recent_paths("image_folder"),
        ).grid(row=1, column=1, pady=4)
        ttk.Button(self, text="Browse...", command=self._pick_image_folder).grid(row=1, column=2, padx=4)

        ttk.Label(self, text="Output .txt file:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            self, textvariable=self._output_path, width=58,
            values=state.load_recent_paths("output_path"),
        ).grid(row=2, column=1, pady=4)
        ttk.Button(self, text="Browse...", command=self._pick_output).grid(row=2, column=2, padx=4)

        ttk.Label(self, text="Start date (YYYY-MM-DD):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self._start_date, width=30).grid(row=3, column=1, sticky="w", pady=4)

        self._cache_check = ttk.Checkbutton(
            self, text="Use cached OCR data for this image folder", variable=self._use_cache
        )
        self._cache_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Checkbutton(
            self, text="Transcribe messages from all users", variable=self._use_all_users,
            command=self._update_approved_users_enabled,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=4)

        approved_frame = ttk.Frame(self)
        approved_frame.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Label(
            approved_frame, text='Approved users (one per line, e.g. "123456789 - Alice"):'
        ).pack(anchor="w")

        text_container = ttk.Frame(approved_frame)
        text_container.pack(anchor="w")
        self._approved_users_text = tk.Text(
            text_container, width=60, height=5, wrap="none", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE - 2),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
        )
        self._approved_users_text.insert("1.0", initial_approved_users_text)
        self._approved_users_text.pack(side="left")
        approved_scroll = ttk.Scrollbar(
            text_container, orient="vertical", command=self._approved_users_text.yview
        )
        approved_scroll.pack(side="left", fill="y")
        self._approved_users_text.configure(yscrollcommand=approved_scroll.set)

        known_users_frame = ttk.Frame(approved_frame)
        known_users_frame.pack(anchor="w", pady=4)
        ttk.Label(known_users_frame, text="Known users:").pack(side="left")
        self._known_user_combo = ttk.Combobox(
            known_users_frame, textvariable=self._known_user_pick, width=40,
            values=state.load_recent_paths("approved_user"),
        )
        self._known_user_combo.pack(side="left", padx=4)
        self._add_known_user_button = ttk.Button(
            known_users_frame, text="Add", command=self._add_known_user
        )
        self._add_known_user_button.pack(side="left")

        self._update_approved_users_enabled()

        self._error_label = ttk.Label(self, text="", foreground="red")
        self._error_label.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Button(self, text="Start", command=on_start).grid(row=8, column=1, pady=12)

    # -- pickers ----------------------------------------------------------

    def _pick_html(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("HTML files", "*.html"), ("All files", "*.*")])
        if path:
            self._html_path.set(path)

    def _pick_image_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self._image_folder.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self._output_path.set(path)

    def _add_known_user(self) -> None:
        value = self._known_user_pick.get().strip()
        if not value:
            return
        self._approved_users_text.insert("end", value + "\n")

    def _update_approved_users_enabled(self) -> None:
        """The approved-users list and "known users" picker are irrelevant
        once "all users" is checked, so grey them out rather than leaving
        them interactive but ignored."""
        if self._use_all_users.get():
            self._approved_users_text.configure(state="disabled")
            self._known_user_combo.state(["disabled"])
            self._add_known_user_button.state(["disabled"])
        else:
            self._approved_users_text.configure(state="normal")
            self._known_user_combo.state(["!disabled"])
            self._add_known_user_button.state(["!disabled"])

    # -- accessors for App -------------------------------------------------

    def get_html_path(self) -> str:
        return self._html_path.get().strip()

    def get_image_folder(self) -> str:
        return self._image_folder.get().strip()

    def get_output_path(self) -> str:
        return self._output_path.get().strip()

    def get_start_date(self) -> str:
        return self._start_date.get()

    def get_use_cache(self) -> bool:
        return self._use_cache.get()

    def get_use_all_users(self) -> bool:
        return self._use_all_users.get()

    def get_approved_users_text(self) -> str:
        return self._approved_users_text.get("1.0", "end-1c")

    def set_error(self, message: str) -> None:
        self._error_label.config(text=message)
