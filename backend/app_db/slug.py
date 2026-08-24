"""Organization slug helpers — URL-safe handles derived from a name."""

import re

from sqlalchemy.orm import Session


def slugify(name: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, trim. Never empty."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "org"


def unique_slug(db: Session, name: str) -> str:
    """A slugified name guaranteed unique against existing organizations."""
    from .models import Organization  # local import to avoid a cycle

    base = slugify(name)
    candidate = base
    i = 2
    while db.query(Organization).filter_by(slug=candidate).first() is not None:
        candidate = f"{base}-{i}"
        i += 1
    return candidate
