# 离线档案断链核查

馆员出借旧网站离线档案前，用本工具核查哪些链接会断：

1. **上传抓存目录 CSV**——每行列明页面地址（完整 URL）、UTC 抓存时刻、同站出链（完整 URL）；
2. **选入口和回看时刻**——从入口沿同站出链逐页核查，每个 URL 取不晚于回看时刻的最近一条抓存，互链页面只查一遍；
3. **保存结论**——列出缺档 URL 及从入口走到它的路径；
4. **补传联动**——补传改变了核查所选的抓存，或为原缺档 URL 补上了可用记录时，相应结论自动标为「待复查」；其余补传不影响结论有效性。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `archive_data` 里，容器重启后结论仍可查。

### 本地运行

```bash
pip install -r requirements.txt
python wsgi.py          # http://localhost:8000
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARCHIVE_DB` | `instance/archive.db` | SQLite 数据库路径 |
| `SECRET_KEY` | `dev-only-secret` | Flask 会话密钥，生产请覆盖 |
| `CHECK_STEP_DELAY_MS` | `0` | 核查每处理一页 sleep 的毫秒数（演示/测试交错操作用） |

## CSV 格式

表头须包含三列：`url,captured_at,outlinks`

```csv
url,captured_at,outlinks
http://old.site/,2023-01-05T08:00:00Z,http://old.site/about|http://old.site/news
http://old.site/about,2023-01-05T08:01:00Z,http://old.site/
http://old.site/news,2023-01-05T08:02:00Z,http://old.site/news/item-1|http://old.site/news/item-2
```

- `url` / `outlinks`：完整 URL（http/https）。多个出链用 `|` 分隔，或用 JSON 数组；无出链留空。
- `captured_at`：ISO 8601 时间；无时区时按 UTC 处理。
- 同一 URL 可有多行（不同时刻的多份抓存）。

`sample_data/` 里有三个示例：`catalog_v1.csv`（首传）、`catalog_v2_supplement.csv`（补传）、`catalog_conflict.csv`（冲突示例）。

## 一致性规则

- **冲突整批退回**：同一 URL 同一时刻的记录，若出链与既有档案（或同批内另一行）不同，整批退回，不写入任何记录；完全相同的记录自动跳过。退回也留痕可查。
- **批次与结论状态同步生效**：目录入库与受影响结论的「待复查」标记在同一个事务里完成。
- **待复查的触发条件（且仅这两个）**：
  1. 结论中某页面出现了更晚但仍不晚于回看时刻的抓存（核查所选会变）；
  2. 结论中的缺档 URL 出现了不晚于回看时刻的抓存（缺档被补上）。
- **交错操作不留过时结论**：核查遍历在同一只读快照事务中进行（WAL 模式下不阻塞补传）；结论落库时在写事务里复查快照之后新生效的批次，若影响本结论则直接标为「待复查」；补传事务也会扫描全部有效结论。两个机制互补，任何交错顺序下都不会留下过时的「有效」结论。
- **遍历期间可补传**：SQLite WAL + 多线程服务器，长核查请求期间上传请求照常处理。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

覆盖：断链识别与路径、回看时刻选抓存、互链去重、补传标复查（两类触发 + 无关补传不误标）、冲突整批退回、交错核查、重启持久化、页面冒烟。

## 已知限制

- 出链是否"同站"由上传的 CSV 保证，核查时不再额外过滤。
- 单进程多线程 + SQLite，适合馆内部署；多实例水平扩展需换外部数据库。
- 极大目录（百万行级）上传为一次性解析，内存随文件大小增长。
