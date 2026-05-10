"""Drug-name normalisation used for matching PSD-side names to PBS Schedule
entries. The two sources don't agree on form: PSDs often write
"abiraterone acetate" or "fluticasone propionate", PBS Schedule lists
"abiraterone" / "fluticasone". And PSDs sometimes carry multi-drug rows
like "abiraterone, enzalutamide" when the submission is a comparison.

Rather than mutating either source, we expose a small set of helpers and
candidate-key generators that consumers can iterate when looking up an ATC
code.
"""

from __future__ import annotations

import re

# Common salt / ester / hydrate suffixes we strip when looking up the base
# substance. These are pharmacologically-active in the same way as the parent
# substance and almost always share an ATC code.
_SALT_SUFFIXES = (
    "acetate", "hydrochloride", "sulfate", "sulphate", "dihydrate",
    "monohydrate", "trihydrate", "fumarate", "tartrate", "maleate",
    "succinate", "phosphate", "citrate", "mesilate", "mesylate",
    "tosilate", "tosylate", "besilate", "besylate", "stearate",
    "lactate", "carbonate", "bicarbonate", "potassium", "sodium",
    "calcium", "dipropionate", "propionate", "valerate", "furoate",
    "etabonate", "xinafoate", "ester", "hcl",
)
_SALT_SUFFIX_RE = re.compile(
    r"\s+(?:" + "|".join(_SALT_SUFFIXES) + r")\b",
    re.IGNORECASE,
)

# Splitters for multi-drug rows: comma, semicolon, slash, " and ", " plus ",
# " with ", " + ".
_SPLIT_RE = re.compile(r"\s*(?:[,;/+]| and | plus | with )\s*", re.IGNORECASE)


def normalise(name: str) -> str:
    """Lowercase, collapse whitespace, strip non-word punctuation."""
    s = (name or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_salts(name: str) -> str:
    """Remove trailing salt/ester/hydrate words.

    "abiraterone acetate" -> "abiraterone"
    "fluticasone propionate" -> "fluticasone"
    "morphine hydrochloride trihydrate" -> "morphine"
    """
    prev = None
    s = name
    # Repeat in case of stacked salts (e.g. "X hydrochloride monohydrate")
    while prev != s:
        prev = s
        s = _SALT_SUFFIX_RE.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def split_multi_drug(name: str) -> list[str]:
    """Split a "drug A, drug B" or "drug A + drug B" entry into components.

    Returns a list of one or more candidate drug names. The original full
    string is always included as the first entry; individual components
    follow.
    """
    full = (name or "").strip()
    if not full:
        return []
    parts = [p.strip() for p in _SPLIT_RE.split(full) if p.strip()]
    out = [full]
    for p in parts:
        if p != full and p not in out:
            out.append(p)
    return out


def candidate_keys(name: str) -> list[str]:
    """Yield lookup candidates in order of specificity.

    Each candidate is normalised. Caller iterates and uses the first key
    that hits in their lookup map.

    Order:
      1. The exact normalised form
      2. The full string with salts stripped
      3. Each component (multi-drug split), normalised
      4. Each component, salts stripped
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(candidate: str) -> None:
        k = normalise(candidate)
        if k and k not in seen:
            seen.add(k)
            out.append(k)

    add(name)
    add(strip_salts(name))
    for component in split_multi_drug(name)[1:]:   # skip the full string (already added)
        add(component)
        add(strip_salts(component))
    return out
