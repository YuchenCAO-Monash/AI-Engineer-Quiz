# 素材检索与 CSV 导出

工作分支为 `quiz/asset-search-csv`。

## 本地运行

项目要求 Python 3.10+。以下步骤在 macOS 上使用 Python 3.14.8 实测。克隆作业分支后，在仓库根目录运行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

export KEEPSTACK_DATA_DIR="$(mktemp -d)"
export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false
export KEEPSTACK_HOST=127.0.0.1

python -m keepstack seed
python -m keepstack
```

打开 [本地 Keepstack](http://127.0.0.1:8000)，使用演示账号 `admin` / `admin`。Seed 在临时目录生成 24 条素材和 3 个集合。每个新演示库生成一次数据即可。

## 复现与验证

1. 从 **Collections** 进入 **Environment & Survey**。搜索 `coastal`，在 **Include all tags** 添加 `coast` 和 `survey`。列表和 CSV 均应包含 2 条素材。
2. 在 **Exclude any tag** 添加 `coast`，结果应变为 0 条。点击 **Export CSV**，文件仅有表头。
3. 点击 **Clear filters**，在 **Library (24)** 中翻到第二页。页面应显示 9 条。点击 **Export CSV** 后，文件包含全部 24 条，UUID 顺序与两页列表拼接后的顺序一致。
4. 编辑一条素材，把标题设为 `harboronly`，保存后应能搜到并导出该素材。再把标题和描述分别改为 `alpineonly` 和 `camelliaonly`，旧词应失效，新词都能搜到同一条素材。添加、删除 `reviewready` 后，用关键词和精确标签筛选分别检查列表及导出。

在另一个终端进入仓库根目录，运行：

```sh
KEEPSTACK_OCR_ENABLED=true .venv/bin/python -B -m pytest -q -ra -p no:cacheprovider
```

实测 **27 项通过，2 项跳过**。两项分别因本地视觉模型和 Tesseract 未安装而跳过。开发依赖安装和全量测试也在干净的 Python 3.14.8 虚拟环境中验证过。

[回归测试](tests/test_core.py) 在独立的临时数据目录生成图片、标签、集合和相同创建时间的素材，覆盖组合筛选、跨页顺序、CSV 一致性及修改后的查询与导出。运行测试即可复现，不依赖已生成的演示数据库。

### 两轮验收记录

- **ChatGPT（Codex）** 在 CI 和 `active` 契约修复后，用干净的 Python 3.14.8 虚拟环境验证依赖安装并重跑上述全量测试。浏览器验证中，从第二页下载的 CSV 含全部 24 条素材，UUID 顺序与两页列表一致。
- **DeepSeek Harness** 在上述两项修复前，对本地工作区独立构造 `harbor` 场景，88 项 API 断言和 41 项浏览器断言全部通过。从第二页导出的 22 条 UUID 与所有匹配列表页按序拼接的结果一致。

## 实现取舍与限制

- 关键词沿用 SQLite FTS5。包含标签要求全部命中，排除标签命中任意一个即剔除。列表和导出共用筛选及排序，状态固定为 `active`。
- 每页 15 条。同一创建时间下按唯一素材 ID 决定顺序。分页和 CSV 一致性以数据不变为前提。侧栏统计为全库计数，结果标题显示当前匹配总数。
- CSV 使用带 BOM 的 UTF-8，`tags` 列为 JSON 数组。导出在内存中生成，内存开销随文件增大。自由文本保留原值，在表格软件中查看不可信数据时应将相应列按文本导入。
- 目录修改和全文索引重建目前分开提交。若索引重建失败，目录数据可能已更新，关键词结果会暂时不同步。
- 本题功能范围内无已知未完成项。团队部署前仍应加固分享页 HTML 转义、公开预览访问控制，以及上传和图片缩放的资源上限。
