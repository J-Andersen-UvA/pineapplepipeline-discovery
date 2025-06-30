import tkinter as tk
from tkinter import ttk

def init_style(root):
    """
    Initialize ttk theme & fonts once on the root.
    """
    style = ttk.Style(root)
    # on Windows you can use 'vista' or 'xpnative'; fall back otherwise
    try:
        style.theme_use('vista')
    except tk.TclError:
        style.theme_use(style.theme_names()[0])
    # apply Segoe UI 10 to the common ttk widgets
    for widget in ('TLabel', 'TButton', 'TEntry', 'TSpinbox', 'TCombobox', 'TLabelframe.Label'):
        style.configure(widget, font=('Segoe UI', 10))
    # slightly tweak spacing on frames & buttons if you like:
    style.configure('TButton', padding=(6,4))
    style.configure('TLabelframe', padding=10)

class SectionFrame(ttk.LabelFrame):
    """A labelled box with groove border, padding & consistent ttk styling."""
    def __init__(self, parent, title, **kwargs):
        super().__init__(parent,
                         text=title,
                         relief='groove',
                         borderwidth=2,
                         **kwargs)

class DiscoveryUI(ttk.Frame):
    """Creates the four grouped sections you asked for."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, padding=10, **kwargs)
        self.columnconfigure(0, weight=1)

        # 1) Configured Devices
        self.configured_devices = SectionFrame(self, "Configured Devices")
        self.configured_devices.grid(row=0, column=0, sticky="ew", padx=5, pady=(5,2))

        # 2) Zeroconf Services
        self.zeroconf = SectionFrame(self, "Zeroconf Services")
        self.zeroconf.grid(row=1, column=0, sticky="ew", padx=5, pady=2)

        # 3) Last Messages
        self.last_messages = SectionFrame(self, "Last Messages")
        self.last_messages.grid(row=2, column=0, sticky="ew", padx=5, pady=2)

        # 4) Current Status
        self.current_status = SectionFrame(self, "Current Status")
        self.current_status.grid(row=3, column=0, sticky="ew", padx=5, pady=(2,5))

        # 5) Button area
        self.button_area = SectionFrame(self, "Actions")
        self.button_area.grid(row=4, column=0, sticky="ew", padx=5, pady=(2,5))

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Discovery Service UI")

    init_style(root)                # set up theme & fonts once

    ui = DiscoveryUI(root)
    ui.pack(fill="both", expand=True)

    # Example content:
    ttk.Label(ui.configured_devices, text="No devices configured yet.").pack(pady=10)
    ttk.Label(ui.zeroconf, text="No Zeroconf services discovered yet.").pack(pady=10)
    ttk.Label(ui.last_messages, text="No messages received yet.").pack(pady=10)
    ttk.Label(ui.current_status, text="Status: Idle").pack(pady=10)

    root.mainloop()
