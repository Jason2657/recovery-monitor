"""Patient-facing LCD — the always-on status the patient actually sees.

Owner: Person C (Sensing & scoring).

The real device drives a small character LCD. Here we render to the console. Per
the architecture, the LCD is updated on BOTH paths every timestep — the patient
always gets a clear, calm status, whether or not the care team was notified.

TODO(sensing-owner): add a hardware backend (e.g. RPi + I2C 16x2/20x4 LCD) behind
the same ``update`` interface; keep messages short enough to fit the display.
"""

from __future__ import annotations


class LCD:
    """Console stand-in for the patient's status display."""

    def __init__(self, label: str = "PATIENT LCD") -> None:
        self.label = label
        #: Last status shown, handy for tests / the hardware backend.
        self.last_status: str = ""
        self.last_detail: str = ""

    def update(self, status: str, detail: str = "") -> None:
        """Show a short status line (and optional second line) to the patient."""
        self.last_status = status
        self.last_detail = detail
        line = f"  [{self.label}] {status}"
        if detail:
            line += f" — {detail}"
        print(line)

    # Convenience renderers for the two pipeline outcomes ------------------- #
    def show_ok(self, detail: str = "Keep moving — try a short walk.") -> None:
        """Benign / Skeptic-wins path: reassure and nudge gentle activity."""
        self.update("✅ All looking steady", detail)

    def show_notified(self, detail: str = "A nurse will check in with you.") -> None:
        """Risk-high path: tell the patient the care team has been looped in."""
        self.update("📞 Care team notified", detail)
