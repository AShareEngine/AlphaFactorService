# Runtime configuration

AlphaFactorService只使用一份部署配置：`runtime.local.yaml`。

首次部署：

```bash
cp config/runtime.example.yaml config/runtime.local.yaml
```

`runtime.local.yaml`已被Git忽略，可保存本机数据库密码和绝对存储路径。配置分区：

- `service`：Factor API监听地址和本机研究网关地址。
- `clickhouse`：因子库与模型预测库的共享连接。
- `sources.factor`：因子计算源表。
- `sources.research`：模型训练行情源库。
- `research`：训练调度服务、AlphaBlocks API及研究文件存储目录。

训练产生的所有本地文件都放在`research.storage.root`下面。推荐在服务器或旧Mac上设置为
容量充足的绝对路径，例如：

```yaml
research:
  storage:
    root: /Volumes/QuantData/alphafactor/research
```

若确实需要从另一位置读取配置，可设置
`ALPHA_FACTOR_RUNTIME_CONFIG=/absolute/path/runtime.local.yaml`。它只选择配置文件，不覆盖
YAML里的业务字段。
