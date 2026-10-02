"""核心仓库逻辑：分块上传、manifest 发布（CAS）、垃圾回收。

并发模型：进程内一把 threading.RLock 串行化所有元数据写事务与 GC；
拉取 blob 时在锁内打开文件句柄，锁外流式发送——即使 GC 随后删除文件，
POSIX 下已打开的 fd 依然可读，因此并发拉取不会被误删打断。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import errors as E
from .config import Settings
from .db import Database
from .digest import is_digest, is_tag, sha256_bytes, sha256_file
from .storage import FileStorage

DEFAULT_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def parse_manifest_refs(content: bytes):
    """解析 manifest，返回 (blob_digests, manifest_digests, media_type|None)。

    blob 引用：config、layers[]；manifest 引用：manifests[]（index）、subject（referrers）。
    结构非法时抛 ValueError。
    """
    doc = json.loads(content.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a JSON object")
    blobs, manifests = [], []

    def _digest_of(descriptor, field):
        if not isinstance(descriptor, dict) or "digest" not in descriptor:
            raise ValueError(f"invalid {field} descriptor: missing digest")
        d = descriptor["digest"]
        if not is_digest(d):
            raise ValueError(f"invalid digest in {field}: {d!r}")
        return d

    if "config" in doc:
        blobs.append(_digest_of(doc["config"], "config"))
    layers = doc.get("layers")
    if layers is not None:
        if not isinstance(layers, list):
            raise ValueError("layers must be a list")
        for layer in layers:
            blobs.append(_digest_of(layer, "layers[]"))
    children = doc.get("manifests")
    if children is not None:
        if not isinstance(children, list):
            raise ValueError("manifests must be a list")
        for child in children:
            manifests.append(_digest_of(child, "manifests[]"))
    if "subject" in doc:
        manifests.append(_digest_of(doc["subject"], "subject"))
    return blobs, manifests, doc.get("mediaType")


def parse_content_range_start(value: str) -> int:
    """接受 '0-511' / 'bytes 0-511' / 'bytes 0-511/1024'，返回起始偏移。"""
    text = value.strip()
    if text.lower().startswith("bytes "):
        text = text[6:]
    text = text.split("/", 1)[0]
    start = text.split("-", 1)[0].strip()
    if not start.isdigit():
        raise E.upload_invalid(f"invalid Content-Range header: {value!r}")
    return int(start)


class Registry:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = FileStorage(settings)
        self.db = Database(settings.db_path)
        self.lock = threading.RLock()
        self._reconcile()

    # ---------------- 启动恢复 ----------------

    def _reconcile(self):
        """重启后恢复：以上传临时文件为准修正会话进度；清理孤儿 blob 文件。"""
        with self.lock:
            for u in self.db.list_uploads(state="active"):
                path = self.storage.upload_path(u["id"])
                if not path.exists():
                    self.db.set_upload_state(u["id"], "cancelled")
                    continue
                actual = path.stat().st_size
                if actual != u["size"]:
                    self.db.update_upload_progress(u["id"], actual, u["expires_at"])
            known = {b["digest"] for b in self.db.all_blobs()}
            self.storage.sweep_orphan_blobs(
                known, older_than_ts=time.time() - self.settings.gc_grace_seconds
            )

    # ---------------- 上传会话 ----------------

    def start_upload(self, repo: str) -> dict:
        with self.lock:
            upload_id = uuid.uuid4().hex
            now = time.time()
            self.db.create_upload(
                upload_id, repo, now, now + self.settings.upload_lease_seconds
            )
            self.storage.upload_path(upload_id).touch()
            return self.db.get_upload(upload_id)

    def _active_upload(self, repo: str, upload_id: str) -> dict:
        u = self.db.get_upload(upload_id)
        if u is None or u["repo"] != repo or u["state"] != "active":
            raise E.upload_unknown(upload_id)
        if u["expires_at"] <= time.time():
            raise E.upload_unknown(upload_id)  # 租约已过期，等 GC 回收
        return u

    def _renew_lease(self, upload_id: str) -> float:
        return time.time() + self.settings.upload_lease_seconds

    def get_upload_status(self, repo: str, upload_id: str) -> dict:
        with self.lock:
            u = self._active_upload(repo, upload_id)
            self.db.update_upload_progress(u["id"], u["size"], self._renew_lease(u["id"]))
            return self.db.get_upload(upload_id)

    def append_chunk(self, repo: str, upload_id: str, chunk: bytes,
                     content_range: "str | None" = None) -> int:
        with self.lock:
            u = self._active_upload(repo, upload_id)
            path = self.storage.upload_path(upload_id)
            if not path.exists():
                raise E.upload_unknown(upload_id)
            if content_range:
                start = parse_content_range_start(content_range)
                if start != u["size"]:
                    raise E.range_invalid(u["size"], content_range)
            with open(path, "ab") as f:
                f.write(chunk)
            new_size = u["size"] + len(chunk)
            self.db.update_upload_progress(upload_id, new_size, self._renew_lease(upload_id))
            return new_size

    def finalize_upload(self, repo: str, upload_id: str, digest: str,
                        chunk: "bytes | None" = None):
        """校验摘要并落库。已 committed 的会话允许同 digest 幂等重试。"""
        with self.lock:
            u = self.db.get_upload(upload_id)
            if u is None or u["repo"] != repo:
                raise E.upload_unknown(upload_id)
            if u["state"] == "committed":
                if u["digest"] == digest and self.db.repo_has_blob(repo, digest):
                    blob = self.db.get_blob(digest)
                    return digest, blob["size"] if blob else 0
                raise E.upload_invalid(
                    "upload already committed with a different digest",
                    {"upload_id": upload_id},
                )
            if u["state"] != "active" or u["expires_at"] <= time.time():
                raise E.upload_unknown(upload_id)

            path = self.storage.upload_path(upload_id)
            if not path.exists():
                raise E.upload_unknown(upload_id)
            if chunk:
                with open(path, "ab") as f:
                    f.write(chunk)
            computed = sha256_file(path)
            if computed != digest:
                # 数据不可信，取消会话让客户端重新上传
                self.storage.delete_upload_file(upload_id)
                self.db.set_upload_state(upload_id, "cancelled")
                raise E.digest_mismatch(expected=digest, computed=computed)

            size = self.storage.commit_blob(path, digest)  # 按摘要去重
            self.db.create_blob(digest, size)
            self.db.link_repo_blob(repo, digest)
            self.db.set_upload_state(upload_id, "committed", digest)
            return digest, size

    def monolithic_upload(self, repo: str, payload: bytes, digest: str):
        computed = sha256_bytes(payload)
        if computed != digest:
            raise E.digest_mismatch(expected=digest, computed=computed)
        with self.lock:
            tmp = self.storage.upload_path(uuid.uuid4().hex)
            with open(tmp, "wb") as f:
                f.write(payload)
            size = self.storage.commit_blob(tmp, digest)
            self.db.create_blob(digest, size)
            self.db.link_repo_blob(repo, digest)
            return digest, size

    def mount_blob(self, repo: str, digest: str) -> bool:
        """跨仓库挂载：blob 已存在则直接关联，实现秒级去重推送。"""
        with self.lock:
            if not self.db.blob_exists(digest) or not self.storage.has_blob(digest):
                return False
            self.db.link_repo_blob(repo, digest)
            return True

    def cancel_upload(self, repo: str, upload_id: str):
        with self.lock:
            u = self.db.get_upload(upload_id)
            if u is None or u["repo"] != repo:
                raise E.upload_unknown(upload_id)
            self.storage.delete_upload_file(upload_id)
            self.db.delete_upload(upload_id)

    # ---------------- blob 读取 ----------------

    def open_blob(self, repo: str, digest: str):
        """返回 (file, size)。文件句柄在锁内打开，GC 之后删文件不影响读取。"""
        with self.lock:
            if not self.db.repo_has_blob(repo, digest):
                raise E.blob_unknown(repo, digest)
            try:
                f = self.storage.open_blob(digest)
            except FileNotFoundError:
                raise E.blob_unknown(repo, digest)
            return f, os.fstat(f.fileno()).st_size

    def stat_blob(self, repo: str, digest: str) -> int:
        with self.lock:
            f, size = self.open_blob(repo, digest)
            f.close()
            return size

    # ---------------- manifest 发布 / 读取 ----------------

    def put_manifest(self, repo: str, ref: str, content: bytes,
                     content_type: "str | None", expected: "str | None") -> str:
        if not content:
            raise E.manifest_invalid("empty manifest body")
        digest = sha256_bytes(content)
        if is_digest(ref):
            if ref != digest:  # digest 寻址的 manifest 不可伪造
                raise E.digest_mismatch(expected=ref, computed=digest)
            tag = None
        elif is_tag(ref):
            tag = ref
        else:
            raise E.tag_invalid(ref)

        try:
            blob_refs, child_refs, doc_media_type = parse_manifest_refs(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise E.manifest_invalid(str(exc))
        media_type = (
            (content_type or "").split(";")[0].strip()
            or doc_media_type
            or DEFAULT_MANIFEST_MEDIA_TYPE
        )

        with self.lock:
            # 校验层引用：config/layers 必须是本 repo 已存在的 blob，
            # index 子 manifest / subject 必须已存在，杜绝悬空引用
            missing_blobs = [d for d in blob_refs if not self.db.repo_has_blob(repo, d)]
            if missing_blobs:
                raise E.manifest_blob_unknown(repo, missing_blobs)
            missing_manifests = [d for d in child_refs if not self.db.manifest_exists(repo, d)]
            if missing_manifests:
                raise E.manifest_blob_unknown(repo, missing_manifests)

            self.db.put_manifest(repo, digest, media_type, content)
            if tag is not None:
                status, current = self.db.cas_tag(repo, tag, expected, digest)
                if status == "conflict":
                    raise E.tag_conflict(repo, tag, current, expected)
            return digest

    def get_manifest(self, repo: str, ref: str) -> dict:
        with self.lock:
            if is_digest(ref):
                row = self.db.get_manifest_by_digest(repo, ref)
            elif is_tag(ref):
                row = self.db.get_manifest_by_tag(repo, ref)
            else:
                raise E.tag_invalid(ref)
            if row is None:
                raise E.manifest_unknown(repo, ref)
            return row

    def list_tags(self, repo: str) -> list:
        with self.lock:
            return self.db.list_tags(repo)

    # ---------------- 垃圾回收 ----------------
    #
    # 两阶段复核：
    #   dry-run  -> 快照候选清单并持久化为 plan（plan_id），只出报告不删除；
    #   execute  -> 必须携带 dry-run 返回的 plan_id；执行时在同一把锁内
    #               重算可达性/租约，只删除“计划内且仍符合条件”的对象。
    # 这样 dry-run 之后新产生的垃圾（如标签移动刚制造的不可达对象）不在计划内，
    # 绝不会被未审阅地删除；重新可达或续租的对象自动跳过。

    PLAN_TTL_SECONDS = 3600              # 计划生成后 1 小时内有效
    EXECUTED_PLAN_RETENTION_SECONDS = 7 * 24 * 3600
    EXPIRED_PLAN_RETENTION_SECONDS = 30 * 24 * 3600

    def _reachable_sets(self) -> "tuple[set, set]":
        """以所有 tag 为根遍历 manifest 图，返回 (可达 manifest 键集, 可达 blob 集)。"""
        reachable_manifests: set = set()
        reachable_blobs: set = set()
        stack = [(t["repo"], t["digest"]) for t in self.db.all_tags()]
        while stack:
            key = stack.pop()
            if key in reachable_manifests:
                continue
            row = self.db.get_manifest_by_digest(*key)
            if row is None:
                continue
            reachable_manifests.add(key)
            try:
                blobs, children, _ = parse_manifest_refs(row["content"])
            except (ValueError, UnicodeDecodeError):
                continue  # 发布时已校验，理论上不会到这里
            reachable_blobs.update(blobs)
            stack.extend((key[0], c) for c in children)
        return reachable_manifests, reachable_blobs

    def _collect_candidates(self, now: float, grace: int) -> dict:
        """在当前状态下计算 GC 候选与受保护对象（调用方持锁）。"""
        reachable_manifests, reachable_blobs = self._reachable_sets()

        dead_manifests = [
            m for m in self.db.all_manifests()
            if (m["repo"], m["digest"]) not in reachable_manifests
        ]

        grace_cutoff = now - grace
        dead_blobs, grace_protected = [], []
        for b in self.db.all_blobs():
            if b["digest"] in reachable_blobs:
                continue
            (grace_protected if b["created_at"] > grace_cutoff else dead_blobs).append(b)

        active_uploads = self.db.list_uploads(state="active")
        expired_uploads = [u for u in active_uploads if u["expires_at"] <= now]
        leased_uploads = [u for u in active_uploads if u["expires_at"] > now]

        return {
            "reachable_manifests": reachable_manifests,
            "reachable_blobs": reachable_blobs,
            "dead_blobs": dead_blobs,
            "grace_protected": grace_protected,
            "dead_manifests": dead_manifests,
            "expired_uploads": expired_uploads,
            "leased_uploads": leased_uploads,
        }

    def gc_dry_run(self, grace_seconds: "int | None" = None) -> dict:
        """生成复核计划：候选快照持久化（重启有效），不删除任何数据。"""
        grace = self.settings.gc_grace_seconds if grace_seconds is None else grace_seconds
        if grace < 0:
            raise E.gc_invalid_request(
                "grace_seconds must be >= 0", {"grace_seconds": grace}
            )
        with self.lock:
            now = time.time()
            cand = self._collect_candidates(now, grace)
            dead_blobs, dead_manifests = cand["dead_blobs"], cand["dead_manifests"]
            expired_uploads = cand["expired_uploads"]
            repos_map = self.db.repos_for_blobs([b["digest"] for b in dead_blobs])

            plan_id = uuid.uuid4().hex
            self.db.prune_gc_plans(
                executed_before=now - self.EXECUTED_PLAN_RETENTION_SECONDS,
                pending_before=now - self.EXPIRED_PLAN_RETENTION_SECONDS,
            )
            self.db.create_gc_plan(
                plan_id, grace, now, self.PLAN_TTL_SECONDS,
                blobs=[{"digest": b["digest"], "size": b["size"],
                        "repos": sorted(repos_map.get(b["digest"], []))}
                       for b in dead_blobs],
                manifests=[{"repo": m["repo"], "digest": m["digest"],
                            "media_type": m["media_type"]} for m in dead_manifests],
                uploads=[u["id"] for u in expired_uploads],
            )

            return {
                "dry_run": True,
                "executed": False,
                "plan_id": plan_id,
                "grace_seconds": grace,
                "generated_at": _iso(now),
                "expires_at": _iso(now + self.PLAN_TTL_SECONDS),
                "summary": {
                    "blobs_to_delete": len(dead_blobs),
                    "manifests_to_delete": len(dead_manifests),
                    "uploads_to_delete": len(expired_uploads),
                    "bytes_to_reclaim": sum(b["size"] for b in dead_blobs)
                    + sum(u["size"] for u in expired_uploads),
                    "manifests_reachable": len(cand["reachable_manifests"]),
                    "blobs_reachable": len(cand["reachable_blobs"]),
                    "blobs_kept_by_grace": len(cand["grace_protected"]),
                    "uploads_kept_by_lease": len(cand["leased_uploads"]),
                },
                "delete": {
                    "blobs": [
                        {"digest": b["digest"], "size": b["size"],
                         "repos": sorted(repos_map.get(b["digest"], [])),
                         "reason": "unreferenced and older than grace period"}
                        for b in sorted(dead_blobs, key=lambda x: x["digest"])
                    ],
                    "manifests": [
                        {"repo": m["repo"], "digest": m["digest"],
                         "media_type": m["media_type"], "reason": "untagged"}
                        for m in sorted(dead_manifests,
                                        key=lambda x: (x["repo"], x["digest"]))
                    ],
                    "uploads": [
                        {"id": u["id"], "repo": u["repo"], "size": u["size"],
                         "expired_at": _iso(u["expires_at"]), "reason": "lease expired"}
                        for u in sorted(expired_uploads, key=lambda x: x["id"])
                    ],
                },
                "keep": {
                    "blobs_grace_protected": [
                        {"digest": b["digest"], "size": b["size"],
                         "age_seconds": round(now - b["created_at"], 3)}
                        for b in sorted(cand["grace_protected"],
                                        key=lambda x: x["digest"])
                    ],
                    "uploads_leased": [
                        {"id": u["id"], "repo": u["repo"], "size": u["size"],
                         "expires_at": _iso(u["expires_at"])}
                        for u in sorted(cand["leased_uploads"], key=lambda x: x["id"])
                    ],
                },
            }

    def gc_execute(self, plan_id: "str | None") -> dict:
        """按已复核计划执行 GC。

        拒绝条件（不删任何数据）：plan_id 缺失、未知、生成已超过 1 小时、已执行。
        执行时在锁内以计划记录的宽限期重算可达性与租约：只删除计划内仍符合
        条件的对象；计划外的新候选、重新可达或续租的对象一律跳过。
        """
        if not plan_id or not isinstance(plan_id, str) or not plan_id.strip():
            raise E.gc_plan_required()
        plan_id = plan_id.strip()
        with self.lock:
            plan = self.db.get_gc_plan(plan_id)
            if plan is None:
                raise E.gc_plan_unknown(plan_id)
            if plan["state"] == "executed":
                raise E.gc_plan_already_executed(
                    plan_id, _iso(plan["executed_at"]) if plan["executed_at"] else None
                )
            now = time.time()
            if plan["expires_at"] <= now:
                raise E.gc_plan_expired(plan_id, _iso(plan["expires_at"]))

            grace = plan["grace_seconds"]
            grace_cutoff = now - grace
            reachable_manifests, reachable_blobs = self._reachable_sets()

            # ---- 逐候选复核：只认计划内对象的当前状态 ----
            plan_blobs = self.db.get_gc_plan_blobs(plan_id)
            plan_manifests = self.db.get_gc_plan_manifests(plan_id)
            plan_upload_ids = self.db.get_gc_plan_uploads(plan_id)

            delete_blobs, skip_blobs = [], []
            for entry in sorted(plan_blobs, key=lambda x: x["digest"]):
                digest = entry["digest"]
                row = self.db.get_blob(digest)
                if row is None:
                    skip_blobs.append({"digest": digest,
                                       "reason": "no longer present"})
                elif digest in reachable_blobs:
                    skip_blobs.append({"digest": digest,
                                       "reason": "reachable again: referenced by a"
                                                 " tagged manifest"})
                elif row["created_at"] > grace_cutoff:
                    skip_blobs.append({"digest": digest,
                                       "reason": "within grace period again"})
                else:
                    delete_blobs.append({"digest": digest, "size": row["size"]})

            delete_manifests, skip_manifests = [], []
            for m in sorted(plan_manifests, key=lambda x: (x["repo"], x["digest"])):
                key = (m["repo"], m["digest"])
                row = self.db.get_manifest_by_digest(*key)
                if row is None:
                    skip_manifests.append({"repo": key[0], "digest": key[1],
                                           "reason": "no longer present"})
                elif key in reachable_manifests:
                    skip_manifests.append({"repo": key[0], "digest": key[1],
                                           "reason": "reachable again: a tag (or a"
                                                     " tagged parent) points to it"})
                else:
                    delete_manifests.append({"repo": key[0], "digest": key[1],
                                             "media_type": m["media_type"]})

            delete_uploads, skip_uploads = [], []
            for upload_id in sorted(plan_upload_ids):
                u = self.db.get_upload(upload_id)
                if u is None:
                    skip_uploads.append({"id": upload_id,
                                         "reason": "no longer present"})
                elif u["state"] != "active":
                    skip_uploads.append({"id": upload_id, "repo": u["repo"],
                                         "reason": f"upload {u['state']}"})
                elif u["expires_at"] > now:
                    skip_uploads.append({"id": upload_id, "repo": u["repo"],
                                         "reason": "lease renewed or still active"})
                else:
                    delete_uploads.append(u)

            # ---- 实际删除（先元数据后文件），全程持锁与发布/拉取互斥 ----
            for u in delete_uploads:
                self.storage.delete_upload_file(u["id"])
                self.db.delete_upload(u["id"])
            self.db.delete_manifests(
                [(m["repo"], m["digest"]) for m in delete_manifests]
            )
            digests = [b["digest"] for b in delete_blobs]
            repos_map = self.db.repos_for_blobs(digests)
            self.db.delete_blobs(digests)
            for d in digests:
                self.storage.delete_blob(d)  # 崩溃残留由启动时 sweep 兜底

            self.db.mark_gc_plan_executed(plan_id, now)

            return {
                "dry_run": False,
                "executed": True,
                "plan_id": plan_id,
                "grace_seconds": grace,
                "generated_at": _iso(plan["created_at"]),
                "executed_at": _iso(now),
                "summary": {
                    "blobs_deleted": len(delete_blobs),
                    "manifests_deleted": len(delete_manifests),
                    "uploads_deleted": len(delete_uploads),
                    "blobs_skipped": len(skip_blobs),
                    "manifests_skipped": len(skip_manifests),
                    "uploads_skipped": len(skip_uploads),
                    "bytes_reclaimed": sum(b["size"] for b in delete_blobs)
                    + sum(u["size"] for u in delete_uploads),
                },
                "deleted": {
                    "blobs": [
                        {"digest": b["digest"], "size": b["size"],
                         "repos": sorted(repos_map.get(b["digest"], [])),
                         "reason": "still unreferenced at execution"}
                        for b in delete_blobs
                    ],
                    "manifests": [
                        {"repo": m["repo"], "digest": m["digest"],
                         "media_type": m["media_type"],
                         "reason": "still untagged at execution"}
                        for m in delete_manifests
                    ],
                    "uploads": [
                        {"id": u["id"], "repo": u["repo"], "size": u["size"],
                         "reason": "lease still expired at execution"}
                        for u in delete_uploads
                    ],
                },
                "skipped": {
                    "blobs": skip_blobs,
                    "manifests": skip_manifests,
                    "uploads": skip_uploads,
                },
            }
