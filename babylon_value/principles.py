"""The principles every rule in the strategy must trace back to.

Each gate, order and exit decision carries one of these labels, so the
decision journal can always answer "which principle made us do this?".
"""

from __future__ import annotations

from enum import Enum


class Principle(str, Enum):
    # Engineering principle that sits underneath everything else.
    INTEGRITY = "Failsafe: act only on verified, point-in-time data"

    # Deep value investing (Graham, Greenblatt, Carlisle).
    MARGIN_OF_SAFETY = "Deep value: buy only with a margin of safety"

    # The Five Laws of Gold - "The Richest Man in Babylon", G. S. Clason.
    LAW_1 = "Law 1: a part of all you earn is yours to keep"
    LAW_2 = "Law 2: gold labours for the owner who finds it profitable employment"
    LAW_3 = "Law 3: gold clings to the cautious owner who heeds the wise"
    LAW_4 = "Law 4: gold slips from the owner who invests in what he does not understand"
    LAW_5 = "Law 5: gold flees those who chase impossible earnings or trust tricksters"

    # The Seven Cures for a Lean Purse (Cure 1 is the same rule as Law 1).
    CURE_2 = "Cure 2: control thy expenditures"
    CURE_3 = "Cure 3: make thy gold multiply"
    CURE_4 = "Cure 4: guard thy treasures from loss"
    CURE_5 = "Cure 5: own productive, tangible property"
    CURE_6 = "Cure 6: insure a future income"
    CURE_7 = "Cure 7: increase thy ability to earn"

    ALL = "Every principle must agree"
