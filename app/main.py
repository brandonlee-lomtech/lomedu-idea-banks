import fcntl
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("IDEA_BANK_DATA_DIR", _ROOT / "data"))
_IDEAS_PATH = _DATA_DIR / "ideas.json"
_LOCK_PATH = _DATA_DIR / ".ideas.lock"

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

app = Flask(
    __name__,
    template_folder=str(_ROOT / "templates"),
    static_folder=None,
)


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_ideas() -> list:
    if not _IDEAS_PATH.exists():
        return []
    with open(_IDEAS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    return data


def _save_ideas_atomic(ideas: list) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=_DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(ideas, tmp, indent=2)
        os.replace(tmp_name, _IDEAS_PATH)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _assign_missing_ids(ideas: list) -> bool:
    changed = False
    for item in ideas:
        if isinstance(item, dict) and not item.get("id"):
            item["id"] = str(uuid.uuid4())
            changed = True
    return changed


def read_ideas_normalize() -> list:
    """Load ideas under exclusive lock; assign ids to legacy rows and persist if needed."""
    _ensure_data_dir()
    with open(_LOCK_PATH, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            ideas = _load_ideas()
            if _assign_missing_ids(ideas):
                _save_ideas_atomic(ideas)
            return ideas
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def append_idea(record: dict) -> None:
    _ensure_data_dir()
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            ideas = _load_ideas()
            _assign_missing_ids(ideas)
            ideas.append(record)
            _save_ideas_atomic(ideas)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def delete_idea_by_id(target_id: str) -> bool:
    """Remove the idea with the given id. Returns True if a row was removed."""
    _ensure_data_dir()
    with open(_LOCK_PATH, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            ideas = _load_ideas()
            changed = _assign_missing_ids(ideas)
            next_ideas = [
                x
                for x in ideas
                if not (isinstance(x, dict) and x.get("id") == target_id)
            ]
            removed = len(next_ideas) < len(ideas)
            if removed:
                _save_ideas_atomic(next_ideas)
                return True
            if changed:
                _save_ideas_atomic(ideas)
            return False
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _validate_payload(data: dict | None) -> tuple[str | None, dict | None]:
    if not isinstance(data, dict):
        return "Invalid JSON body", None
    name = data.get("name")
    email = data.get("email")
    idea = data.get("idea")
    if not isinstance(name, str) or not name.strip():
        return "Name is required", None
    if not isinstance(email, str) or not email.strip():
        return "Email is required", None
    if not isinstance(idea, str) or not idea.strip():
        return "Idea is required", None
    email = email.strip()
    if not _EMAIL_RE.match(email):
        return "Invalid email address", None
    return None, {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "email": email,
        "idea": idea.strip(),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/submissions")
def submissions():
    load_error = False
    try:
        ideas = list(reversed(read_ideas_normalize()))
    except (OSError, json.JSONDecodeError):
        ideas = []
        load_error = True
    return render_template("submissions.html", ideas=ideas, load_error=load_error)


@app.post("/api/ideas")
def create_idea():
    data = request.get_json(silent=True)
    err, record = _validate_payload(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        append_idea(record)
    except OSError:
        return jsonify({"ok": False, "error": "Could not save idea"}), 500
    return jsonify({"ok": True}), 201


@app.delete("/api/ideas/<uuid:idea_id>")
def delete_idea(idea_id: uuid.UUID):
    key = str(idea_id)
    try:
        removed = delete_idea_by_id(key)
    except OSError:
        return jsonify({"ok": False, "error": "Could not update storage"}), 500
    if not removed:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True}), 200
