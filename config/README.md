# Runtime configuration

AlphaFactorService只使用一份部署配置：`runtime.local.yaml`。

首次部署：

```bash
cp config/runtime.example.yaml config/runtime.local.yaml
```

`runtime.local.yaml`已被Git忽略，可保存本机数据库密码和绝对存储路径。配置分区：

- `service`：统一Factor API与模型研究API的监听地址。
- `clickhouse`：因子库与模型预测库的共享连接。
- `control_database`：模型任务、版本、事件和产物元数据的PostgreSQL控制库。
- `sources.factor`：因子计算源表。
- `sources.research`：模型训练行情源库。
- `research`：训练调度器的工作目录与正式模型产物目录。

训练工作文件和正式模型产物分别由`research.storage.work_root`和
`research.storage.model_artifacts_root`管理。推荐在服务器或旧Mac上设置为容量充足的绝对路径：

```yaml
research:
  storage:
    work_root: /Volumes/QuantData/alphafactor/research-work
    model_artifacts_root: /Volumes/QuantData/alphafactor/model-artifacts
```

若确实需要从另一位置读取配置，可设置
`ALPHA_FACTOR_RUNTIME_CONFIG=/absolute/path/runtime.local.yaml`。它只选择配置文件，不覆盖
YAML里的业务字段。
