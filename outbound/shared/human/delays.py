"""Randomized timing primitives (pure functions, zero internal deps).

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S1). Pure move — no
behavior change.
"""

import math
import random
import time


def human_delay(min_s: float, max_s: float, distribution: str = "uniform") -> float:
    """Sleep for a randomized duration. Returns the actual delay used."""
    if distribution == "gaussian":
        mean = (min_s + max_s) / 2
        std = (max_s - min_s) / 6  # 99.7% within range
        delay = max(min_s, min(max_s, random.gauss(mean, std)))
    elif distribution == "log_normal":
        # Skews toward shorter delays with occasional longer ones
        mean = math.log((min_s + max_s) / 2)
        std = 0.5
        delay = max(min_s, min(max_s, random.lognormvariate(mean, std)))
    else:  # uniform
        delay = random.uniform(min_s, max_s)
    time.sleep(delay)
    return delay


def typing_delay():
    """Delay between keystrokes for natural typing."""
    # Occasional longer pauses (thinking mid-word)
    if random.random() < 0.08:
        time.sleep(random.uniform(0.3, 0.8))
    else:
        time.sleep(random.uniform(0.05, 0.20))
