"""Broad taxonomic groups ("crabs", "sponges") and WoRMS lineage matching.

SeaTube annotations carry a WoRMS AphiaID but no lineage, so "give me every
crab" cannot be asked of the ONC API directly.  This module resolves an
AphiaID to its WoRMS classification once, caches the lineage on disk, and
matches it against a small vocabulary of colloquial groups defined by their
ancestor taxa.

Groups are matched on ancestor *names*, not ranks: WoRMS reorganises ranks
over time (Actinopterygii is currently a Gigaclass, Anthozoa a Subphylum),
but the names in a lineage are stable.
"""

from __future__ import annotations

import json
import logging
import re
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

import requests

WORMS_REST = "https://www.marinespecies.org/rest"
logger = logging.getLogger(__name__)


class IncompleteTaxonomyWarning(UserWarning):
    """Some taxa could not be classified; absence of matches is inconclusive."""


def as_names(values: Sequence[str]) -> List[str]:
    """Accept either one name or a sequence, without iterating a string's letters."""
    return [values] if isinstance(values, str) else list(values)

# group name -> (ancestor taxa, human description)
TAXON_GROUPS: Dict[str, Dict[str, Any]] = {
    "fish": {
        "ancestors": ["Actinopterygii", "Chondrichthyes", "Elasmobranchii",
                      "Holocephali", "Myxini", "Petromyzontida"],
        "description": "ray-finned fishes, sharks, rays, chimaeras, hagfish, lampreys",
    },
    "sharks-and-rays": {"ancestors": ["Elasmobranchii"], "description": "sharks, skates and rays"},
    "rockfish": {"ancestors": ["Sebastidae"], "description": "Sebastes and relatives"},
    "crabs": {"ancestors": ["Brachyura", "Anomura"],
              "description": "true crabs plus hermit/king/squat crabs"},
    "true-crabs": {"ancestors": ["Brachyura"], "description": "Brachyura only"},
    "hermit-crabs": {"ancestors": ["Paguroidea"], "description": "hermit crabs"},
    "squat-lobsters": {"ancestors": ["Galatheoidea", "Chirostyloidea"], "description": "squat lobsters"},
    "shrimp": {"ancestors": ["Caridea", "Dendrobranchiata", "Stenopodidea"], "description": "shrimps and prawns"},
    "lobsters": {"ancestors": ["Achelata", "Astacidea", "Polychelida"], "description": "clawed and spiny lobsters"},
    "barnacles": {"ancestors": ["Cirripedia"], "description": "barnacles"},
    "crustaceans": {"ancestors": ["Crustacea"], "description": "all crustaceans"},
    "sea-stars": {"ancestors": ["Asteroidea"], "description": "starfish"},
    "brittle-stars": {"ancestors": ["Ophiuroidea"], "description": "brittle and basket stars"},
    "sea-urchins": {"ancestors": ["Echinoidea"], "description": "urchins and sand dollars"},
    "sea-cucumbers": {"ancestors": ["Holothuroidea"], "description": "holothurians"},
    "crinoids": {"ancestors": ["Crinoidea"], "description": "feather stars and sea lilies"},
    "echinoderms": {"ancestors": ["Echinodermata"], "description": "all echinoderms"},
    "corals": {"ancestors": ["Scleractinia", "Antipatharia", "Octocorallia", "Stylasteridae"],
               "description": "hard, soft, black and hydrocorals"},
    "hard-corals": {"ancestors": ["Scleractinia"], "description": "stony corals"},
    "soft-corals": {"ancestors": ["Octocorallia"], "description": "octocorals, gorgonians, sea pens"},
    "black-corals": {"ancestors": ["Antipatharia"], "description": "black corals"},
    "sea-pens": {"ancestors": ["Pennatuloidea", "Pennatulacea"],
                 "description": "sea pens (current and legacy ancestor names)"},
    "anemones": {"ancestors": ["Actiniaria", "Corallimorpharia"], "description": "sea anemones"},
    "tube-anemones": {"ancestors": ["Ceriantharia"], "description": "cerianthid tube anemones"},
    "jellyfish": {"ancestors": ["Scyphozoa", "Cubozoa", "Staurozoa"], "description": "true jellyfish"},
    "hydroids": {"ancestors": ["Hydrozoa"], "description": "hydroids and hydromedusae"},
    "cnidarians": {"ancestors": ["Cnidaria"], "description": "all cnidarians"},
    "ctenophores": {"ancestors": ["Ctenophora"], "description": "comb jellies"},
    "sponges": {"ancestors": ["Porifera"], "description": "all sponges"},
    "glass-sponges": {"ancestors": ["Hexactinellida"], "description": "hexactinellid glass sponges"},
    "octopus-and-squid": {"ancestors": ["Cephalopoda"], "description": "cephalopods"},
    "snails": {"ancestors": ["Gastropoda"], "description": "gastropods"},
    "nudibranchs": {"ancestors": ["Nudibranchia"], "description": "nudibranch sea slugs (not all sea slugs)"},
    "bivalves": {"ancestors": ["Bivalvia"], "description": "clams, mussels, scallops"},
    "molluscs": {"ancestors": ["Mollusca"], "description": "all molluscs"},
    "worms": {"ancestors": ["Annelida", "Nemertea", "Sipuncula", "Echiura"],
              "description": "segmented, ribbon and peanut worms"},
    "tunicates": {"ancestors": ["Tunicata"], "description": "sea squirts and salps"},
    "bryozoans": {"ancestors": ["Bryozoa"], "description": "moss animals"},
    "brachiopods": {"ancestors": ["Brachiopoda"], "description": "lamp shells"},
    "sea-spiders": {"ancestors": ["Pycnogonida"], "description": "pycnogonids"},
    "marine-mammals": {"ancestors": ["Mammalia"], "description": "all mammal annotations, including whales, dolphins and seals"},
    "seabirds": {"ancestors": ["Aves"], "description": "all bird annotations; not restricted to seabirds"},
    "algae": {"ancestors": ["Rhodophyta", "Chlorophyta", "Phaeophyceae", "Ochrophyta"],
              "description": "red/green algae and ochrophytes (including brown algae)"},
    "bacteria": {"ancestors": ["Bacteria"], "description": "all bacterial annotations, including mats"},
}

# Colloquial spellings accepted by both Python and the CLI. Aliases select the
# ENTIRE target group: e.g. "squid" also includes other cephalopods.
GROUP_ALIASES: Dict[str, str] = {
    "crab": "crabs", "sponge": "sponges", "seastar": "sea-stars", "starfish": "sea-stars",
    "sea-star": "sea-stars", "urchin": "sea-urchins", "sea-urchin": "sea-urchins",
    "urchins": "sea-urchins", "cucumber": "sea-cucumbers", "sea-cucumber": "sea-cucumbers",
    "coral": "corals", "anemone": "anemones", "octopus": "octopus-and-squid",
    "squid": "octopus-and-squid", "cephalopods": "octopus-and-squid", "fishes": "fish",
    "shark": "sharks-and-rays", "sharks": "sharks-and-rays", "ray": "sharks-and-rays",
    "rays": "sharks-and-rays", "shrimps": "shrimp", "prawn": "shrimp", "prawns": "shrimp",
    "lobster": "lobsters", "snail": "snails", "gastropods": "snails", "bivalve": "bivalves",
    "worm": "worms", "polychaetes": "worms", "tunicate": "tunicates", "jelly": "jellyfish",
    "jellies": "jellyfish", "brittlestar": "brittle-stars", "brittle-star": "brittle-stars",
    "feather-star": "crinoids", "crinoid": "crinoids", "sea-pen": "sea-pens",
    "barnacle": "barnacles", "kelp": "algae", "seaweed": "algae", "bird": "seabirds",
    "birds": "seabirds", "mammal": "marine-mammals", "mammals": "marine-mammals",
    "glass-sponge": "glass-sponges", "hermit-crab": "hermit-crabs",
}


def normalize_group_name(name: str) -> str:
    """Map a user-typed group name onto a canonical key, or raise."""
    key = str(name).strip().lower().replace("_", "-").replace(" ", "-")
    key = GROUP_ALIASES.get(key, key)
    if key not in TAXON_GROUPS:
        raise KeyError(name)
    return key


def ancestors_for_groups(names: Iterable[str]) -> Set[str]:
    """Collect the ancestor taxa for a set of group names (lowercased)."""
    out: Set[str] = set()
    for name in as_names(names):
        out.update(a.lower() for a in TAXON_GROUPS[normalize_group_name(name)]["ancestors"])
    return out


def organism_groups(query: str = "") -> List[Dict[str, Any]]:
    """Discover supported groups, definitions and aliases (no network).

    These are search definitions, not evidence that an organism occurs in a
    particular dive or date range. Use ``AnnotationSet.group_summary`` for that.
    """
    rows = []
    needle = query.strip().casefold().replace("_", "-").replace(" ", "-")
    for name, spec in TAXON_GROUPS.items():
        aliases = sorted(a for a, target in GROUP_ALIASES.items() if target == name)
        row = {"group": name, "description": spec["description"],
               "ancestors": list(spec["ancestors"]), "aliases": aliases}
        searchable = " ".join([name, spec["description"], *spec["ancestors"], *aliases])
        if not needle or needle in searchable.casefold().replace(" ", "-"):
            rows.append(row)
    return rows


def search_terms(organisms: Sequence[str]) -> tuple:
    """Split familiar group names and explicit scientific names for a search."""
    groups, taxa = [], []
    for term in as_names(organisms):
        if not isinstance(term, str) or not term.strip():
            raise ValueError("Each organism must be a non-empty group or scientific name")
        try:
            groups.append(normalize_group_name(term))
        except KeyError:
            taxa.append(term.strip())
    return groups, taxa


def format_group_table() -> str:
    width = max(len(k) for k in TAXON_GROUPS)
    lines = [f"{'group':<{width}}  matches (WoRMS ancestor taxa)", "-" * (width + 50)]
    for name, spec in TAXON_GROUPS.items():
        lines.append(f"{name:<{width}}  {spec['description']} [{', '.join(spec['ancestors'])}]")
    return "\n".join(lines)


def aphia_id_from_taxon(taxon: Dict[str, Any]) -> Optional[int]:
    """Pull the WoRMS AphiaID out of a SeaTube taxonomy entry.

    ``taxonId`` is an ONC-internal identifier; the AphiaID travels in
    ``referenceId`` (and in the ``taxonUrl`` query string).
    """
    if str(taxon.get("taxonomyCode") or "").upper() not in {"WORMS", ""}:
        return None
    ref = taxon.get("referenceId")
    if ref not in (None, ""):
        try:
            return int(str(ref).strip())
        except ValueError:
            pass
    match = re.search(r"[?&]id=(\d+)", str(taxon.get("taxonUrl") or ""))
    return int(match.group(1)) if match else None


def taxon_names(taxon: Dict[str, Any]) -> List[str]:
    """Every name the annotation itself offers, before any lookup."""
    raw = [taxon.get("taxonName"), taxon.get("acceptedName"), taxon.get("scientificName"),
           taxon.get("validName")]
    label = str(taxon.get("displayText") or "")
    if label:
        raw.append(label.split("(")[0].split("|")[0])
    return [str(n).strip() for n in raw if n]


class WormsResolver:
    """Resolves WoRMS AphiaIDs to full lineages, with an on-disk cache.

    One REST call per distinct taxon, ever: lineages go into a JSON cache
    keyed by AphiaID, and ``offline=True`` forbids network entirely.
    """

    def __init__(
        self,
        cache_path: Optional[str] = None,
        *,
        offline: bool = False,
        fetcher: Optional[Callable[[int], List[str]]] = None,
        timeout_seconds: int = 45,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.offline = offline
        self.timeout_seconds = timeout_seconds
        self._fetcher = fetcher or self._fetch_lineage
        self._cache: Dict[str, List[str]] = {}
        self._dirty = False
        self.unresolved: Set[str] = set()
        self._failed: Set[str] = set()
        if self.cache_path and self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    self._cache = {k: v for k, v in cached.items()
                                   if isinstance(v, list) and v and
                                   all(isinstance(n, str) for n in v)}
            except (OSError, ValueError):
                self._cache = {}

    def _fetch_lineage(self, aphia_id: int) -> List[str]:
        url = f"{WORMS_REST}/AphiaClassificationByAphiaID/{int(aphia_id)}"
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=self.timeout_seconds)
                if resp.status_code == 204:  # WoRMS says "no content" for unknown IDs
                    return []
                resp.raise_for_status()
                node = resp.json()
                names: List[str] = []
                while node:
                    name = node.get("scientificname")
                    if name:
                        names.append(str(name))
                    node = node.get("child")
                return names
            except Exception as exc:  # network flake, rate limit, malformed payload
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"WoRMS lookup failed for AphiaID {aphia_id}: {last_error}")

    def lineage(self, aphia_id: int) -> List[str]:
        key = str(int(aphia_id))
        if key in self._cache:
            return self._cache[key]
        if self.offline or key in self._failed:
            self.unresolved.add(key)
            return []
        try:
            names = self._fetcher(int(aphia_id))
        except Exception as exc:
            logger.warning("%s", exc)
            self.unresolved.add(key)
            self._failed.add(key)
            return []
        if not names:
            self.unresolved.add(key)
            self._failed.add(key)
            return []
        self.unresolved.discard(key)
        self._cache[key] = names
        self._dirty = True
        return names

    def save(self) -> None:
        if self.cache_path and self._dirty:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._cache, indent=1, sort_keys=True), encoding="utf-8")
            temporary.replace(self.cache_path)
            self._dirty = False

    def taxon_matches(self, taxon: Dict[str, Any], wanted: Set[str]) -> bool:
        """True if this taxonomy entry sits at or below any wanted ancestor.

        ``wanted`` holds lowercased taxon names.  Names carried by the
        annotation are checked first so that an annotation labelled
        "Porifera" matches ``--group sponges`` without any network call.
        """
        if not wanted:
            return True
        if any(name.lower() in wanted for name in taxon_names(taxon)):
            return True
        aphia_id = aphia_id_from_taxon(taxon)
        if aphia_id is None:
            self.unresolved.add("label:" + "|".join(taxon_names(taxon) or ["unknown"]))
            return False
        return any(name.lower() in wanted for name in self.lineage(aphia_id))

    def matching_taxa(self, annotation: Dict[str, Any], wanted: Set[str]) -> List[Dict[str, Any]]:
        return [t for t in (annotation.get("taxonomy") or []) if self.taxon_matches(t, wanted)]

    def annotation_matches(self, annotation: Dict[str, Any], wanted: Set[str]) -> bool:
        if not wanted:
            return True
        return bool(self.matching_taxa(annotation, wanted))

    def groups_for(self, taxon: Dict[str, Any]) -> List[str]:
        """Which vocabulary groups this taxon belongs to (for labelling output)."""
        names = {n.lower() for n in taxon_names(taxon)}
        aphia_id = aphia_id_from_taxon(taxon)
        if aphia_id is not None:
            names.update(n.lower() for n in self.lineage(aphia_id))
        else:
            self.unresolved.add("label:" + "|".join(taxon_names(taxon) or ["unknown"]))
        return [g for g, spec in TAXON_GROUPS.items()
                if names & {a.lower() for a in spec["ancestors"]}]

    def warn_unresolved(self) -> None:
        if self.unresolved:
            warnings.warn(
                f"Could not fully classify {len(self.unresolved)} taxon(s). "
                "Results may be incomplete; inspect resolver.unresolved. "
                "A zero count does not establish absence.",
                IncompleteTaxonomyWarning, stacklevel=3,
            )


def wanted_ancestor_names(groups: Sequence[str], ancestors: Sequence[str]) -> Set[str]:
    """Build the lowercased ancestor set from --group and --taxon-name values."""
    wanted = ancestors_for_groups(groups) if groups else set()
    wanted.update(a.strip().lower() for a in as_names(ancestors) if a.strip())
    return wanted
