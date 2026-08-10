from pathlib import Path
import hashlib
import html
import json
import os
import threading
import uuid
import yaml

from collections.abc import Callable
from typing import cast
from fastapi import Body, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from organizer.attempt_review import (
    Abandon,
    Accept,
    AttemptFilters,
    AttemptReviewDetails,
    AttemptReview,
    MarkActionApplied,
    Reopen,
    RetryFromStart,
    RetryRemaining,
)
from organizer.config import (
    ConfigError,
    WatchFolderConfig,
    rebuild_boundary_policy,
    validate_watch_id,
    validate_watch_root,
)
from organizer.daemon import WatchMutator, effective_stability_interval
from organizer.item_processor import BoundaryPolicy, ItemProcessor, ItemSnapshot
from organizer.operational_health import OperationalHealth
from organizer.structured_log import LogLevel, MemoryLogSink

__all__ = ["WatchFolderConfig", "create_app"]

_WEB_ROOT = Path(__file__).parent / "web_ui"
_TEMPLATES = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))
_RULE_SAVE_LOCK = threading.Lock()
_WATCH_SAVE_LOCK = threading.Lock()
_RUNTIME_WATCHES_LOCK = threading.RLock()


def _resolved_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _rules_yaml_to_model(rules_text: str) -> list[dict[str, object]]:
    """Parse a rules YAML document into the visual rule-builder model.

    The model is a list of rule mappings, each with ``name``, optional flags,
    an ordered ``conditions`` list (name/field/pattern) and an ordered
    ``actions`` list (kind/params). The primary ``match`` condition is folded
    into ``conditions`` first, matching the parser's treatment of it as a
    condition named ``match``.
    """
    try:
        document = yaml.safe_load(rules_text) or {}
    except yaml.YAMLError:
        return []
    raw_rules = document.get("rules", []) if isinstance(document, dict) else []
    if not isinstance(raw_rules, list):
        return []
    model: list[dict[str, object]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue
        conditions: list[dict[str, object]] = []
        match = rule.get("match")
        if isinstance(match, dict):
            conditions.append(
                {
                    "name": "match",
                    "field": match.get("field", ""),
                    "pattern": match.get("pattern", ""),
                }
            )
        raw_conditions = rule.get("conditions")
        if isinstance(raw_conditions, dict):
            for cond_name, condition in raw_conditions.items():
                if isinstance(condition, dict):
                    conditions.append(
                        {
                            "name": cond_name,
                            "field": condition.get("field", ""),
                            "pattern": condition.get("pattern", ""),
                        }
                    )
        actions: list[dict[str, object]] = []
        raw_actions = rule.get("actions")
        if isinstance(raw_actions, list):
            for action in raw_actions:
                if isinstance(action, dict) and len(action) == 1:
                    kind = next(iter(action))
                    params = action[kind]
                    actions.append(
                        {
                            "kind": kind,
                            "params": params if isinstance(params, dict) else {},
                        }
                    )
        model.append(
            {
                "name": rule.get("name", ""),
                "allow_direct_deletion": bool(rule.get("allow_direct_deletion", False)),
                "allow_hard_link_removal": bool(
                    rule.get("allow_hard_link_removal", False)
                ),
                "conditions": conditions,
                "actions": actions,
            }
        )
    return model


def _model_to_rules_yaml(model: object) -> str:
    """Serialize the visual rule-builder model into a rules YAML document."""
    if not isinstance(model, list):
        raise ValueError("rule model must be a list")
    rules_list: list[dict[str, object]] = []
    for rule in model:
        if not isinstance(rule, dict):
            raise ValueError("each rule must be a mapping")
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each rule requires a name")
        conditions = rule.get("conditions", [])
        if not isinstance(conditions, list):
            raise ValueError("conditions must be a list")
        actions = rule.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError("actions must be a list")
        out: dict[str, object] = {"name": name}
        if conditions:
            first = conditions[0]
            if not isinstance(first, dict):
                raise ValueError("each condition must be a mapping")
            out["match"] = {
                "field": first.get("field", "file_name"),
                "pattern": first.get("pattern", ""),
            }
            if len(conditions) > 1:
                extra: dict[str, object] = {}
                for index, condition in enumerate(conditions[1:], start=1):
                    if not isinstance(condition, dict):
                        raise ValueError("each condition must be a mapping")
                    cond_name = condition.get("name") or f"condition_{index}"
                    extra[str(cond_name)] = {
                        "field": condition.get("field", "file_name"),
                        "pattern": condition.get("pattern", ""),
                    }
                out["conditions"] = extra
        serialized_actions: list[dict[str, object]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = action.get("kind")
            if not isinstance(kind, str) or not kind:
                continue
            serialized_actions.append({kind: action.get("params", {})})
        if serialized_actions:
            out["actions"] = serialized_actions
        if rule.get("allow_direct_deletion"):
            out["allow_direct_deletion"] = True
        if rule.get("allow_hard_link_removal"):
            out["allow_hard_link_removal"] = True
        rules_list.append(out)
    return yaml.dump({"rules": rules_list}, default_flow_style=False, sort_keys=False)


def create_app(
    processor: ItemProcessor,
    *,
    log_sink: MemoryLogSink | None = None,
    health_checker: OperationalHealth | None = None,
    watch_folders: list[WatchFolderConfig]
    | tuple[WatchFolderConfig, ...]
    | None = None,
    db_path: Path | None = None,
    watch_mutator: WatchMutator | None = None,
    config_path: Path | None = None,
    stability_interval: float = 0.0,
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    review = AttemptReview(processor)
    _runtime_watches: list[WatchFolderConfig] = list(watch_folders or [])

    def _watch_config(watch_id: str) -> WatchFolderConfig | None:
        with _RUNTIME_WATCHES_LOCK:
            for config in _runtime_watches:
                if config.watch_id == watch_id:
                    return config
        return None

    def _watch_snapshot() -> list[WatchFolderConfig]:
        with _RUNTIME_WATCHES_LOCK:
            return list(_runtime_watches)

    def _rule_count(rules_path: Path) -> int:
        try:
            document = yaml.safe_load(rules_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return 0
        rules = document.get("rules", []) if isinstance(document, dict) else []
        return len(rules) if isinstance(rules, list) else 0

    def _health_by_watch() -> dict[str, tuple[bool, str]]:
        watches = _watch_snapshot()
        if health_checker is None or db_path is None or not watches:
            return {}
        health = health_checker.check_all(
            watch_folders=[(config.watch_id, config.watch_root) for config in watches],
            db_path=db_path,
        )
        return {
            entry.watch_id: (entry.accessible, entry.detail)
            for entry in health.watch_folder_healths
        }

    def _fragment(request: Request, template: str, **context: object) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, template, context)

    def _rules_feedback(message: str, *, error: bool = False) -> str:
        css_class = "feedback error" if error else "feedback success"
        return f'<p class="{css_class}">{html.escape(message)}</p>'

    def _attempt_payload(details: AttemptReviewDetails) -> dict[str, object]:
        return {
            "attempt_id": details.attempt_id,
            "watch_id": details.watch_id,
            "source_path": details.source_path,
            "source_fingerprint": details.source_fingerprint,
            "source_size": details.source_size,
            "source_mtime": details.source_mtime,
            "rule_name": details.rule_name,
            "status": details.status,
            "planned_actions": list(details.planned_actions),
            "action_results": list(details.action_results),
            "filesystem_evidence": list(details.filesystem_evidence),
            "resulting_paths": list(details.resulting_paths),
            "failure_detail": details.failure_detail,
            "suppressions": list(details.suppressions),
            "linked_attempts": list(details.linked_attempts),
            "processing_lineage": list(details.processing_lineage),
            "accepted_results": list(details.accepted_results),
            "audit_events": list(details.audit_events),
            "abandoned_reason": details.abandoned_reason,
            "created_at": details.created_at,
            "completed_at": details.completed_at,
        }

    def _is_html_request(request: Request) -> bool:
        return request.headers.get(
            "HX-Request"
        ) == "true" or "text/html" in request.headers.get("accept", "")

    def _form_or_json(body: bytes) -> dict[str, object]:
        try:
            return dict(json.loads(body))
        except (json.JSONDecodeError, TypeError, ValueError):
            from urllib.parse import parse_qs

            return {key: values[-1] for key, values in parse_qs(body.decode()).items()}

    def _update_watches_on_disk(
        transform: Callable[[list[dict[str, object]]], list[dict[str, object]]]
    ) -> list[dict[str, object]]:
        """Read-modify-write watches in organizer.yaml atomically under one lock.

        Raises HTTPException(500) if the config cannot be read or written so a
        transient read error can never silently replace the file with a partial
        document. When no ``config_path`` is configured (in-memory mode) this is
        a documented no-op: the caller still mutates the runtime watches.
        """
        if config_path is None:
            return []
        with _WATCH_SAVE_LOCK:
            try:
                loaded = yaml.safe_load(config_path.read_text())
            except (OSError, yaml.YAMLError) as error:
                raise HTTPException(
                    status_code=500,
                    detail=f"cannot read config for update: {error}",
                ) from error
            document: dict[str, object] = loaded if isinstance(loaded, dict) else {}
            raw_watches = document.get("watches", [])
            watches: list[dict[str, object]] = []
            if isinstance(raw_watches, list):
                for watch in raw_watches:
                    if isinstance(watch, dict):
                        watches.append({k: v for k, v in watch.items()})
            updated = transform(watches)
            document["watches"] = updated
            temp_path = config_path.with_suffix(
                f"{config_path.suffix}.tmp-{uuid.uuid4().hex}"
            )
            try:
                temp_path.write_text(yaml.dump(document, default_flow_style=False))
                config_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_path, config_path)
            except OSError as error:
                raise HTTPException(
                    status_code=500,
                    detail=f"cannot persist watch config: {error}",
                ) from error
            finally:
                if temp_path.exists():
                    temp_path.unlink()
        return updated

    def _data_roots_and_config_root() -> tuple[list[Path], Path]:
        if _runtime_watches:
            bp = _watch_snapshot()[0].boundary_policy
            config_root: Path = (
                bp.config_root if bp.config_root is not None else Path("/config")
            )
            return list(bp.data_roots), config_root
        if config_path is not None:
            try:
                document = yaml.safe_load(config_path.read_text()) or {}
                if isinstance(document, dict):
                    raw_roots = document.get("data_roots", [])
                    raw_config_root = document.get("config_root", config_path.parent)
                    if isinstance(raw_roots, list):
                        roots = [Path(value) for value in raw_roots if isinstance(value, str)]
                        config_root = Path(raw_config_root) if isinstance(raw_config_root, str) else config_path.parent
                        return roots, config_root
            except (OSError, yaml.YAMLError):
                pass
        return [], Path("/config")

    def _default_rules_path() -> Path:
        _, config_root = _data_roots_and_config_root()
        return config_root / "rules_.yaml"

    def _watch_form_context(
        data_roots: list[Path],
        *,
        errors: str | None = None,
        watch_id: object = "",
        root: object = "",
        folder: object = "/",
        rules_path: object = "",
    ) -> dict[str, object]:
        _, config_root = _data_roots_and_config_root()
        selected_root = root if isinstance(root, str) and root else (str(data_roots[0]) if data_roots else "")
        return {
            "data_roots": data_roots,
            "config_root": str(config_root),
            "errors": errors,
            "watch_id": watch_id if isinstance(watch_id, str) else "",
            "selected_root": selected_root,
            "folder": folder if isinstance(folder, str) else "/",
            "rules_path": rules_path if isinstance(rules_path, str) else "",
        }

    def _watch_form_response(
        request: Request,
        data_roots: list[Path],
        error: str,
        **values: object,
    ) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "watch_form.html",
            _watch_form_context(data_roots, errors=error, **values),
            status_code=422,
        )

    def _build_watches() -> list[dict[str, object]]:
        health_by_watch = _health_by_watch()
        watches = []
        for config in _watch_snapshot():
            accessible, detail = health_by_watch.get(config.watch_id, (True, ""))
            entries = (
                log_sink.read_recent(limit=1, watch=config.watch_id)
                if log_sink is not None
                else []
            )
            watches.append(
                {
                    "id": config.watch_id,
                    "root": config.watch_root,
                    "healthy": accessible
                    and not processor.validate_rules_document(
                        config.rules_path,
                        policy=config.boundary_policy,
                        watch_root=config.watch_root,
                    ),
                    "health_detail": detail,
                    "rule_count": _rule_count(config.rules_path),
                    "recent_activity": entries[-1] if entries else None,
                }
            )
        return watches

    def _require_watch(watch_id: str) -> WatchFolderConfig:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(
                status_code=404, detail="watch folder not configured"
            )
        return config

    def _validate_rules_text(watch_id: str, rules_text: str) -> list[str]:
        config = _require_watch(watch_id)
        temp_path = config.rules_path.with_suffix(
            f"{config.rules_path.suffix}.build-{uuid.uuid4().hex}"
        )
        try:
            temp_path.write_text(rules_text)
            return processor.validate_rules_document(
                temp_path,
                policy=config.boundary_policy,
                watch_root=config.watch_root,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _save_rules_text(
        watch_id: str, rules_text: str, expected_revision: str
    ) -> tuple[str, int, str]:
        """Validate and atomically save rules text; returns (message, status, revision)."""
        diagnostics = _validate_rules_text(watch_id, rules_text)
        if diagnostics:
            return ("; ".join(diagnostics), 422, expected_revision)
        rules_path = _require_watch(watch_id).rules_path
        temp_path = rules_path.with_suffix(
            f"{rules_path.suffix}.tmp-{uuid.uuid4().hex}"
        )
        try:
            with _RULE_SAVE_LOCK:
                try:
                    current_revision = hashlib.sha256(
                        rules_path.read_bytes()
                    ).hexdigest()
                except OSError as error:
                    return (
                        f"cannot read current rules: {error}",
                        500,
                        expected_revision,
                    )
                if current_revision != expected_revision:
                    return (
                        "Ruleset revision conflict. Reload before saving.",
                        409,
                        current_revision,
                    )
                rules_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_text(rules_text)
                os.replace(temp_path, rules_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return (
            f"Rules saved. Revision: {hashlib.sha256(rules_text.encode()).hexdigest()}",
            200,
            hashlib.sha256(rules_text.encode()).hexdigest(),
        )

    def _browse_context(path: str, data_roots: list[Path]) -> dict[str, object]:
        current = Path(path).resolve() if path else data_roots[0].resolve()
        current_root = next(
            (root for root in data_roots if _resolved_within(current, root)),
            None,
        )
        if current_root is None:
            raise HTTPException(
                status_code=422, detail="path outside data volumes"
            )
        if not current.is_dir():
            raise HTTPException(status_code=422, detail="path is not a directory")
        dirs: list[tuple[str, str]] = []
        try:
            children = sorted(current.iterdir(), key=lambda path: path.name.lower())
        except OSError as error:
            raise HTTPException(
                status_code=422, detail=f"cannot list directory: {error}"
            ) from error
        for child in children:
            if child.name.startswith(".") or not child.is_dir():
                continue
            resolved = child.resolve()
            if not _resolved_within(resolved, current_root):
                continue
            dirs.append((child.name, str(resolved)))
        relative = current.relative_to(current_root)
        crumb: list[tuple[str, str]] = [(current_root.name or str(current_root), str(current_root))]
        ancestor = current_root
        for part in relative.parts:
            ancestor = ancestor / part
            crumb.append((part, str(ancestor)))
        parent_path = (
            str(current.parent)
            if relative != Path(".") and _resolved_within(current.parent, current_root)
            else ""
        )
        return {
            "data_roots": data_roots,
            "current_root": str(current_root),
            "current": str(current),
            "relative": "" if relative == Path(".") else relative.as_posix(),
            "crumb": crumb,
            "dirs": dirs,
            "parent": parent_path,
        }

    @app.get("/browse/tree", response_model=None)
    def browse_tree(path: str = "") -> dict[str, object] | JSONResponse:
        data_roots, _ = _data_roots_and_config_root()
        if not data_roots:
            return JSONResponse(status_code=400, content={"error": "no data roots configured"})
        try:
            context = _browse_context(path, data_roots)
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"error": error.detail})
        roots = cast("list[Path]", context["data_roots"])
        crumb = cast("list[tuple[str, str]]", context["crumb"])
        dirs = cast("list[tuple[str, str]]", context["dirs"])
        return {
            "data_roots": [str(root) for root in roots],
            "current_root": context["current_root"],
            "current": context["current"],
            "relative": context["relative"],
            "parent": context["parent"],
            "crumb": [{"name": name, "path": path} for name, path in crumb],
            "dirs": [{"name": name, "path": path} for name, path in dirs],
        }

    @app.post("/browse/create", response_model=None)
    async def browse_create(request: Request) -> dict[str, object] | JSONResponse:
        payload = _form_or_json(await request.body())
        path = payload.get("path")
        name = payload.get("name")
        if not isinstance(path, str) or not path:
            return JSONResponse(status_code=422, content={"error": "missing parent path"})
        if (
            not isinstance(name, str)
            or not name.strip()
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
        ):
            return JSONResponse(status_code=422, content={"error": "invalid folder name"})
        data_roots, _ = _data_roots_and_config_root()
        if not data_roots:
            return JSONResponse(status_code=400, content={"error": "no data roots configured"})
        parent = Path(path)
        if not any(_resolved_within(parent, root) for root in data_roots):
            return JSONResponse(status_code=422, content={"error": "parent path is outside data volumes"})
        try:
            (parent / name).mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            return JSONResponse(status_code=422, content={"error": "a folder with that name already exists"})
        except OSError as error:
            return JSONResponse(status_code=500, content={"error": f"cannot create folder: {error}"})
        return {"ok": True, "path": str((parent / name).resolve())}


    @app.post("/watches/{watch_id}/rules/build/generate", response_model=None)
    def rule_build_generate(
        watch_id: str, body: dict[str, object] = Body(...)
    ) -> dict[str, object] | JSONResponse:
        _require_watch(watch_id)
        try:
            yaml_text = _model_to_rules_yaml(body.get("model", []))
        except (TypeError, ValueError) as error:
            return JSONResponse(status_code=422, content={"errors": [str(error)]})
        return {"yaml": yaml_text}

    @app.post("/watches/{watch_id}/rules/build/validate", response_model=None)
    def rule_build_validate(
        watch_id: str, body: dict[str, object] = Body(...)
    ) -> dict[str, object] | JSONResponse:
        _require_watch(watch_id)
        try:
            yaml_text = _model_to_rules_yaml(body.get("model", []))
        except (TypeError, ValueError) as error:
            return JSONResponse(
                status_code=422, content={"errors": [str(error)], "yaml": None}
            )
        return {"yaml": yaml_text, "errors": _validate_rules_text(watch_id, yaml_text)}

    @app.post("/watches/{watch_id}/rules/build/save", response_model=None)
    def rule_build_save(
        watch_id: str, body: dict[str, object] = Body(...)
    ) -> JSONResponse:
        _require_watch(watch_id)
        expected_revision = str(body.get("expected_revision", ""))
        try:
            yaml_text = _model_to_rules_yaml(body.get("model", []))
        except (TypeError, ValueError) as error:
            return JSONResponse(
                status_code=422,
                content={"errors": [str(error)], "yaml": None, "message": str(error)},
            )
        message, status, new_revision = _save_rules_text(
            watch_id, yaml_text, expected_revision
        )
        return JSONResponse(
            status_code=status,
            content={
                "message": message,
                "yaml": yaml_text,
                "revision": new_revision,
                "errors": [] if status < 400 else [message],
            },
        )

    @app.post("/watches/{watch_id}/rules/build/dry-run", response_model=None)
    def rule_build_dry_run(
        watch_id: str, body: dict[str, object] = Body(...)
    ) -> dict[str, object] | JSONResponse:
        config = _require_watch(watch_id)
        try:
            yaml_text = _model_to_rules_yaml(body.get("model", []))
        except (TypeError, ValueError) as error:
            return JSONResponse(status_code=422, content={"errors": [str(error)]})
        diagnostics = _validate_rules_text(watch_id, yaml_text)
        if diagnostics:
            return JSONResponse(status_code=422, content={"errors": diagnostics})
        item_value = body.get("item")
        if not isinstance(item_value, str) or not item_value:
            return JSONResponse(
                status_code=422, content={"errors": ["item path is required"]}
            )
        item = Path(item_value)
        try:
            stat = item.stat()
        except OSError as error:
            return JSONResponse(status_code=422, content={"errors": [str(error)]})
        temp_path = config.rules_path.with_suffix(
            f"{config.rules_path.suffix}.build-{uuid.uuid4().hex}"
        )
        try:
            temp_path.write_text(yaml_text)
            snapshot = ItemSnapshot(path=item, size=stat.st_size, mtime=stat.st_mtime)
            batch = processor.process_batch(
                watch_id=watch_id,
                watch_root=config.watch_root,
                rules_path=temp_path,
                snapshots=[snapshot],
                stability_interval=effective_stability_interval(config.watch_root, stability_interval),
                boundary_policy=config.boundary_policy,
                dry_run=True,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()
        if not batch.items:
            return {"detail": "No items processed.", "actions": []}
        batch_item = batch.items[0]
        if batch_item.report and batch_item.report.actions:
            return {
                "detail": "",
                "actions": [
                    {
                        "kind": action.kind,
                        "source": str(action.source),
                        "target": str(action.target),
                    }
                    for action in batch_item.report.actions
                ],
            }
        return {"detail": batch_item.detail, "actions": []}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        data_roots, _ = _data_roots_and_config_root()
        context = {"watches": _build_watches(), "data_roots": data_roots}
        if request.headers.get("HX-Request") == "true":
            return _fragment(request, "dashboard_content.html", **context)
        return _fragment(request, "dashboard.html", **context)

    @app.get("/watches/new", response_class=HTMLResponse)
    def new_watch_form(request: Request) -> HTMLResponse:
        data_roots, _ = _data_roots_and_config_root()
        return _fragment(
            request,
            "watch_form.html",
            **_watch_form_context(data_roots, rules_path=str(_default_rules_path())),
        )

    @app.get("/watches/{watch_id}/rules", response_class=HTMLResponse)
    def rule_editor(request: Request, watch_id: str) -> HTMLResponse:
        config = _require_watch(watch_id)
        try:
            rules = config.rules_path.read_text()
        except OSError as error:
            raise HTTPException(
                status_code=500, detail=f"cannot read rules: {error}"
            ) from error
        revision = hashlib.sha256(rules.encode()).hexdigest()
        return _fragment(
            request,
            "rule_editor.html",
            watch=config,
            rules=rules,
            revision=revision,
            model=_rules_yaml_to_model(rules),
        )

    @app.post("/watches/{watch_id}/rules/validate", response_class=HTMLResponse)
    def validate_rules(
        request: Request, watch_id: str, rules: str = Form()
    ) -> HTMLResponse:
        diagnostics = _validate_rules_text(watch_id, rules)
        if diagnostics:
            return HTMLResponse(
                _rules_feedback("; ".join(diagnostics), error=True), status_code=422
            )
        return HTMLResponse(_rules_feedback("Rules are valid."))

    @app.post("/watches/{watch_id}/rules/dry-run", response_class=HTMLResponse)
    def editor_dry_run(
        request: Request, watch_id: str, item: Path = Form(), rules: str = Form()
    ) -> HTMLResponse:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(status_code=404, detail="watch folder not configured")
        temp_path = config.rules_path.with_suffix(
            f"{config.rules_path.suffix}.preview-{uuid.uuid4().hex}"
        )
        try:
            temp_path.write_text(rules)
            diagnostics = processor.validate_rules_document(
                temp_path, policy=config.boundary_policy, watch_root=config.watch_root
            )
            if diagnostics:
                return HTMLResponse(
                    _rules_feedback("; ".join(diagnostics), error=True), status_code=422
                )
            try:
                stat = item.stat()
            except OSError as error:
                return HTMLResponse(
                    _rules_feedback(str(error), error=True), status_code=422
                )
            snapshot = ItemSnapshot(path=item, size=stat.st_size, mtime=stat.st_mtime)
            batch = processor.process_batch(
                watch_id=watch_id,
                watch_root=config.watch_root,
                rules_path=temp_path,
                snapshots=[snapshot],
                stability_interval=effective_stability_interval(config.watch_root, stability_interval),
                boundary_policy=config.boundary_policy,
                dry_run=True,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()
        if not batch.items:
            return HTMLResponse(_rules_feedback("No items processed."))
        batch_item = batch.items[0]
        if batch_item.report and batch_item.report.actions:
            rows = "".join(
                f"<li>{html.escape(action.kind)}: {html.escape(str(action.source))} to {html.escape(str(action.target))}</li>"
                for action in batch_item.report.actions
            )
            return HTMLResponse(
                f'<section class="preview"><h2>Dry run</h2><ul>{rows}</ul></section>'
            )
        detail = batch_item.detail
        if "no valid rule matched" in detail:
            return HTMLResponse(_rules_feedback("Dry run: No rule matched."))
        return HTMLResponse(_rules_feedback(f"Dry run: {html.escape(detail)}"))

    @app.post("/watches/{watch_id}/rules/save", response_class=HTMLResponse)
    def editor_save(
        request: Request,
        watch_id: str,
        rules: str = Form(),
        expected_revision: str = Form(),
    ) -> HTMLResponse:
        message, status, _ = _save_rules_text(watch_id, rules, expected_revision)
        return HTMLResponse(
            _rules_feedback(message, error=status >= 400), status_code=status
        )

    @app.get(
        "/watches/{watch_id}/dry-run", response_class=HTMLResponse, response_model=None
    )
    def dry_run(watch_id: str, item: Path) -> str | JSONResponse:
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        try:
            stat = item.stat()
        except OSError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        snapshot = ItemSnapshot(path=item, size=stat.st_size, mtime=stat.st_mtime)
        batch = processor.process_batch(
            watch_id=watch_id,
            watch_root=config.watch_root,
            rules_path=config.rules_path,
            snapshots=[snapshot],
            stability_interval=effective_stability_interval(config.watch_root, stability_interval),
            boundary_policy=config.boundary_policy,
            dry_run=True,
        )
        if not batch.items:
            return "<h1>Dry run: no items processed</h1>"
        batch_item = batch.items[0]
        if batch_item.report and batch_item.report.actions:
            rows = "".join(
                f"<li>{action.kind}: {action.source} to {action.target}</li>"
                for action in batch_item.report.actions
            )
            return f"<h1>Dry run: {watch_id}</h1><ul>{rows}</ul>"
        return f"<h1>Dry run: {watch_id}</h1><p>{html.escape(batch_item.detail)}</p>"

    @app.put("/watches/{watch_id}/rules", response_model=None)
    def save_rules(
        watch_id: str, expected_revision: str, body: bytes = Body(...)
    ) -> dict[str, str] | JSONResponse:
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        rules_path = config.rules_path
        try:
            processor._resolve_destination(rules_path)
        except ValueError as error:
            return JSONResponse(
                status_code=500, content={"detail": f"unsafe rules path: {error}"}
            )
        loaded = yaml.safe_load(body) or {}
        if not isinstance(loaded, dict) or not isinstance(
            loaded.get("rules", []), list
        ):
            return JSONResponse(
                status_code=422, content={"detail": "invalid rules document"}
            )
        temp_path = rules_path.with_suffix(
            f"{rules_path.suffix}.tmp-{uuid.uuid4().hex}"
        )
        try:
            temp_path.write_bytes(body)
            diagnostics = processor.validate_rules_document(
                temp_path, policy=config.boundary_policy, watch_root=config.watch_root
            )
            if diagnostics:
                return JSONResponse(
                    status_code=422, content={"detail": "; ".join(diagnostics)}
                )
            try:
                current_revision = hashlib.sha256(rules_path.read_bytes()).hexdigest()
            except OSError as error:
                return JSONResponse(
                    status_code=500,
                    content={"detail": f"cannot read current rules: {error}"},
                )
            if current_revision != expected_revision:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "ruleset revision conflict",
                        "revision": current_revision,
                    },
                )
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, rules_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return {"watch_id": watch_id, "revision": hashlib.sha256(body).hexdigest()}

    @app.get("/attempts", response_model=None)
    def list_attempts(
        request: Request, status: str = "", watch_id: str = ""
    ) -> list[dict[str, object]] | HTMLResponse | JSONResponse:
        if watch_id and _watch_config(watch_id) is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        statuses = (
            tuple(s.strip() for s in status.split(",") if s.strip()) if status else ()
        )
        summaries = review.list(AttemptFilters(statuses=statuses, watch_id=watch_id))
        if _is_html_request(request):
            return _fragment(
                request,
                "attempt_list.html",
                attempts=summaries,
                status=status,
                watch_id=watch_id,
                watches=_watch_snapshot(),
            )
        return [
            {
                "attempt_id": s.attempt_id,
                "watch_id": s.watch_id,
                "source_path": s.source_path,
                "source_fingerprint": s.source_fingerprint,
                "rule_name": s.rule_name,
                "status": s.status,
                "failure_detail": s.failure_detail,
                "created_at": s.created_at,
                "retry_of_attempt_id": s.retry_of_attempt_id,
            }
            for s in summaries
        ]

    @app.get("/attempts/{attempt_id}", response_model=None)
    def inspect_attempt(
        request: Request, attempt_id: str
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            details = review.inspect(attempt_id)
        except ValueError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        if _is_html_request(request):
            config = _watch_config(details.watch_id)
            return _fragment(
                request, "attempt_detail.html", attempt=details, watch=config
            )
        return _attempt_payload(details)

    @app.post("/attempts/{attempt_id}/accept", response_model=None)
    def accept_attempt(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            action_index = int(str(payload["action_index"]))
            resulting_path = str(payload["resulting_path"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            details = review.inspect(attempt_id)
        except ValueError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        config = _watch_config(details.watch_id)
        if config is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        try:
            result = review.command(
                attempt_id,
                Accept(
                    action_index=action_index,
                    resulting_path=resulting_path,
                    watch_root=config.watch_root,
                    boundary_policy=config.boundary_policy,
                ),
            )
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
        }

    @app.post("/attempts/{attempt_id}/abandon", response_model=None)
    def abandon_attempt(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            reason = str(payload.get("reason", ""))
        except (UnicodeDecodeError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, Abandon(reason=reason))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
        }

    @app.post("/attempts/{attempt_id}/mark-action-applied", response_model=None)
    def mark_action_applied(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            details = review.inspect(attempt_id)
            config = _watch_config(details.watch_id)
            if config is None:
                return JSONResponse(
                    status_code=404, content={"detail": "watch folder not configured"}
                )
            result = review.command(
                attempt_id,
                MarkActionApplied(
                    int(str(payload["action_index"])),
                    str(payload["resulting_path"]),
                    config.watch_root,
                    config.boundary_policy,
                ),
            )
        except (KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
        }

    @app.post("/attempts/{attempt_id}/retry-remaining", response_model=None)
    def retry_remaining(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            details = review.inspect(attempt_id)
            config = _watch_config(details.watch_id)
            if config is None:
                return JSONResponse(
                    status_code=404, content={"detail": "watch folder not configured"}
                )
            result = review.command(
                attempt_id,
                RetryRemaining(
                    int(str(payload["action_index"])),
                    str(payload["resulting_path"]),
                    config.watch_root,
                    config.boundary_policy,
                ),
            )
        except (KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.post("/attempts/{attempt_id}/reopen", response_model=None)
    def reopen_attempt(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            watch_id = str(payload["watch_id"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        try:
            result = review.command(
                attempt_id,
                Reopen(
                    watch_root=config.watch_root,
                    rules_path=config.rules_path,
                    boundary_policy=config.boundary_policy,
                ),
            )
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.post("/attempts/{attempt_id}/retry", response_model=None)
    def retry_attempt(
        request: Request, attempt_id: str, body: bytes = Body(...)
    ) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            watch_id = str(payload["watch_id"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(
                status_code=404, content={"detail": "watch folder not configured"}
            )
        try:
            result = review.command(
                attempt_id,
                RetryFromStart(
                    watch_root=config.watch_root,
                    rules_path=config.rules_path,
                    boundary_policy=config.boundary_policy,
                ),
            )
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(
                request, "command_feedback.html", result=result, attempt_id=attempt_id
            )
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.get("/logs", response_model=None)
    def get_logs(
        request: Request,
        watch: str = "",
        level: str = "",
        limit: int = 1000,
        start: str = "",
        end: str = "",
    ) -> list[dict[str, object]] | HTMLResponse:
        if log_sink is None:
            return []
        try:
            log_level = LogLevel(level) if level else None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid log level")
        entries = log_sink.read_recent(limit=None, watch=watch, level=log_level)
        if start:
            entries = [entry for entry in entries if entry.timestamp[:10] >= start]
        if end:
            entries = [entry for entry in entries if entry.timestamp[:10] <= end]
        entries = entries[-limit:]
        payload: list[dict[str, object]] = [
            {
                "timestamp": entry.timestamp,
                "level": entry.level.value,
                "watch": entry.watch,
                "rule": entry.rule,
                "action": entry.action,
                "item": entry.item,
                "result": entry.result.value,
                "detail": entry.detail,
            }
            for entry in entries
        ]
        if _is_html_request(request):
            return _fragment(
                request,
                "log_viewer.html",
                entries=payload,
                watch=watch,
                level=level,
                start=start,
                end=end,
                watches=_watch_snapshot(),
            )
        return payload

    @app.get("/health", response_model=None)
    def get_health() -> dict[str, object]:
        watches = _watch_snapshot()
        if health_checker is None or db_path is None or not watches:
            return {
                "all_healthy": True,
                "watch_folder_healths": [],
                "persistence_health": {"tracking_db_writable": True, "detail": ""},
            }
        folder_tuples = [
            (config.watch_id, config.watch_root) for config in watches
        ]
        overall = health_checker.check_all(watch_folders=folder_tuples, db_path=db_path)
        return {
            "all_healthy": overall.all_healthy,
            "watch_folder_healths": [
                {
                    "watch_id": h.watch_id,
                    "accessible": h.accessible,
                    "detail": h.detail,
                }
                for h in overall.watch_folder_healths
            ],
            "persistence_health": {
                "tracking_db_writable": overall.persistence_health.tracking_db_writable,
                "detail": overall.persistence_health.detail,
            },
        }

    @app.post("/watches", response_model=None)
    async def add_watch(
        request: Request,
    ) -> dict[str, object] | JSONResponse | HTMLResponse:
        body = await request.body()
        payload = _form_or_json(body)
        watch_id = payload.get("id")
        root = payload.get("root")
        rules_path = payload.get("rules_path")
        folder = payload.get("folder", "/")

        is_htmx = request.headers.get("HX-Request") == "true"

        if not isinstance(watch_id, str) or not watch_id:
            if is_htmx:
                data_roots, _ = _data_roots_and_config_root()
                return _watch_form_response(request, data_roots, "id is required", root=root, folder=folder, rules_path=rules_path)
            return JSONResponse(status_code=422, content={"detail": "id is required"})
        if not isinstance(root, str) or not root:
            if is_htmx:
                data_roots, _ = _data_roots_and_config_root()
                return _watch_form_response(request, data_roots, "root is required", watch_id=watch_id, folder=folder, rules_path=rules_path)
            return JSONResponse(status_code=422, content={"detail": "root is required"})
        data_roots, config_root = _data_roots_and_config_root()
        if not isinstance(folder, str) or not folder:
            folder = "/"
        if any(part == ".." for part in folder.split("/")):
            error = f"folder may not contain '..': {folder!r}"
            if is_htmx:
                return _watch_form_response(request, data_roots, error, watch_id=watch_id, root=root, folder=folder, rules_path=rules_path)
            return JSONResponse(status_code=422, content={"detail": error})
        root_path = (Path(root) / folder.lstrip("/")).resolve()
        if not isinstance(rules_path, str) or not rules_path:
            rules_path_value = config_root / f"rules_{watch_id}.yaml"
        else:
            rules_path_value = Path(rules_path)
            if not rules_path_value.is_absolute():
                rules_path_value = config_root / rules_path_value
        rules_path_value = rules_path_value.resolve()

        existing = _watch_snapshot()
        existing_ids = [w.watch_id for w in existing]
        existing_roots = [w.watch_root for w in existing]

        try:
            validate_watch_id(watch_id, existing_ids)
            validate_watch_root(
                root_path,
                config_root,
                tuple(data_roots),
                tuple(existing_roots),
                watch_id,
            )
        except ConfigError as e:
            if is_htmx:
                return _watch_form_response(request, data_roots, str(e), watch_id=watch_id, root=root, folder=folder, rules_path=rules_path)
            return JSONResponse(status_code=422, content={"detail": str(e)})

        try:
            rules_path_value.parent.mkdir(parents=True, exist_ok=True)
            if not rules_path_value.exists():
                rules_path_value.write_text("rules: []\n")
        except OSError as e:
            if is_htmx:
                return _watch_form_response(
                    request,
                    data_roots,
                    f"cannot create rules file: {e}",
                    watch_id=watch_id,
                    root=root,
                    folder=folder,
                    rules_path=rules_path,
                )
            return JSONResponse(
                status_code=500, content={"detail": f"cannot create rules file: {e}"}
            )

        new_config = WatchFolderConfig(
            watch_id=watch_id,
            watch_root=root_path,
            rules_path=rules_path_value,
            boundary_policy=existing[0].boundary_policy
            if existing
            else BoundaryPolicy(
                data_roots=tuple(data_roots),
                config_root=config_root,
                allowed_destinations=tuple(data_roots),
            ),
        )
        with _RUNTIME_WATCHES_LOCK:
            _runtime_watches.append(new_config)
            rebuild_boundary_policy(_runtime_watches)
            if watch_mutator is not None:
                watch_mutator.add_watch(_runtime_watches[-1])
            try:
                _update_watches_on_disk(
                    lambda watches: [
                        *watches,
                        {"id": watch_id, "root": str(root_path), "rules": str(rules_path_value)},
                    ]
                )
            except BaseException:
                _runtime_watches[:] = [
                    w for w in _runtime_watches if w.watch_id != watch_id
                ]
                rebuild_boundary_policy(_runtime_watches)
                if watch_mutator is not None:
                    watch_mutator.remove_watch(watch_id)
                raise

        if is_htmx:
            data_roots, _ = _data_roots_and_config_root()
            return _TEMPLATES.TemplateResponse(
                request,
                "dashboard_content.html",
                {"watches": _build_watches(), "data_roots": data_roots},
            )
        return {"id": watch_id, "root": str(root_path), "rules_path": str(rules_path_value)}

    @app.delete("/watches/{watch_id}", response_model=None)
    async def remove_watch(
        request: Request, watch_id: str
    ) -> dict[str, object] | JSONResponse | HTMLResponse:
        watch = next((w for w in _watch_snapshot() if w.watch_id == watch_id), None)
        if watch is None:
            if request.headers.get("HX-Request") == "true":
                return HTMLResponse(
                    f"<p>Watch folder not found: {html.escape(watch_id)}</p>",
                    status_code=404,
                )
            return JSONResponse(
                status_code=404,
                content={"detail": f"watch folder not found: {watch_id}"},
            )

        with _RUNTIME_WATCHES_LOCK:
            _runtime_watches[:] = [w for w in _runtime_watches if w.watch_id != watch_id]
            rebuild_boundary_policy(_runtime_watches)
            if watch_mutator is not None:
                watch_mutator.remove_watch(watch_id)
            try:
                _update_watches_on_disk(
                    lambda watches: [w for w in watches if w.get("id") != watch_id]
                )
            except BaseException:
                _runtime_watches.append(watch)
                rebuild_boundary_policy(_runtime_watches)
                if watch_mutator is not None:
                    watch_mutator.add_watch(watch)
                raise

        if request.headers.get("HX-Request") == "true":
            orphaned_rules = str(watch.rules_path) if watch.rules_path.exists() else ""
            return _fragment(
                request,
                "watch_list.html",
                watches=_build_watches(),
                orphaned_rules=orphaned_rules,
            )
        return {
            "id": watch_id,
            "root": str(watch.watch_root),
            "rules_path": str(watch.rules_path),
            "orphaned_rules_path": str(watch.rules_path)
            if watch.rules_path.exists()
            else None,
            "status": "removed",
        }

    return app
