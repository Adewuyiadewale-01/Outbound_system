"""Activity-tab policy: tab orders, allowed keys, disallowed destination paths.

Extracted verbatim from helpers/linkedin_helper.py during the linkedin_helper
carve (see docs/CARVE-LINKEDIN-HELPER-PLAN.md, slice S9). Pure move.
"""

import re

ACTIVITY_TAB_ORDER = [
    ("all", "all"),
    ("comments", "Comments"),
    ("reactions", "Reactions"),
]


ACTIVITY_RANKING_TAB_ORDER = [
    ("posts", "Posts"),
    ("reactions", "Reactions"),
    ("comments", "Comments"),
]


ACTIVITY_SELECTOR_RANKING_TAB_ORDER = [
    ("posts", "Posts"),
    ("reactions", "Reactions"),
    ("comments", "Comments"),
]


ACTIVITY_ALLOWED_TAB_KEYS = {"all", "posts", "comments", "reactions"}


ACTIVITY_DISALLOWED_PATH_RE = re.compile(
    r"/recent-activity/(articles|videos|images|documents)/?",
    re.IGNORECASE,
)
