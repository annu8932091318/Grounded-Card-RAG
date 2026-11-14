"""Known conflicts between corpus documents and how they are resolved (defects D1, D5)."""

CONFLICTS = [
    {
        "id": "D1",
        "docs": ["POL-CRD-001", "POL-CRD-011"],
        "topic": "minimum gap between limit increases",
        "prevails": "POL-CRD-001",
        "prevailing_section": "3",
        "resolution": (
            "POL-CRD-001 section 3 (6 months) prevails. POL-CRD-011 states 3 months, "
            "but its own note says POL-CRD-001 prevails until it is revised. Apply 6 months."
        ),
    },
    {
        "id": "D5",
        "docs": ["POL-CRD-001", "POL-CRD-003"],
        "topic": "thin-file customers and the 6 statement cycle eligibility rule",
        "prevails": "POL-CRD-003",
        "prevailing_section": "5",
        "applies_when": "thin_file",  # only a conflict for customers with < 6 months history
        "resolution": (
            "Specific beats general: thin-file customers follow POL-CRD-003 section 5 "
            "(remain eligible, increase capped at 1.25x, must be referred to a credit officer). "
            "Never promise an approval."
        ),
    },
]
