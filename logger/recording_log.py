
from datetime import datetime, timezone
import json, os, tempfile
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = "1.0"
ISO_TS_TIMESPEC = "seconds"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec=ISO_TS_TIMESPEC).replace("+00:00", "Z")

def _atomic_write(path: str, content: str) -> None:
    dir_name = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8", newline="\n") as tf:
        tmp = tf.name
        tf.write(content)
    os.replace(tmp, path)

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

class RecordingLog:
    def __init__(self, jsonl_path: str):
        self.path = jsonl_path
        _ensure_dir(self.path)
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()

    def _iter_records(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _write_all(self, records):
        content = "\n".join(json.dumps(r, ensure_ascii=False, separators=(',', ':')) for r in records)
        if content:
            content += "\n"
        _atomic_write(self.path, content)

    def read_all(self):
        return list(self._iter_records())

    def get_record(self, recording_id: str):
        for rec in self._iter_records():
            if rec.get("recording_id") == recording_id:
                return rec
        return None

    @staticmethod
    def new_record(recording_id: str, gloss: str, capture_start: Optional[str] = None, capture_end: Optional[str] = None):
        now = _utc_now_iso()
        rec = {
            "version": SCHEMA_VERSION,
            "recording_id": recording_id,
            "gloss": gloss,
            "capture_start": capture_start or now,
            "capture_end": capture_end or None,
            "assets": {
                "blendshape_csv": [],
                "retargeted_animation_fbx": [],
                "original_mocap_mcp": [],
                "video_mkv": [],
                "original_animation_fbx": [],
                "mocap_marker_csv": []
            },
            "created_at": now,
            "updated_at": now
        }
        return rec

    @staticmethod
    def _new_asset_entry(path: Optional[str], machine: str, status: str = "ready",
                         mtime: Optional[str] = None, quality: Optional[Dict[str, Any]] = None,
                         metadata: Optional[Dict[str, Any]] = None):
        entry = {
            "path": path,
            "machine": machine,
            "status": status,
            "mtime": mtime,
            "quality": quality if quality is not None else {}
        }
        if metadata is not None:
            entry["metadata"] = metadata
        return entry

    def create_recording(self, recording_id: str, gloss: str, capture_start: Optional[str] = None, capture_end: Optional[str] = None):
        if self.get_record(recording_id) is not None:
            raise ValueError(f"Recording with id '{recording_id}' already exists.")
        rec = self.new_record(recording_id, gloss, capture_start, capture_end)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        return rec

    def _update_record(self, recording_id: str, updater):
        updated = None
        records = []
        for rec in self._iter_records():
            if rec.get("recording_id") == recording_id:
                updated = updater(rec)
                rec = updated
            records.append(rec)
        if updated is None:
            raise KeyError(f"Recording '{recording_id}' not found.")
        self._write_all(records)
        return updated

    def add_asset(self, recording_id: str, asset_type: str, path: Optional[str], machine: str,
                  status: str = "ready", mtime: Optional[str] = None,
                  quality: Optional[Dict[str, Any]] = None, metadata: Optional[Dict[str, Any]] = None):
        def do_update(rec):
            assets = rec.setdefault("assets", {})
            if asset_type not in assets:
                assets[asset_type] = []
            entry = self._new_asset_entry(path, machine, status, mtime, quality, metadata)
            assets[asset_type].append(entry)
            rec["updated_at"] = _utc_now_iso()
            return rec
        return self._update_record(recording_id, do_update)

    def update_asset_status(self, recording_id: str, asset_type: str, path: Optional[str], new_status: str,
                            mtime: Optional[str] = None):
        def do_update(rec):
            assets = rec.get("assets", {})
            lst = assets.get(asset_type, [])
            matched = False
            for item in lst:
                if (path is None and item.get("path") is None) or (path is not None and item.get("path") == path):
                    item["status"] = new_status
                    if mtime is not None:
                        item["mtime"] = mtime
                    matched = True
                    break
            if not matched:
                raise KeyError(f"Asset not found for type='{asset_type}' and path='{path}'.")
            rec["updated_at"] = _utc_now_iso()
            return rec
        return self._update_record(recording_id, do_update)

    def set_capture_times(self, recording_id: str, capture_start: Optional[str] = None, capture_end: Optional[str] = None):
        def do_update(rec):
            if capture_start is not None:
                rec["capture_start"] = capture_start
            if capture_end is not None:
                rec["capture_end"] = capture_end
            rec["updated_at"] = _utc_now_iso()
            return rec
        return self._update_record(recording_id, do_update)

    def set_field(self, recording_id: str, field_path: List[str], value: Any):
        def do_update(rec):
            ref = rec
            for key in field_path[:-1]:
                ref = ref[key]
            ref[field_path[-1]] = value
            rec["updated_at"] = _utc_now_iso()
            return rec
        return self._update_record(recording_id, do_update)
