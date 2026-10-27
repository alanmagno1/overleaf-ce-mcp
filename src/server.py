import asyncio
import base64
import contextlib
import datetime
import hmac
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import time
import urllib.parse
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pymongo
import redis
from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("overleaf-mcp")

# Configuration
API_KEY = os.environ.get(
    "OVERLEAF_MCP_API_KEY",
    "change-this-to-a-secure-random-key",
)
OVERLEAF_WEB_URL = os.environ.get("OVERLEAF_WEB_URL", "http://overleaf:80").rstrip("/")
OVERLEAF_API_URL = os.environ.get("OVERLEAF_API_URL", "http://overleaf:3000").rstrip("/")
OVERLEAF_MONGO_URL = os.environ.get(
    "OVERLEAF_MONGO_URL",
    "mongodb://overleaf-mongo:27017/sharelatex?replicaSet=overleaf",
)
OVERLEAF_REDIS_URL = os.environ.get("OVERLEAF_REDIS_URL", "redis://overleaf-redis:6379/0")
PUBLIC_BASE_URL = os.environ.get(
    "OVERLEAF_PUBLIC_BASE_URL",
    "https://overleaf.alanmagno.com/mcp-bridge",
).rstrip("/")
TAILSCALE_BASE_URL = os.environ.get(
    "OVERLEAF_TAILSCALE_BASE_URL",
    "http://100.65.13.127:8099",
).rstrip("/")
OVERLEAF_ADMIN_EMAIL = os.environ.get(
    "OVERLEAF_ADMIN_EMAIL",
    "admin@example.com",
).strip().lower()


def _iso_dt(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=datetime.timezone.utc)
        return val.isoformat()
    return str(val)


def _is_object_id(val: str) -> bool:
    return bool(val and re.fullmatch(r"[0-9a-fA-F]{24}", val.strip()))


def _parse_latex_log(log_text: str) -> Dict[str, Any]:
    """Extract structured errors, warnings, and tail from a LaTeX .log file."""
    if not log_text:
        return {"errors": [], "warnings": [], "log_tail": ""}

    lines = log_text.splitlines()
    errors: List[Dict[str, Any]] = []
    warnings: List[str] = []

    file_line_err_re = re.compile(r"^([^\s:]+\.(?:tex|sty|cls|bib|ltx|bbl)):(\d+):\s*(.+)$")
    line_num_re = re.compile(r"^l\.(\d+)\s*(.*)$")
    warning_re = re.compile(
        r"^(?:LaTeX|Package\s+[^\s]+|Class\s+[^\s]+|BibTeX|biber)\s+Warning:\s*(.+)$",
        re.IGNORECASE,
    )

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("! "):
            err_msg = line[2:].strip()
            err_line_num: Optional[int] = None
            context_lines: List[str] = []
            for j in range(i + 1, min(i + 10, n)):
                ctx = lines[j]
                context_lines.append(ctx)
                m_line = line_num_re.match(ctx)
                if m_line:
                    err_line_num = int(m_line.group(1))
                    break
            errors.append(
                {
                    "message": err_msg,
                    "line": err_line_num,
                    "context": "\n".join(context_lines[:6]),
                }
            )
        else:
            m_fl = file_line_err_re.match(line)
            if m_fl:
                errors.append(
                    {
                        "file": m_fl.group(1),
                        "line": int(m_fl.group(2)),
                        "message": m_fl.group(3).strip(),
                    }
                )
            else:
                m_warn = warning_re.match(line)
                if m_warn:
                    warnings.append(line.strip())
                elif "Undefined control sequence" in line or "Emergency stop" in line:
                    if not errors or errors[-1].get("message") != line.strip():
                        errors.append({"message": line.strip()})
        i += 1

    log_tail = "\n".join(lines[-80:])
    return {
        "errors": errors[:50],
        "warnings": warnings[:50],
        "log_tail": log_tail,
    }


class OverleafBridge:
    def __init__(self) -> None:
        self.mongo = pymongo.MongoClient(OVERLEAF_MONGO_URL, serverSelectionTimeoutMS=5000)
        self.db = self.mongo["sharelatex"]
        self.redis = redis.Redis.from_url(OVERLEAF_REDIS_URL, decode_responses=True)
        self.http = httpx.Client(timeout=180.0, follow_redirects=False)
        # Cache of {user_id: {"cookie": str, "csrf": str, "sid": str, "expires_at": float}}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        # Cache of latest compiled PDF bytes per project_id: {project_id: {"bytes": bytes, "build_id": str, "filename": str, "ts": float}}
        self._pdf_cache: Dict[str, Dict[str, Any]] = {}

    def _get_default_admin_user(self) -> Dict[str, Any]:
        user = self.db.users.find_one({"isAdmin": True})
        if not user:
            user = self.db.users.find_one({})
        if not user:
            raise RuntimeError("No users found in Overleaf MongoDB")
        return user

    def _get_user_for_project(self, project_doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if project_doc and project_doc.get("owner_ref"):
            owner_id = project_doc["owner_ref"]
            if not isinstance(owner_id, ObjectId) and _is_object_id(str(owner_id)):
                owner_id = ObjectId(str(owner_id))
            user = self.db.users.find_one({"_id": owner_id})
            if user:
                return user
        return self._get_default_admin_user()

    def _ensure_session(
        self,
        user: Optional[Dict[str, Any]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, str]:
        if user is None:
            user = self._get_default_admin_user()
        user_id = str(user["_id"])

        cached = self._sessions.get(user_id)
        now = time.time()
        if cached and not force_refresh and cached.get("expires_at", 0) > now:
            sid = cached["sid"]
            if self.redis.exists(f"sess:{sid}"):
                return {"cookie": cached["cookie"], "csrf": cached["csrf"], "user_id": user_id}

        # Step 1: Request GET /login from Overleaf Web to obtain a fresh signed overleaf.sid cookie & CSRF token
        resp = self.http.get(
            f"{OVERLEAF_WEB_URL}/login",
            headers={"X-Forwarded-Proto": "https"},
        )
        set_cookie = resp.headers.get("set-cookie", "")
        if "overleaf.sid=" not in set_cookie:
            raise RuntimeError(f"Failed to obtain overleaf.sid cookie from /login (status={resp.status_code})")

        cookie_pair = set_cookie.split(";")[0].strip()
        raw_val = urllib.parse.unquote(cookie_pair.split("=", 1)[1])
        if not raw_val.startswith("s:") or "." not in raw_val:
            raise RuntimeError(f"Unexpected overleaf.sid format: {raw_val}")
        sid = raw_val[2:].split(".", 1)[0]

        m_csrf = re.search(r'name="ol-csrfToken"\s+content="([^"]+)"', resp.text)
        if not m_csrf:
            raise RuntimeError("Failed to extract ol-csrfToken from Overleaf /login page")
        csrf_token = m_csrf.group(1)

        # Step 2: Inject authenticated passport.user into sess:<sid> in Overleaf Redis
        sess_raw = self.redis.get(f"sess:{sid}")
        if not sess_raw:
            sess_data: Dict[str, Any] = {
                "cookie": {
                    "originalMaxAge": 432000000,
                    "expires": (
                        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)
                    ).isoformat(),
                    "secure": True,
                    "httpOnly": True,
                    "path": "/",
                    "sameSite": "lax",
                },
                "validationToken": f"v1:{sid[-4:]}",
            }
        else:
            sess_data = json.loads(sess_raw)

        analytics_id = str(user.get("analyticsId") or user_id)
        sess_data["validationToken"] = f"v1:{sid[-4:]}"
        sess_data["justLoggedIn"] = True
        sess_data["analyticsId"] = analytics_id
        sess_data["passport"] = {
            "user": {
                "_id": user_id,
                "first_name": user.get("first_name", ""),
                "last_name": user.get("last_name", ""),
                "email": user.get("email", ""),
                "referal_id": user.get("referal_id", ""),
                "session_created": _iso_dt(datetime.datetime.now(datetime.timezone.utc)),
                "ip_address": "127.0.0.1",
                "must_reconfirm": False,
                "analyticsId": analytics_id,
                "labsProgram": bool(user.get("labsProgram", False)),
                "isAdmin": bool(user.get("isAdmin", False)),
                "adminRoles": user.get("adminRoles", []),
            }
        }

        ttl_seconds = 5 * 24 * 3600
        self.redis.setex(f"sess:{sid}", ttl_seconds, json.dumps(sess_data))

        session_info = {
            "cookie": cookie_pair,
            "csrf": csrf_token,
            "sid": sid,
            "user_id": user_id,
            "expires_at": now + 3600,
        }
        self._sessions[user_id] = session_info
        logger.info("Initialized authenticated Overleaf session for user %s (%s)", user.get("email"), user_id)
        return {"cookie": cookie_pair, "csrf": csrf_token, "user_id": user_id}

    def _web_request(
        self,
        method: str,
        path: str,
        project_doc: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        user = self._get_user_for_project(project_doc)
        for attempt in range(2):
            sess = self._ensure_session(user=user, force_refresh=(attempt > 0))
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Cookie"] = sess["cookie"]
            headers["X-Forwarded-Proto"] = "https"
            if method.upper() not in ("GET", "HEAD", "OPTIONS"):
                headers["X-Csrf-Token"] = sess["csrf"]
            url = f"{OVERLEAF_WEB_URL}{path}" if path.startswith("/") else f"{OVERLEAF_WEB_URL}/{path}"
            resp = self.http.request(method, url, headers=headers, **kwargs)
            if resp.status_code in (401, 403) or (
                resp.status_code in (301, 302, 303, 307)
                and "/login" in resp.headers.get("location", "")
            ):
                if attempt == 0:
                    continue
            return resp
        return resp

    def _get_project_doc(self, project_id: str) -> Dict[str, Any]:
        if not _is_object_id(project_id):
            # Also allow looking up by exact project name if not a 24-hex ObjectId
            proj = self.db.projects.find_one({"name": project_id})
            if not proj:
                raise ValueError(f"Invalid project_id or project not found: {project_id}")
            return proj
        proj = self.db.projects.find_one({"_id": ObjectId(project_id)})
        if not proj:
            raise ValueError(f"Project not found: {project_id}")
        return proj

    def list_projects(self) -> Dict[str, Any]:
        cursor = self.db.projects.find(
            {},
            {
                "name": 1,
                "lastUpdated": 1,
                "compiler": 1,
                "rootDoc_id": 1,
                "owner_ref": 1,
                "archived": 1,
                "trashed": 1,
                "rootFolder._id": 1,
            },
        ).sort("lastUpdated", pymongo.DESCENDING)

        projects: List[Dict[str, Any]] = []
        for p in cursor:
            root_folders = p.get("rootFolder") or []
            root_folder_id = str(root_folders[0]["_id"]) if root_folders and "_id" in root_folders[0] else None
            projects.append(
                {
                    "project_id": str(p["_id"]),
                    "name": p.get("name", ""),
                    "lastUpdated": _iso_dt(p.get("lastUpdated")),
                    "compiler": p.get("compiler", "pdflatex"),
                    "rootDoc_id": str(p["rootDoc_id"]) if p.get("rootDoc_id") else None,
                    "root_folder_id": root_folder_id,
                    "owner_id": str(p["owner_ref"]) if p.get("owner_ref") else None,
                    "archived": bool(p.get("archived")),
                    "trashed": bool(p.get("trashed")),
                }
            )
        return {"count": len(projects), "projects": projects}

    def get_project_files(self, project_id: str) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        root_doc_id = str(proj["rootDoc_id"]) if proj.get("rootDoc_id") else None
        root_folders = proj.get("rootFolder") or []
        if not root_folders:
            raise RuntimeError(f"Project {real_project_id} has no rootFolder")

        docs_flat: List[Dict[str, Any]] = []
        files_flat: List[Dict[str, Any]] = []
        folders_flat: List[Dict[str, Any]] = []

        def walk_folder(
            folder_obj: Dict[str, Any],
            current_path: str,
            parent_folder_id: Optional[str],
        ) -> Dict[str, Any]:
            f_id = str(folder_obj["_id"])
            f_name = folder_obj.get("name", "")
            folders_flat.append(
                {
                    "folder_id": f_id,
                    "name": f_name,
                    "path": current_path,
                    "parent_folder_id": parent_folder_id,
                }
            )

            folder_docs: List[Dict[str, Any]] = []
            for d in folder_obj.get("docs") or []:
                if not d or "_id" not in d:
                    continue
                d_id = str(d["_id"])
                d_name = d.get("name", "")
                d_path = posixpath.join(current_path, d_name)
                doc_item = {
                    "doc_id": d_id,
                    "name": d_name,
                    "path": d_path,
                    "folder_id": f_id,
                    "folder_path": current_path,
                    "is_root_doc": (d_id == root_doc_id),
                }
                folder_docs.append(doc_item)
                docs_flat.append(doc_item)

            folder_files: List[Dict[str, Any]] = []
            for f in folder_obj.get("fileRefs") or []:
                if not f or "_id" not in f:
                    continue
                file_id = str(f["_id"])
                file_name = f.get("name", "")
                file_path = posixpath.join(current_path, file_name)
                file_item = {
                    "file_id": file_id,
                    "name": file_name,
                    "path": file_path,
                    "folder_id": f_id,
                    "folder_path": current_path,
                    "created": _iso_dt(f.get("created")),
                    "hash": f.get("hash"),
                }
                folder_files.append(file_item)
                files_flat.append(file_item)

            subfolders: List[Dict[str, Any]] = []
            for sub in folder_obj.get("folders") or []:
                if not sub or "_id" not in sub:
                    continue
                sub_name = sub.get("name", "")
                sub_path = posixpath.join(current_path, sub_name)
                subfolders.append(walk_folder(sub, sub_path, f_id))

            return {
                "folder_id": f_id,
                "name": f_name,
                "path": current_path,
                "docs": folder_docs,
                "files": folder_files,
                "folders": subfolders,
            }

        tree = walk_folder(root_folders[0], "/", None)
        return {
            "project_id": real_project_id,
            "name": proj.get("name", ""),
            "compiler": proj.get("compiler", "pdflatex"),
            "root_doc_id": root_doc_id,
            "root_folder_id": str(root_folders[0]["_id"]),
            "lastUpdated": _iso_dt(proj.get("lastUpdated")),
            "docs": docs_flat,
            "files": files_flat,
            "folders": folders_flat,
            "tree": tree,
        }

    def _resolve_entity(self, project_id: str, path_or_doc_id: str) -> Dict[str, Any]:
        manifest = self.get_project_files(project_id)
        target = path_or_doc_id.strip()
        norm_path = "/" + target.lstrip("/")

        # 1. Exact doc_id match
        for d in manifest["docs"]:
            if d["doc_id"] == target:
                return {"entity_type": "doc", **d}

        # 2. Exact path match on docs
        for d in manifest["docs"]:
            if d["path"] == norm_path:
                return {"entity_type": "doc", **d}

        # 3. Exact file_id or path match on binary files
        for f in manifest["files"]:
            if f["file_id"] == target or f["path"] == norm_path:
                return {"entity_type": "file", **f}

        # 4. Unique basename match on docs
        basename_matches = [d for d in manifest["docs"] if d["name"] == target]
        if len(basename_matches) == 1:
            return {"entity_type": "doc", **basename_matches[0]}

        # 5. Unique basename match on binary files
        file_basename_matches = [f for f in manifest["files"] if f["name"] == target]
        if len(file_basename_matches) == 1:
            return {"entity_type": "file", **file_basename_matches[0]}

        raise FileNotFoundError(
            f"File or doc '{path_or_doc_id}' not found in project {manifest['project_id']}. "
            f"Available docs: {[d['path'] for d in manifest['docs']]}"
        )

    def read_file(self, project_id: str, path_or_doc_id: str) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        entity = self._resolve_entity(real_project_id, path_or_doc_id)

        if entity["entity_type"] == "doc":
            doc_id = entity["doc_id"]
            # Read live content via Overleaf Web's DocumentUpdaterController endpoint
            # (GET /Project/:Project_id/doc/:Doc_id/download), which queries document-updater (port 3003)
            resp = self._web_request(
                "GET",
                f"/Project/{real_project_id}/doc/{doc_id}/download",
                project_doc=proj,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to read doc {doc_id} from Overleaf (status={resp.status_code}): {resp.text[:300]}"
                )
            content = resp.text
            return {
                "project_id": real_project_id,
                "doc_id": doc_id,
                "name": entity["name"],
                "path": entity["path"],
                "folder_id": entity["folder_id"],
                "is_binary": False,
                "line_count": len(content.splitlines()),
                "content": content,
            }
        else:
            file_id = entity["file_id"]
            resp = self._web_request(
                "GET",
                f"/Project/{real_project_id}/file/{file_id}",
                project_doc=proj,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to read binary file {file_id} from Overleaf (status={resp.status_code})"
                )
            raw_bytes = resp.content
            return {
                "project_id": real_project_id,
                "file_id": file_id,
                "name": entity["name"],
                "path": entity["path"],
                "folder_id": entity["folder_id"],
                "is_binary": True,
                "size_bytes": len(raw_bytes),
                "content_base64": base64.b64encode(raw_bytes).decode("ascii"),
            }

    def write_file(
        self,
        project_id: str,
        folder_id: Optional[str],
        filename: str,
        content: str,
        is_base64: bool = False,
    ) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        manifest = self.get_project_files(real_project_id)
        root_folder_id = manifest["root_folder_id"]

        # Normalize folder_id & filename (support nested paths in filename or path in folder_id)
        clean_filename = filename.strip().lstrip("/")
        if not clean_filename:
            raise ValueError("filename cannot be empty")

        folder_by_id = {f["folder_id"]: f for f in manifest["folders"]}
        folder_by_path = {f["path"]: f for f in manifest["folders"]}

        target_folder_id = root_folder_id
        if folder_id and folder_id.strip() and folder_id.strip().lower() not in ("root", "/", "rootfolder"):
            f_arg = folder_id.strip()
            if f_arg in folder_by_id:
                target_folder_id = f_arg
            else:
                norm_f_path = "/" + f_arg.lstrip("/").rstrip("/")
                if norm_f_path in folder_by_path:
                    target_folder_id = folder_by_path[norm_f_path]["folder_id"]
                elif _is_object_id(f_arg):
                    target_folder_id = f_arg
                else:
                    # Treat folder_id as a relative folder path prefix
                    clean_filename = posixpath.join(f_arg.strip("/"), clean_filename)
                    target_folder_id = root_folder_id

        relative_path_field: Optional[str] = None
        if "/" in clean_filename:
            subdir, base_name = posixpath.split(clean_filename)
            base_folder_path = folder_by_id.get(target_folder_id, {}).get("path", "/")
            full_target_folder_path = posixpath.normpath(posixpath.join(base_folder_path, subdir))
            if full_target_folder_path in folder_by_path:
                target_folder_id = folder_by_path[full_target_folder_path]["folder_id"]
                clean_filename = base_name
            else:
                # Overleaf's uploadFile endpoint supports `relativePath` and automatically calls EditorController.mkdirp
                relative_path_field = clean_filename
                clean_filename = base_name

        if is_base64:
            file_bytes = base64.b64decode(content)
        else:
            file_bytes = content.encode("utf-8")

        mime_type, _ = mimetypes.guess_type(clean_filename)
        if not mime_type:
            mime_type = "application/octet-stream" if is_base64 else "text/plain; charset=utf-8"

        form_data: Dict[str, str] = {"name": clean_filename}
        if relative_path_field:
            form_data["relativePath"] = relative_path_field

        files = {
            "qqfile": (clean_filename, io.BytesIO(file_bytes), mime_type),
        }

        resp = self._web_request(
            "POST",
            f"/Project/{real_project_id}/upload?folder_id={target_folder_id}",
            project_doc=proj,
            data=form_data,
            files=files,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"Overleaf upload failed (status={resp.status_code}): {resp.text[:500]}"
            )

        res_json = resp.json()
        if not res_json.get("success"):
            raise RuntimeError(f"Overleaf upload returned error: {res_json}")

        # Refresh project files to report the canonical path and folder_id
        updated_manifest = self.get_project_files(real_project_id)
        entity_id = res_json.get("entity_id")
        entity_type = res_json.get("entity_type", "doc")
        resolved_path = None
        resolved_folder_id = target_folder_id

        if entity_id:
            for d in updated_manifest["docs"]:
                if d["doc_id"] == entity_id:
                    resolved_path = d["path"]
                    resolved_folder_id = d["folder_id"]
                    break
            if not resolved_path:
                for f in updated_manifest["files"]:
                    if f["file_id"] == entity_id:
                        resolved_path = f["path"]
                        resolved_folder_id = f["folder_id"]
                        break

        return {
            "success": True,
            "project_id": real_project_id,
            "folder_id": resolved_folder_id,
            "filename": clean_filename,
            "path": resolved_path or f"/{clean_filename}",
            "entity_id": entity_id,
            "entity_type": entity_type,
            "size_bytes": len(file_bytes),
        }

    def patch_file(
        self,
        project_id: str,
        path_or_doc_id: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> Dict[str, Any]:
        if not old_str:
            raise ValueError("old_str cannot be empty")

        doc_info = self.read_file(project_id, path_or_doc_id)
        if doc_info.get("is_binary"):
            raise ValueError(f"Cannot patch binary file: {doc_info.get('path')}")

        content: str = doc_info["content"]
        count = content.count(old_str)
        if count == 0:
            raise ValueError(
                f"old_str not found in {doc_info['path']} (doc_id={doc_info['doc_id']}). "
                "Read the file first to verify exact text and whitespace."
            )
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_str matched {count} times in {doc_info['path']}. "
                "Provide more surrounding context in old_str or set replace_all=True."
            )

        if replace_all:
            updated_content = content.replace(old_str, new_str)
            replaced_count = count
        else:
            updated_content = content.replace(old_str, new_str, 1)
            replaced_count = 1

        write_res = self.write_file(
            project_id=doc_info["project_id"],
            folder_id=doc_info["folder_id"],
            filename=doc_info["name"],
            content=updated_content,
            is_base64=False,
        )

        return {
            "success": True,
            "project_id": doc_info["project_id"],
            "doc_id": doc_info["doc_id"],
            "name": doc_info["name"],
            "path": doc_info["path"],
            "folder_id": doc_info["folder_id"],
            "replacements": replaced_count,
            "new_line_count": len(updated_content.splitlines()),
            "entity_id": write_res.get("entity_id"),
        }

    def create_folder(
        self,
        project_id: str,
        parent_folder_id: Optional[str],
        name: str,
    ) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        manifest = self.get_project_files(real_project_id)
        if not parent_folder_id or parent_folder_id.strip().lower() in ("", "root", "/", "rootfolder"):
            parent_folder_id = manifest["root_folder_id"]

        resp = self._web_request(
            "POST",
            f"/project/{real_project_id}/folder",
            project_doc=proj,
            json={"parent_folder_id": parent_folder_id, "name": name.strip()},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to create folder '{name}' (status={resp.status_code}): {resp.text[:300]}"
            )
        folder_data = resp.json()
        return {
            "success": True,
            "project_id": real_project_id,
            "folder_id": folder_data.get("_id"),
            "name": folder_data.get("name", name),
            "parent_folder_id": parent_folder_id,
        }

    def delete_entity(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
    ) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        etype = entity_type.strip().lower()
        if etype not in ("doc", "file", "folder"):
            raise ValueError("entity_type must be one of: 'doc', 'file', 'folder'")

        resp = self._web_request(
            "DELETE",
            f"/project/{real_project_id}/{etype}/{entity_id.strip()}",
            project_doc=proj,
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to delete {etype} {entity_id} (status={resp.status_code}): {resp.text[:300]}"
            )
        return {
            "success": True,
            "project_id": real_project_id,
            "entity_type": etype,
            "entity_id": entity_id.strip(),
        }

    def compile_project(
        self,
        project_id: str,
        root_doc_id: Optional[str] = None,
        compiler: Optional[str] = None,
        draft: bool = False,
    ) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])

        payload: Dict[str, Any] = {
            "incrementalCompilesEnabled": True,
            "stopOnFirstError": False,
            "draft": bool(draft),
        }
        if root_doc_id:
            payload["rootDoc_id"] = root_doc_id
        if compiler:
            payload["compiler"] = compiler

        resp = self._web_request(
            "POST",
            f"/project/{real_project_id}/compile",
            project_doc=proj,
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Compile request failed (status={resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()
        overleaf_status = data.get("status", "error")
        output_files = data.get("outputFiles") or []

        log_entry = next((f for f in output_files if f.get("path") == "output.log"), None)
        pdf_entry = next((f for f in output_files if f.get("path") == "output.pdf"), None)
        build_id = (
            (pdf_entry or log_entry or {}).get("build")
            or (output_files[0].get("build") if output_files else None)
        )

        log_text = ""
        if log_entry and log_entry.get("url"):
            log_resp = self._web_request("GET", log_entry["url"], project_doc=proj)
            if log_resp.status_code == 200:
                log_text = log_resp.text

        parsed_log = _parse_latex_log(log_text)

        pdf_available = False
        pdf_size_bytes = 0
        if pdf_entry and pdf_entry.get("url"):
            pdf_resp = self._web_request("GET", pdf_entry["url"], project_doc=proj)
            if pdf_resp.status_code == 200 and pdf_resp.content:
                pdf_available = True
                pdf_size_bytes = len(pdf_resp.content)
                safe_name = re.sub(r"[^\w\-]+", "_", proj.get("name", "document")).strip("_") or "document"
                self._pdf_cache[real_project_id] = {
                    "bytes": pdf_resp.content,
                    "build_id": build_id,
                    "filename": f"{safe_name}.pdf",
                    "log_text": log_text,
                    "ts": time.time(),
                }

        normalized_status = "success" if overleaf_status == "success" and pdf_available else "error"

        pdf_public_url = (
            f"{PUBLIC_BASE_URL}/api/projects/{real_project_id}/pdf?api_key={API_KEY}"
            if pdf_available
            else None
        )
        pdf_tailscale_url = (
            f"{TAILSCALE_BASE_URL}/api/projects/{real_project_id}/pdf?api_key={API_KEY}"
            if pdf_available
            else None
        )

        return {
            "status": normalized_status,
            "overleaf_status": overleaf_status,
            "project_id": real_project_id,
            "project_name": proj.get("name", ""),
            "build_id": build_id,
            "pdf_available": pdf_available,
            "pdf_size_bytes": pdf_size_bytes,
            "pdf_url": pdf_public_url,
            "pdf_tailscale_url": pdf_tailscale_url,
            "error_count": len(parsed_log["errors"]),
            "warning_count": len(parsed_log["warnings"]),
            "errors": parsed_log["errors"],
            "warnings": parsed_log["warnings"],
            "validation_problems": data.get("validationProblems"),
            "timings": data.get("timings"),
            "stats": data.get("stats"),
            "output_files": [
                {"path": f.get("path"), "type": f.get("type"), "size": f.get("size")}
                for f in output_files
            ],
            "log_tail": parsed_log["log_tail"],
        }

    def get_compiled_pdf(self, project_id: str) -> Dict[str, Any]:
        proj = self._get_project_doc(project_id)
        real_project_id = str(proj["_id"])
        if real_project_id not in self._pdf_cache:
            self.compile_project(real_project_id)
        cached = self._pdf_cache.get(real_project_id)
        if not cached or not cached.get("bytes"):
            raise RuntimeError(f"No compiled PDF available for project {real_project_id}")
        return cached


bridge = OverleafBridge()

# Configure FastMCP (supports both Streamable HTTP /mcp and SSE /sse + /messages/)
try:
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_server = FastMCP(
        "overleaf-mcp-bridge",
        stateless_http=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
except Exception:
    mcp_server = FastMCP("overleaf-mcp-bridge", stateless_http=True, host="0.0.0.0")


@mcp_server.tool(
    name="list_projects",
    description="Devuelve nombre, project_id, compiler, rootDoc_id, root_folder_id y fecha de modificación (lastUpdated) de todos los proyectos en Overleaf.",
)
def mcp_list_projects() -> Dict[str, Any]:
    return bridge.list_projects()


@mcp_server.tool(
    name="get_project_files",
    description="Devuelve el árbol completo de carpetas (tree), doc_id de archivos de texto (.tex, .bib, .cls) y file_id de archivos binarios, junto con sus folder_id y rutas.",
)
def mcp_get_project_files(project_id: str) -> Dict[str, Any]:
    return bridge.get_project_files(project_id)


@mcp_server.tool(
    name="read_file",
    description="Devuelve el contenido en texto plano del archivo .tex / .bib / .cls (sincronizado en vivo con document-updater) o el contenido en base64 si es un archivo binario. Acepta doc_id o ruta (ej. 'main.tex' o '/sections/intro.tex').",
)
def mcp_read_file(project_id: str, path_or_doc_id: str) -> Dict[str, Any]:
    return bridge.read_file(project_id, path_or_doc_id)


@mcp_server.tool(
    name="write_file",
    description="Crea o actualiza el contenido completo de un archivo .tex / .bib / .cls (o archivo binario/imagen si is_base64=True) vía Overleaf upload/document-updater sin romper la sesión en vivo en el navegador. folder_id puede ser el ID de la carpeta, 'root' / '/' para la carpeta raíz, o una ruta de carpeta.",
)
def mcp_write_file(
    project_id: str,
    folder_id: str,
    filename: str,
    content: str,
    is_base64: bool = False,
) -> Dict[str, Any]:
    return bridge.write_file(
        project_id=project_id,
        folder_id=folder_id,
        filename=filename,
        content=content,
        is_base64=is_base64,
    )


@mcp_server.tool(
    name="patch_file",
    description="Reemplaza un bloque exacto de texto (old_str por new_str) dentro de un archivo .tex / .bib y lo guarda en vivo sincronizado con document-updater.",
)
def mcp_patch_file(
    project_id: str,
    path_or_doc_id: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> Dict[str, Any]:
    return bridge.patch_file(
        project_id=project_id,
        path_or_doc_id=path_or_doc_id,
        old_str=old_str,
        new_str=new_str,
        replace_all=replace_all,
    )


@mcp_server.tool(
    name="compile_project",
    description="Ejecuta la compilación LaTeX en Overleaf y devuelve el estado ('success' / 'error'), los errores y warnings extraídos del .log, y las URLs de descarga del PDF generado.",
)
def mcp_compile_project(
    project_id: str,
    root_doc_id: Optional[str] = None,
    compiler: Optional[str] = None,
    draft: bool = False,
) -> Dict[str, Any]:
    return bridge.compile_project(
        project_id=project_id,
        root_doc_id=root_doc_id,
        compiler=compiler,
        draft=draft,
    )


@mcp_server.tool(
    name="create_folder",
    description="Crea una nueva carpeta dentro de un proyecto de Overleaf. parent_folder_id puede ser el ID de la carpeta padre o 'root' para la raíz.",
)
def mcp_create_folder(project_id: str, parent_folder_id: str, name: str) -> Dict[str, Any]:
    return bridge.create_folder(project_id, parent_folder_id, name)


@mcp_server.tool(
    name="delete_entity",
    description="Elimina un documento ('doc'), archivo binario ('file') o carpeta ('folder') de un proyecto de Overleaf.",
)
def mcp_delete_entity(project_id: str, entity_type: str, entity_id: str) -> Dict[str, Any]:
    return bridge.delete_entity(project_id, entity_type, entity_id)


# Build MCP ASGI sub-apps
streamable_http_app = mcp_server.streamable_http_app()
sse_app = mcp_server.sse_app()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(
    title="Overleaf MCP & REST API Bridge",
    version="1.0.0",
    description="MCP Server (Streamable HTTP + SSE) & REST API Bridge for Self-Hosted Overleaf CE",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_token_from_scope(scope: Dict[str, Any]) -> Optional[str]:
    headers = {
        k.decode("latin1").lower(): v.decode("latin1")
        for k, v in (scope.get("headers") or [])
    }
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if "x-api-key" in headers:
        return headers["x-api-key"].strip()
    qs = scope.get("query_string", b"").decode("latin1")
    if qs:
        params = urllib.parse.parse_qs(qs)
        if "api_key" in params and params["api_key"]:
            return params["api_key"][0].strip()
    return None


def _is_valid_token(token: Optional[str]) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8"))


# Request models for REST endpoints
class WriteFileRequest(BaseModel):
    folder_id: Optional[str] = Field(default="root", description="Target folder_id, 'root', or folder path")
    filename: str = Field(..., description="File name, e.g. 'main.tex' or 'sections/intro.tex'")
    content: str = Field(..., description="UTF-8 text content or Base64 string if is_base64=True")
    is_base64: bool = Field(default=False, description="True if content is Base64-encoded binary data")


class PatchFileRequest(BaseModel):
    path_or_doc_id: str = Field(..., description="Document ID or file path, e.g. 'main.tex'")
    old_str: str = Field(..., description="Exact text block to find")
    new_str: str = Field(..., description="Replacement text block")
    replace_all: bool = Field(default=False, description="Replace all occurrences if True")


class CompileProjectRequest(BaseModel):
    root_doc_id: Optional[str] = None
    compiler: Optional[str] = None
    draft: bool = False


class CreateFolderRequest(BaseModel):
    parent_folder_id: Optional[str] = "root"
    name: str


@app.get("/health")
@app.get("/api/health")
def health_check() -> Dict[str, Any]:
    return {"status": "ok", "service": "overleaf-mcp-bridge"}


@app.get("/api/projects")
def api_list_projects() -> Dict[str, Any]:
    try:
        return bridge.list_projects()
    except Exception as e:
        logger.exception("Error listing projects")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/projects/{project_id}/files")
def api_get_project_files(project_id: str) -> Dict[str, Any]:
    try:
        return bridge.get_project_files(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Error getting project files")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/projects/{project_id}/file")
def api_read_file(
    project_id: str,
    path_or_doc_id: str = Query(..., description="Path (e.g. main.tex) or doc_id"),
) -> Dict[str, Any]:
    try:
        return bridge.read_file(project_id, path_or_doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error reading file")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/{project_id}/file")
@app.put("/api/projects/{project_id}/file")
def api_write_file(project_id: str, body: WriteFileRequest) -> Dict[str, Any]:
    try:
        return bridge.write_file(
            project_id=project_id,
            folder_id=body.folder_id,
            filename=body.filename,
            content=body.content,
            is_base64=body.is_base64,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error writing file")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/projects/{project_id}/file")
@app.post("/api/projects/{project_id}/file/patch")
def api_patch_file(project_id: str, body: PatchFileRequest) -> Dict[str, Any]:
    try:
        return bridge.patch_file(
            project_id=project_id,
            path_or_doc_id=body.path_or_doc_id,
            old_str=body.old_str,
            new_str=body.new_str,
            replace_all=body.replace_all,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error patching file")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/{project_id}/compile")
def api_compile_project(
    project_id: str,
    body: Optional[CompileProjectRequest] = None,
) -> Dict[str, Any]:
    try:
        req = body or CompileProjectRequest()
        return bridge.compile_project(
            project_id=project_id,
            root_doc_id=req.root_doc_id,
            compiler=req.compiler,
            draft=req.draft,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Error compiling project")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/projects/{project_id}/pdf")
def api_download_pdf(project_id: str) -> Response:
    try:
        cached = bridge.get_compiled_pdf(project_id)
        return Response(
            content=cached["bytes"],
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{cached["filename"]}"',
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        logger.exception("Error downloading PDF")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/projects/{project_id}/log")
def api_download_log(project_id: str) -> Response:
    try:
        cached = bridge.get_compiled_pdf(project_id)
        return PlainTextResponse(content=cached.get("log_text", ""))
    except Exception as e:
        logger.exception("Error downloading log")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/{project_id}/folder")
def api_create_folder(project_id: str, body: CreateFolderRequest) -> Dict[str, Any]:
    try:
        return bridge.create_folder(project_id, body.parent_folder_id, body.name)
    except Exception as e:
        logger.exception("Error creating folder")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/projects/{project_id}/{entity_type}/{entity_id}")
def api_delete_entity(project_id: str, entity_type: str, entity_id: str) -> Dict[str, Any]:
    try:
        return bridge.delete_entity(project_id, entity_type, entity_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error deleting entity")
        raise HTTPException(status_code=500, detail=str(e))


class UnifiedASGIGateway:
    """
    ASGI router that:
    1. Strips optional '/mcp-bridge' path prefix so reverse-proxied and direct requests work identically.
    2. Enforces Bearer / API Key authentication on all endpoints except /health and /api/health.
    3. Routes '/mcp' to FastMCP Streamable HTTP app, '/sse' & '/messages' to FastMCP SSE app,
       and everything else to FastAPI REST app.
    """

    def __init__(self, fastapi_app: FastAPI, mcp_http_app: Any, mcp_sse_app: Any) -> None:
        self.fastapi_app = fastapi_app
        self.mcp_http_app = mcp_http_app
        self.mcp_sse_app = mcp_sse_app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.fastapi_app(scope, receive, send)
            return

        path: str = scope.get("path", "/")
        if path == "/mcp-bridge":
            path = "/"
        elif path.startswith("/mcp-bridge/"):
            path = path[len("/mcp-bridge") :]

        scope = dict(scope)
        scope["path"] = path
        scope["raw_path"] = path.encode("latin1")

        # Allow unauthenticated health checks and CORS preflight
        method = scope.get("method", "GET").upper()
        if method != "OPTIONS" and path not in ("/health", "/api/health"):
            token = _extract_token_from_scope(scope)
            if not _is_valid_token(token):
                resp = JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized: missing or invalid Bearer API Key"},
                )
                await resp(scope, receive, send)
                return

        if path == "/mcp" or path.startswith("/mcp/"):
            if path == "/mcp/":
                scope["path"] = "/mcp"
                scope["raw_path"] = b"/mcp"
            await self.mcp_http_app(scope, receive, send)
        elif path == "/sse" or path.startswith("/sse/") or path.startswith("/messages"):
            await self.mcp_sse_app(scope, receive, send)
        else:
            await self.fastapi_app(scope, receive, send)


app = UnifiedASGIGateway(app, streamable_http_app, sse_app)
