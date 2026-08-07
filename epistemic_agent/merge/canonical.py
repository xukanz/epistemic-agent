"""Canonical ID generation and label normalisation for a technology corpus.

Division of labour, deliberately:

* **This module** does *generic* normalisation — case, whitespace, separators,
  version suffixes, multi-value splitting. It knows nothing about which
  technologies exist.
* **The vocabulary shards** (``onto/shards/*.yaml``) do the *semantic* folding —
  ``portkey-ai`` → ``portkey``, ``postgres`` → ``postgresql``, ``k8s`` →
  ``kubernetes``. That is data, and it lives with the instance, not the code.

Mixing the two is the mistake to avoid: hard-coding a language- or
organisation-specific spelling rule into the generic layer makes it stop being
generic the moment a different organisation uses it. Nothing here hard-codes a
technology.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# ---------------------------------------------------------------------------
# Primitives


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Keeps ``+`` as ``plus`` and ``#`` as ``sharp`` so
    that C++ / C# survive; everything else non-alphanumeric becomes a dash.

    A label with no ASCII at all — common here, where descriptions are written
    in Chinese — would otherwise slugify to the empty string and collapse every
    such label onto one ID. Those fall back to a short digest of the original
    text: stable across runs, unique per label, and visibly not a name.
    """
    s = strip_accents(text).lower().strip()
    s = s.replace("++", "plus-plus").replace("#", "sharp")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:8]
        return f"x{digest}"
    return s


# ---------------------------------------------------------------------------
# Technology labels

# Tokens whose trailing digits are part of the name, not a version.
NO_VERSION_STRIP = {
    "s3", "ec2", "oauth2", "sila2", "a2a", "mv3", "chrome-mv3", "utf8",
    "log4j", "web3", "http2", "ipv6", "sha256", "md5", "gpt-4o", "d3",
    "boto3", "pydoe2", "camelot-py", "docx2txt", "nlmixr2", "sqlite3",
}

# " 4.4.1", " 22+", " v6" — a version written as a separate token.
_TRAILING_VERSION = re.compile(r"[\s_-]+v?\d+(?:\.\d+)*\+?$")
# "react18", "svelte5", "spring-boot3" — a version glued to the stem.
# The stem minimum is 5 characters: at 4 it strips `boto3` → `boto`, and short
# names ending in a digit are far more often part of the name than a version.
_GLUED_VERSION = re.compile(r"^(?P<stem>[a-z][a-z.\-]{4,}?)[\s-]?(?P<ver>\d+(?:\.\d+)*)\+?$")

# Separators that indicate one label is carrying several values. The `+` case
# needs a guard so that C++ survives.
MULTI_VALUE_SPLIT = re.compile(r"\s*[/|、]\s*|\s*(?<!\+)\+(?!\+)\s*")

# A trailing parenthetical qualifies the thing, it is not part of its name:
# "AWS CDK(Python)" → "AWS CDK", "torch(CPU)" → "torch", "openai(npm)" → "openai".
_PARENTHETICAL = re.compile(r"\s*[（(\[].*?[)）\]]\s*$")

# Labels that are not technologies at all — extraction noise from the source
# corpus. Matched after normalisation.
NOISE_PATTERNS = [
    re.compile(r"^(无|none|n/?a|证据不足|unknown|tbd)$", re.I),
    re.compile(r"^[.\w-]*\.(py|md|json|ya?ml|ini|txt|csv)$", re.I),  # generate_schema.py
    re.compile(r"脚本$"),                                            # bash 脚本 / shell 脚本
    re.compile(r"^(markdown|python|java|git|shell|bash|yaml|json schema)$", re.I),
]


def normalise_tech_label(raw: str) -> str:
    """Conservative normalisation of a free-text technology label.

    >>> normalise_tech_label("LangGraph")
    'langgraph'
    >>> normalise_tech_label("Next.js 14")
    'next.js'
    >>> normalise_tech_label("react18")
    'react'
    >>> normalise_tech_label("AWS  Bedrock")
    'aws bedrock'
    >>> normalise_tech_label("S3")
    's3'
    """
    s = strip_accents(raw).strip().lower()
    s = _PARENTHETICAL.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("_", "-")
    if s in NO_VERSION_STRIP:
        return s

    stripped = _TRAILING_VERSION.sub("", s)
    if stripped and stripped not in NO_VERSION_STRIP:
        s = stripped

    if s not in NO_VERSION_STRIP:
        m = _GLUED_VERSION.match(s)
        if m and m.group("stem") not in NO_VERSION_STRIP:
            s = m.group("stem")

    return s.strip(" -")


def canonical_tech_id(raw: str) -> str:
    return f"tech-{slugify(normalise_tech_label(raw))}"


def canonical_capability_id(raw: str) -> str:
    return f"cap-{slugify(raw)}"


def canonical_pattern_id(raw: str) -> str:
    return f"pattern-{slugify(raw)}"


def canonical_system_id(raw: str) -> str:
    return f"sys-{slugify(normalise_tech_label(raw))}"


def canonical_domain_id(raw: str) -> str:
    return f"domain-{slugify(raw)}"


def split_multi_value(raw: str) -> list[str]:
    """Split 'anthropic/openai' → ['anthropic', 'openai'].

    Returns a single-element list when there is nothing to split. Paths and
    scoped npm packages are left alone — ``@modelcontextprotocol/sdk`` and
    ``nf-core/Slurm`` are one thing each, not two.
    """
    s = raw.strip()
    if s.startswith("@") or s.startswith("http"):
        return [s]
    # Separators inside a parenthetical belong to the qualifier, not to the
    # name: "R(dplyr/ggplot2)" is R, not R-and-ggplot2.
    s = _PARENTHETICAL.sub("", s).strip()
    if not s:
        return [raw.strip()]
    parts = [p.strip(" -") for p in MULTI_VALUE_SPLIT.split(s) if p.strip(" -")]
    if len(parts) <= 1:
        return [s]
    # A split that produces a one-character fragment is almost always wrong.
    if any(len(p) < 2 for p in parts):
        return [s]
    return parts


def is_noise(raw: str) -> bool:
    s = normalise_tech_label(raw)
    if not s or len(s) < 2:
        return True
    return any(p.search(s) for p in NOISE_PATTERNS)


# ---------------------------------------------------------------------------
# Repos, teams, people

# Repo IDs must stay injective over paths, so unlike `slugify` this keeps `_`
# and `.` distinct from `-`. A slug that folds both onto `-` would collide two
# real, distinct repos differing only in that one character — e.g.
# `team/data_pipeline` and `team/data-pipeline`.
_PATH_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def canonical_repo_id(path_with_namespace: str) -> str:
    """A repo's full path is its natural key — globally unique by
    construction, so unlike a person's name it never needs fuzzy resolution.

    Two repos named ``widget-service`` under different namespaces are two
    repos, and two repos differing only in `_` vs `-` are also two repos. A
    merge strategy that collapsed either case on a shared key would hide the
    collision instead of surfacing it — see `NaturalKeyGuard`, which reports
    instead of merging — and this function does not create the collision in
    the first place.
    """
    s = strip_accents(path_with_namespace).lower().strip().strip("/")
    s = s.replace("/", "--")
    return "repo-" + _PATH_UNSAFE.sub("-", s).strip("-")


def team_namespace(path_with_namespace: str) -> str:
    """Top-level namespace of a repo path — the owning team or personal space."""
    return path_with_namespace.strip("/").split("/", 1)[0]


def canonical_team_id(namespace: str) -> str:
    return f"team-{slugify(namespace)}"


_TITLES = re.compile(r"\b(prof|dr|pd|hc|mult|md|phd)\.?\b", re.I)


def canonical_person_id(name: str) -> str:
    """`person-<first>-<last>`, handling 'Surname, Firstname'."""
    s = strip_accents(name).strip()
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    s = _TITLES.sub(" ", s)
    return f"person-{slugify(s)}"


def name_parts(raw: str) -> tuple[str, str]:
    """Return (first, last) for 'Surname, Firstname' or 'Firstname Surname'."""
    s = strip_accents(raw).lower().strip()
    s = _TITLES.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if "," in s:
        last, _, first = s.partition(",")
        return first.strip(), last.strip().replace(" ", "-")
    parts = s.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]).strip(), parts[-1].strip()
    return "", s


def first_initial(first: str) -> str:
    first = first.strip()
    return first[0].lower() if first else ""
