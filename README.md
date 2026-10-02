# 本地容器制品仓库（Local OCI Registry）

基于 **Python + FastAPI + SQLite + 本地文件存储** 的轻量 OCI 风格制品仓库。
支持分块/断点续传 blob、按摘要全局去重、重启后恢复上传、不可变 manifest
发布校验、标签 CAS 移动，以及带 dry-run 复核的可达性垃圾回收。

## 功能特性

| 能力 | 说明 |
| --- | --- |
| OCI 风格 API | `/v2/<repo>/blobs/uploads`、`/v2/<repo>/manifests/<ref>`、`/v2/<repo>/blobs/<digest>`、`/v2/<repo>/tags/list` |
| 分块上传 / 续传 | `POST → PATCH* → PUT?digest=`；`GET` 会话返回 `Range` 进度，可断点续传 |
| 摘要校验 | 上传完成时按内容计算 sha256，与客户端摘要不一致即拒绝并取消会话 |
| 按摘要去重 | blob 全局内容寻址存储，跨仓库/重复推送只占一份磁盘；支持 `?mount=&from=` 跨仓库挂载 |
| 重启恢复 | 上传会话与进度持久化在 SQLite，重启后自动对齐磁盘实际进度，可继续续传 |
| 不可变 manifest | digest 寻址的 manifest 内容与摘要不符即拒绝；发布时校验 config/layers/subject/子 manifest 引用必须已存在，杜绝悬空引用 |
| 标签 CAS | 移动已存在的标签必须携带期望摘要（`If-Match` 头或 `?expected=`），不匹配返回 `409 TAG_CONFLICT` |
| 安全 GC | 以「标签 + 未过期上传租约」为根建立可达图；**先 dry-run 生成持久化复核计划（`plan_id`，1 小时有效、重启有效），再凭 `plan_id` 执行**；执行时重算可达性/租约，只删计划内仍符合条件的对象，复核窗口内新产生的候选不会被误删；与并发拉取/发布互斥 |

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build -d
```

服务监听在 **http://localhost:5000**（容器内 8000 端口），数据持久化在
`registry-data` 卷中。验证：

```bash
curl http://localhost:5000/v2/        # -> {} ，响应头含 Docker-Distribution-API-Version: registry/2.0
curl http://localhost:5000/healthz    # -> {"status":"ok"}
```

也可以本地直接运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5000
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REGISTRY_DATA_DIR` | `./data`（镜像内 `/data`） | 数据目录（SQLite + blob 文件 + 上传临时文件） |
| `REGISTRY_UPLOAD_LEASE_SECONDS` | `3600` | 上传会话租约；超时无读写即被 GC 回收 |
| `REGISTRY_GC_GRACE_SECONDS` | `3600` | GC 宽限期；创建不足该时长的未引用 blob 不回收，保护「已推送、待发布」窗口 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v2/` | API 版本探测 |
| POST | `/v2/{repo}/blobs/uploads` | 创建上传会话（202）；`?digest=` 整体上传（201）；`?mount=&from=` 跨仓库挂载 |
| GET | `/v2/{repo}/blobs/uploads/{id}` | 查询上传进度（204 + `Range`），用于续传 |
| PATCH | `/v2/{repo}/blobs/uploads/{id}` | 追加分块，可选 `Content-Range` 校验偏移 |
| PUT | `/v2/{repo}/blobs/uploads/{id}?digest=` | 完成上传（可携带最后一块），校验摘要 |
| DELETE | `/v2/{repo}/blobs/uploads/{id}` | 取消上传 |
| GET/HEAD | `/v2/{repo}/blobs/{digest}` | 拉取 blob |
| PUT | `/v2/{repo}/manifests/{ref}` | 发布 manifest；`ref` 为标签时需 CAS（`If-Match` 或 `?expected=`） |
| GET/HEAD | `/v2/{repo}/manifests/{ref}` | 拉取 manifest（`ref` 可为标签或 digest） |
| GET | `/v2/{repo}/tags/list` | 列出标签 |
| POST | `/gc` | 垃圾回收。`{"dry_run": true, "grace_seconds": 3600}` 生成持久化复核计划并返回 `plan_id`；`{"dry_run": false, "plan_id": "..."}` 凭计划执行 |

错误统一为 OCI 格式：`{"errors":[{"code","message","detail"}]}`。

## curl 示例

以下假设服务在 `localhost:5000`，仓库为 `demo/app`。

### 1. 分块上传 blob（断点续传）

```bash
# 准备数据并计算摘要
printf 'hello-layer-content' > layer.bin
DIGEST="sha256:$(sha256sum layer.bin | cut -d' ' -f1)"

# 创建上传会话，记录 Location
LOC=$(curl -s -D - -o /dev/null -X POST \
  http://localhost:5000/v2/demo/app/blobs/uploads | awk '/^[Ll]ocation:/{print $2}' | tr -d '\r')
echo "$LOC"   # /v2/demo/app/blobs/uploads/<uuid>

# 上传前 5 字节（模拟中断）
curl -s -X PATCH -H 'Content-Type: application/octet-stream' \
  --data-binary @- "http://localhost:5000$LOC" <<< '' >/dev/null  # 仅占位
head -c 5 layer.bin | curl -s -D - -o /dev/null -X PATCH \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @- "http://localhost:5000$LOC" | grep -i range
# Range: 0-4

# 断点续传：先查询进度，再带 Content-Range 续传剩余部分
curl -s -D - -o /dev/null "http://localhost:5000$LOC" | grep -i range
tail -c +6 layer.bin | curl -s -X PATCH \
  -H 'Content-Type: application/octet-stream' -H 'Content-Range: 5-18' \
  --data-binary @- "http://localhost:5000$LOC" -o /dev/null

# 完成上传并校验摘要（服务按内容重算 sha256，不符则 400 并取消会话）
curl -s -D - -o /dev/null -X PUT "http://localhost:5000$LOC?digest=$DIGEST"
# 201 Created, Docker-Content-Digest: sha256:...
```

小文件也可整体上传：

```bash
curl -s -X POST --data-binary @layer.bin \
  "http://localhost:5000/v2/demo/app/blobs/uploads?digest=$DIGEST"   # 201，重复推送会去重秒回
```

### 2. 发布 manifest（标签 CAS）

```bash
# 先推 config 与 layer 两个 blob（略，同上），然后组装 manifest
cat > manifest.json <<EOF
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config":   {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "$CONFIG_DIGEST", "size": $CONFIG_SIZE},
  "layers":   [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": "$DIGEST", "size": 19}]
}
EOF

# 首次打 tag：无需期望摘要
curl -s -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data-binary @manifest.json \
  http://localhost:5000/v2/demo/app/manifests/v1     # 201

# 移动 tag 必须携带期望摘要（CAS），否则 409 TAG_CONFLICT
curl -s -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data-binary @manifest-v2.json \
  http://localhost:5000/v2/demo/app/manifests/v1     # 409

# 正确姿势：If-Match 携带 tag 当前指向的 digest
CUR=$(curl -sI http://localhost:5000/v2/demo/app/manifests/v1 | awk '/^[Dd]ocker-[Cc]ontent-[Dd]igest:/{print $2}' | tr -d '\r')
curl -s -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  -H "If-Match: $CUR" \
  --data-binary @manifest-v2.json \
  http://localhost:5000/v2/demo/app/manifests/v1     # 201，tag 完成移动
```

发布时服务端会校验 manifest 引用的 config/layers（以及 index 的子
manifest、referrers 的 subject）必须已存在于本仓库，否则返回
`400 MANIFEST_BLOB_UNKNOWN`，从源头杜绝悬空引用。

### 3. 拉取

```bash
curl -s http://localhost:5000/v2/demo/app/manifests/v1            # 按 tag 拉 manifest
curl -s http://localhost:5000/v2/demo/app/manifests/$MANIFEST_DIGEST
curl -s http://localhost:5000/v2/demo/app/blobs/$DIGEST -o layer.bin
curl -s http://localhost:5000/v2/demo/app/tags/list
```

### 4. 垃圾回收（dry-run 复核计划 → 凭 plan_id 执行）

GC 采用**两阶段复核**，修复了「dry-run 后标签移动产生新垃圾、执行时被一并
删除」的竞态：dry-run 把候选清单**快照持久化**成计划（`plan_id`），执行时只
处理计划内、且此刻仍符合条件的对象；复核窗口内新出现的候选一律留给下次审阅。

> 计划保存在 SQLite 中，**进程重启后依然有效**；生成后 **1 小时**内可执行；
> 一份计划**只能执行一次**，重复执行返回 `409` 且不再删数据。

#### 第一步：dry-run 生成计划（不删任何数据）

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"dry_run": true, "grace_seconds": 0}' \
  http://localhost:5000/gc
```

实际响应（`grace_seconds: 0` 仅为演示立刻可回收；生产可用默认 3600）：

```json
{
  "dry_run": true,
  "executed": false,
  "plan_id": "8c127c6eacfc43e99e61c00fb65eafff",
  "grace_seconds": 0,
  "generated_at": "2026-10-02T02:07:03.770712+00:00",
  "expires_at": "2026-10-02T03:07:03.770712+00:00",
  "summary": {
    "blobs_to_delete": 2,
    "manifests_to_delete": 1,
    "uploads_to_delete": 1,
    "bytes_to_reclaim": 21,
    "manifests_reachable": 1,
    "blobs_reachable": 2,
    "blobs_kept_by_grace": 0,
    "uploads_kept_by_lease": 1
  },
  "delete": {
    "blobs": [
      {"digest": "sha256:165f83583dbdfe9eae2121e5ac9d56638daa6730509308f9c355762e893177f4",
       "size": 7, "repos": ["demo/app"],
       "reason": "unreferenced and older than grace period"},
      {"digest": "sha256:97de4a6c1d6a83826325596ca29cc454f5b8c87bb1d5ea60c6ca2d15ab9dec92",
       "size": 14, "repos": ["demo/app"],
       "reason": "unreferenced and older than grace period"}
    ],
    "manifests": [
      {"repo": "demo/app",
       "digest": "sha256:eb42747048bf86fd2e571093614525963ef737e7d35e2d8cb20bba6586857225",
       "media_type": "application/vnd.oci.image.manifest.v1+json",
       "reason": "untagged"}
    ],
    "uploads": [
      {"id": "660ab73e99684f49999203878365fa3b", "repo": "demo/app", "size": 0,
       "expired_at": "1970-01-01T00:00:01+00:00", "reason": "lease expired"}
    ]
  },
  "keep": {
    "blobs_grace_protected": [],
    "uploads_leased": [
      {"id": "dc4704cef321442e8180a9b24d86aafd", "repo": "demo/app", "size": 0,
       "expires_at": "2026-10-02T03:06:45.215453+00:00"}
    ]
  }
}
```

复核要点：`delete.blobs / delete.manifests / delete.uploads` 即本次计划的候选；
`keep` 是宽限期/租约保护下保留的对象。记下 `plan_id`。

#### 第二步：凭 plan_id 执行

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"dry_run": false, "plan_id": "8c127c6eacfc43e99e61c00fb65eafff"}' \
  http://localhost:5000/gc
```

实际响应——`deleted` 是真正删除的项，`skipped` 是计划内、但执行时已重新可达/
续租/消失而跳过的项：

```json
{
  "dry_run": false,
  "executed": true,
  "plan_id": "8c127c6eacfc43e99e61c00fb65eafff",
  "grace_seconds": 0,
  "generated_at": "2026-10-02T02:07:03.770712+00:00",
  "executed_at": "2026-10-02T02:07:25.296842+00:00",
  "summary": {
    "blobs_deleted": 2,
    "manifests_deleted": 1,
    "uploads_deleted": 1,
    "blobs_skipped": 0,
    "manifests_skipped": 0,
    "uploads_skipped": 0,
    "bytes_reclaimed": 21
  },
  "deleted": {
    "blobs": [
      {"digest": "sha256:165f83583dbdfe9eae2121e5ac9d56638daa6730509308f9c355762e893177f4",
       "size": 7, "repos": ["demo/app"], "reason": "still unreferenced at execution"},
      {"digest": "sha256:97de4a6c1d6a83826325596ca29cc454f5b8c87bb1d5ea60c6ca2d15ab9dec92",
       "size": 14, "repos": ["demo/app"], "reason": "still unreferenced at execution"}
    ],
    "manifests": [
      {"repo": "demo/app",
       "digest": "sha256:eb42747048bf86fd2e571093614525963ef737e7d35e2d8cb20bba6586857225",
       "media_type": "application/vnd.oci.image.manifest.v1+json",
       "reason": "still untagged at execution"}
    ],
    "uploads": [
      {"id": "660ab73e99684f49999203878365fa3b", "repo": "demo/app", "size": 0,
       "reason": "lease still expired at execution"}
    ]
  },
  "skipped": {"blobs": [], "manifests": [], "uploads": []}
}
```

复核窗口内若把一个候选重新打上标签，执行时它会进入 `skipped` 而不是被删，例如：

```json
"skipped": {
  "blobs": [
    {"digest": "sha256:....", "reason": "reachable again: referenced by a tagged manifest"}
  ],
  "manifests": [
    {"repo": "demo/app", "digest": "sha256:....",
     "reason": "reachable again: a tag (or a tagged parent) points to it"}
  ],
  "uploads": [
    {"id": "....", "repo": "demo/app", "reason": "lease renewed or still active"}
  ]
}
```

#### 拒绝执行的情况（均不删数据）

| 情况 | 状态码 | `errors[0].code` |
| --- | --- | --- |
| 未带 `plan_id` / 空白 | 400 | `GC_PLAN_REQUIRED` |
| `plan_id` 未知 | 404 | `GC_PLAN_UNKNOWN` |
| 计划生成已超过 1 小时 | 410 | `GC_PLAN_EXPIRED` |
| 计划已经执行过 | 409 | `GC_PLAN_ALREADY_EXECUTED` |

实测示例：

```bash
$ curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"dry_run": false}' http://localhost:5000/gc
# HTTP 400
{"errors":[{"code":"GC_PLAN_REQUIRED",
  "message":"execution (dry_run=false) requires the plan_id of a prior dry-run report; ...",
  "detail":{"hint":"POST /gc with {\"dry_run\": true} to obtain a plan_id"}}]}

$ curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"dry_run": false, "plan_id": "does-not-exist"}' http://localhost:5000/gc
# HTTP 404 -> GC_PLAN_UNKNOWN

$ curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"dry_run": false, "plan_id": "8c127c6e..."}' http://localhost:5000/gc
# HTTP 410 -> GC_PLAN_EXPIRED（超过生成后 1 小时）
# HTTP 409 -> GC_PLAN_ALREADY_EXECUTED（重复执行）
```

#### 复核窗口内新产生的候选不会被误删

在上面 dry-run 与执行之间，若发生标签移动或悬空推送而产生**新的**不可达对象
（即使它已老过宽限期），执行时它**不在计划内，因而保留**；它只会出现在**下一次**
dry-run 的候选中，等待新一轮人工复核：

```bash
# 执行后：计划内旧垃圾已删，dry-run 之后才出现的新 blob 仍在
$ curl -s -o /dev/null -w '%{http_code}\n' \
    http://localhost:5000/v2/demo/app/blobs/sha256:c97adf18...
200

# 下一次 dry-run 才把它列为候选
$ curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"dry_run": true, "grace_seconds": 0}' http://localhost:5000/gc
# {"plan_id": "23338789...", "delete": {"blobs": [{"digest": "sha256:c97adf18..."}]}, ...}
```

GC 语义：

- **两阶段复核**：dry-run 快照候选为持久化计划（`gc_plans` 等表），执行必须凭
  `plan_id`；计划缺失/未知/超过 1 小时/已执行一律拒绝，不删任何数据；计划跨
  重启有效，且只能执行一次。
- **执行时复核（TOCTOU 防护）**：在同一把进程锁内，以**计划记录的宽限期**重新
  计算可达图与上传租约——计划内且仍不可达/仍超宽限/租约仍过期的才删除；
  重新可达、续租或已不存在的进入 `skipped`。
- **计划外不碰**：dry-run 之后因标签移动、删除、悬空推送新产生的候选不在计划内，
  本次执行绝不删除，留待下次 dry-run 审阅（上面已实测）。
- **根集合**：所有 tag 指向的 manifest；沿 `config`/`layers`/`manifests[]`/
  `subject` 遍历出全部可达 blob 与 manifest。
- **回收对象**：不可达且超出宽限期的 blob、不可达（无标签）的 manifest、
  租约已过期的上传会话。
- **保护机制**：未过期上传租约不会被清理；宽限期内的“新 blob”不会被清理
  （覆盖“已推送 blob、尚未发布 manifest”的并发窗口）；GC 与发布/上传在同一
  写锁下串行，拉取在打开文件句柄后再流式发送，文件随后被删也不影响进行中的下载。
- 删除顺序为先清元数据再删文件，崩溃残留由下次启动时的孤儿文件清扫兜底。

## 目录结构

```
app/
  main.py       # FastAPI 路由（/v2/*、/gc 两阶段复核）
  registry.py   # 核心逻辑：上传、manifest CAS、GC 可达图与复核计划执行
  db.py         # SQLite 元数据（WAL，进程内 RLock 串行化；含 gc_plans 持久化计划）
  storage.py    # 本地文件存储：blobs/sha256/<hex> 内容寻址 + uploads/<uuid>
  digest.py     # digest/tag/repo 校验与 sha256 计算
  errors.py     # OCI 错误模型（含 GC_PLAN_*）
  config.py     # 环境变量配置
tests/test_registry.py   # 29 个端到端测试
Dockerfile / docker-compose.yml
```

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

测试覆盖：分块上传与续传、摘要校验失败、幂等重试、跨仓库去重与挂载、
manifest 引用校验、digest 不可变性、标签 CAS 冲突、GC dry-run/执行、
宽限期与租约保护、GC 与并发拉取/发布竞争、重启后恢复上传，以及 GC 复核计划：
dry-run 后新增候选不被误删、重新可达/续租跳过、计划缺失/未知/过期(410)/已执行
(409)拒绝、计划跨重启有效且只能执行一次。

## 设计说明与限制

- **单进程单写**：SQLite（WAL）+ 进程内写锁保证元数据一致；请用单 worker
  运行（compose 默认如此）。多实例水平扩展需替换为外部数据库与共享存储。
- **命名空间隔离**：blob 物理上全局去重，但 `GET /v2/<repo>/blobs/<digest>`
  要求该 blob 已在 `<repo>` 命名空间内关联，跨仓库不可直接读。
- **manifest 不可变**：同一 digest 的内容恒定；digest 寻址的 PUT 会校验内容
  与摘要一致；标签只是指向 digest 的可变指针，且移动必须走 CAS。
