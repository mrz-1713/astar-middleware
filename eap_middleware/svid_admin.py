"""Admin-editable SVID collection JSON support."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .models import SvidAdminState, SvidSelection
from .profiles import MachineProfile

logger = logging.getLogger(__name__)


class SvidAdminError(ValueError):
    """Raised when an SVID admin file is missing or malformed."""

    pass


class SvidAdminConfig:
    """Loads and hot-reloads DataCollectSwitch, RecipeList, and SvidList files."""

    SWITCH_FILE = "DataCollectSwitch.json"
    RECIPE_FILE = "RecipeList.json"
    SVID_FILE = "SvidList.json"

    def __init__(self, directory: str | Path, profile: MachineProfile):
        self.directory = Path(directory)
        self.profile = profile
        self._cached_state: Optional[SvidAdminState] = None
        self._cached_mtimes: Dict[str, float] = {}

    def ensure_default_files(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        defaults = {
            self.SWITCH_FILE: {"DataCollectSwitch": "ON", "DataIntervalInSec": 1},
            self.RECIPE_FILE: {"Recipe_List": []},
            self.SVID_FILE: {
                "RecipeSvidList": [],
                "SvidList": list(self.profile.identity_svid_names),
            },
        }
        for filename, content in defaults.items():
            path = self.directory / filename
            if not path.exists():
                path.write_text(json.dumps(content, indent=2), encoding="utf-8")

    def load(self) -> SvidAdminState:
        mtimes = self._current_mtimes()
        if self._cached_state is not None and mtimes == self._cached_mtimes:
            return self._cached_state
        state = self._load_uncached()
        self._cached_state = state
        self._cached_mtimes = mtimes
        return state

    def _current_mtimes(self) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for filename in (self.SWITCH_FILE, self.RECIPE_FILE, self.SVID_FILE):
            path = self.directory / filename
            result[filename] = path.stat().st_mtime if path.exists() else -1
        return result

    def _read_json(self, filename: str, default: Mapping[str, Any]) -> Mapping[str, Any]:
        path = self.directory / filename
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SvidAdminError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SvidAdminError(f"{path} must contain a JSON object")
        return data

    def _load_uncached(self) -> SvidAdminState:
        switch = self._read_json(
            self.SWITCH_FILE,
            {"DataCollectSwitch": "ON", "DataIntervalInSec": 1},
        )
        enabled_value = str(switch.get("DataCollectSwitch", "ON")).upper()
        if enabled_value not in {"ON", "OFF"}:
            raise SvidAdminError("DataCollectSwitch must be ON or OFF")
        try:
            interval = float(switch.get("DataIntervalInSec", 1))
        except (TypeError, ValueError) as exc:
            raise SvidAdminError("DataIntervalInSec must be numeric") from exc
        if interval <= 0:
            raise SvidAdminError("DataIntervalInSec must be greater than zero")

        recipes = self._read_json(self.RECIPE_FILE, {"Recipe_List": []})
        recipe_list = self._list_of_str(recipes.get("Recipe_List", []), "Recipe_List")

        svid_data = self._read_json(
            self.SVID_FILE,
            {"RecipeSvidList": [], "SvidList": []},
        )
        recipe_svid_list = self._list_of_str(
            svid_data.get("RecipeSvidList", []),
            "RecipeSvidList",
        )
        selections, invalid_entries = self._parse_svid_list(svid_data.get("SvidList", []))

        return SvidAdminState(
            enabled=enabled_value == "ON",
            interval_sec=interval,
            recipe_list=recipe_list,
            recipe_svid_list=recipe_svid_list,
            svids=selections,
            invalid_entries=invalid_entries,
        )

    def _list_of_str(self, value: Any, field_name: str) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise SvidAdminError(f"{field_name} must be a list")
        result: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SvidAdminError(f"{field_name} items must be strings")
            if item.strip():
                result.append(item.strip())
        return result

    def _parse_svid_list(self, raw_items: Any) -> tuple[List[SvidSelection], List[str]]:
        if raw_items is None:
            return [], []
        if not isinstance(raw_items, list):
            raise SvidAdminError("SvidList must be a list")
        selections: List[SvidSelection] = []
        invalid: List[str] = []
        for item in raw_items:
            parsed = self._parse_svid_item(item)
            if parsed is None:
                invalid.append(str(item))
            else:
                selections.append(parsed)
        return selections, invalid

    def _parse_svid_item(self, item: Any) -> Optional[SvidSelection]:
        if isinstance(item, str):
            name = item.strip()
            svid = self.profile.resolve_svid_name(name)
            if svid is None:
                return None
            return SvidSelection(svid=svid, name=name)
        if isinstance(item, dict):
            name = str(item.get("Name") or item.get("name") or "").strip()
            raw_svid = item.get("SVID", item.get("svid"))
            if raw_svid is not None:
                try:
                    svid = int(raw_svid)
                except (TypeError, ValueError):
                    return None
                if svid <= 0:
                    return None  # caller reports it in invalid_entries
                return SvidSelection(svid=svid, name=name or f"SVID_{svid}")
            if name:
                svid = self.profile.resolve_svid_name(name)
                if svid is not None:
                    return SvidSelection(svid=svid, name=name)
        return None

