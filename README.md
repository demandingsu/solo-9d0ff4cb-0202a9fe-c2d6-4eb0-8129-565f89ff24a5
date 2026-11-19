# 纸样厚度计数核对与复卷纸追溯 API

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部已接收计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。

另提供复卷纸追溯：登记原卷长度并关联已有质检登记号；成卷按接入顺序记录
来源卷（可以是另一成卷）、截取区间与是否掉头。查询成卷上的一段即可按成卷
顺序得到原卷号、原卷区间、方向和质检登记号，遇接缝自动分段。更正卷长或
接段若造成循环或使已登记的下游截取越界，**整次拒收**，原数据保持不变。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入与查询。SQLite 数据保存在命名卷
`inspection-data`（容器内 `/data/inspection.db`）中，重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记及已接收的分次补报 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与已有计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界 |
| `POST` | `/rolls/raw` | 登记原卷（长度 mm + 关联质检登记号） |
| `POST` | `/rolls/finished` | 登记成卷及按接入顺序的接段（来源可为另一成卷，可掉头） |
| `GET` | `/rolls` | 列出全部原卷与成卷 |
| `GET` | `/rolls/{id}` | 查看卷信息与接段（含每段在成卷上的 `[start,end)`） |
| `PATCH` | `/rolls/{id}` | 更正原卷长度/备注或成卷备注；下游越界则 409 拒收 |
| `POST` | `/rolls/{id}/splices` | 向成卷追加接段；越界/循环则 409 拒收 |
| `PATCH` | `/splices/{id}` | 更正接段（来源/区间/掉头）；循环或下游越界则 409 拒收 |
| `GET` | `/rolls/{id}/trace?lo=&hi=` | 追溯成卷一段的原卷号、区间、方向与质检登记号 |
| `GET` | `/health` | 健康检查 |

### 复卷纸追溯示例

```bash
# 原卷：登记长度（毫米）并关联质检登记号 1
curl -X POST localhost:8000/rolls/raw -H 'content-type: application/json' \
  -d '{"length_mm":1000,"inspection_id":1}'

# 成卷 3：按接入顺序接两段——原卷1 的 [0,300) 正向、原卷2 的 [100,600) 掉头
curl -X POST localhost:8000/rolls/finished -H 'content-type: application/json' \
  -d '{"splices":[
    {"source_roll_id":1,"source_lo":0,"source_hi":300,"reversed":false},
    {"source_roll_id":2,"source_lo":100,"source_hi":600,"reversed":true}
  ]}'

# 追溯成卷 3 上 [200,400) 这一段（跨越 300mm 处的接缝）
curl 'localhost:8000/rolls/3/trace?lo=200&hi=400'
# {"roll_id":3,"query_lo":200,"query_hi":400,"items":[
#   {"raw_roll_id":1,"inspection_id":1,"raw_lo":200,"raw_hi":300,
#    "direction":"正向","reversed":false},
#   {"raw_roll_id":2,"inspection_id":2,"raw_lo":500,"raw_hi":600,
#    "direction":"反向","reversed":true}]}

# 把原卷 1 缩短到 250：成卷 3 已登记截取 [0,300)，越界 => 整次 409 拒收
curl -X PATCH localhost:8000/rolls/1 -H 'content-type: application/json' \
  -d '{"length_mm":250}'
# {"detail":"长度更正被整次拒收：成卷 3 已登记的接段 [0, 300) 会超出原卷 1
#  更正后的长度 250 毫米，造成下游截取越界，整次拒收"}
```

位置约定：所有位置从卷头起按毫米计，区间均为 `[lo, hi)`——含起点不含终点。
掉头表示该段相对来源卷反向接入；沿接段链递归到原卷时，方向取沿途掉头标记
的异或（奇数次掉头即反向），且掉头接段指向多接段成卷时按其物理顺序反序展开。
来源链成环（含成卷直接或间接引用自己）在任何写入路径上都会被整次拒收。

并发语义：所有写操作在 `BEGIN IMMEDIATE` 串行化事务内完成“校验 + 写入”，
失败即回滚；追溯查询在单一只读快照事务内读取全部相关表，赶上并发更正时
只会看到**完整的旧结果或完整的新结果**，不会读到中间状态。

### 示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# {"detail":"补报与已有计数（或本报告内部）不相容，整份拒收；已有结果保持不变"}
```

## 计算方法

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设前缀和为 `S_k`：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `S_j - S_i = c`；
* 总片数 `N` ⇔ `S_last - S_0 = N`；
* 原子片数非负 ⇔ `S_k <= S_{k+1}`。

目标“合格区间片数”为 `S_hi - S_lo`。其最大值等于差分约束图上 `lo → hi` 的
最短路，最小值等于 `hi → lo` 最短路取负。约束矩阵为连续一矩阵（全幺模），
最短路给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 求最短路，
负环触发时用 Bellman-Ford 复核，避免误判。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、`/docs`。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报全部接收。
* `tests/test_traceability.py`：原卷/成卷登记、接段追加与更正、跨接缝分段、
  来源为另一成卷、单次/两次掉头的坐标与方向、循环拒收（自引用与间接成环）、
  卷长/接段更正导致下游越界整次拒收且旧数据不变、查询校验。
* `tests/test_trace_concurrency.py`：追溯查询与并发更正只看到完整旧/新结果；
  并发相容接段全部接收、并发冲突更正只有一个成功。
