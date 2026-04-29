# 企业订单处理中台 — CrewAI 异常处理与恢复

## 项目背景

本示例模拟电商/零售企业订单处理链路中的异常处理场景，
基于文档《第12章：异常处理和恢复》设计，使用 **CrewAI** 框架实现。

---

## 业务场景

订单提交后需要顺序调用 4 个内部系统：

```
客户下单 → 库存预占(ERP) → 支付扣款(网关) → 物流路由(TMS) → 通知发送(消息中台)
```

每个接口都可能出现超时、500错误、业务拒绝等各类故障。

---

## 异常处理策略矩阵

| 服务       | 主路径              | 重试策略          | 降级方案               | 回滚操作         |
|-----------|---------------------|-------------------|-----------------------|-----------------|
| 库存(ERP)  | inventory_hold      | 3次指数退避       | 查询本地缓存+排队       | inventory_release |
| 支付网关   | payment_charge      | 3次指数退避       | 写入离线消息队列        | payment_refund   |
| 物流(TMS)  | logistics_route     | 3次指数退避       | 人工调度队列+告警       | 无（不阻断）      |
| 消息中台   | notify_customer     | 2次重试           | 邮件兜底通知           | 无（不阻断）      |

---

## 恢复模式（Saga 补偿事务）

当支付成功但后续步骤不可恢复时，按**逆序**执行补偿：

```
物流失败（不可恢复）
  → 退款 (payment_refund)
  → 释放库存 (inventory_release)
  → 告警升级 (alert_ops_team → 企业微信/钉钉)
```

---

## 架构图

```
OrderOrchestrator (CrewAI Sequential Crew)
├── InventoryAgent    → ERP库存接口 + 缓存降级
├── PaymentAgent      → 支付网关 + 离线队列降级
├── LogisticsAgent    → TMS物流路由 + 人工调度降级
├── NotifyAgent       → 消息中台 + 邮件兜底
└── ExceptionRecoveryAgent → Saga回滚 + 告警 + 报告输出

SharedStateStore (OrderContext dataclass)
└── 在 tool 闭包中共享状态、异常日志、回滚记录
```

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行演示（随机故障注入，每次结果不同）
python main.py
```

---

## 核心设计要点

### 1. 指数退避重试（`with_retry` 装饰器）
```python
# 第1次失败 → 等1s → 第2次失败 → 等2s → 第3次失败 → 等4s → 放弃
with_retry(step_name="inventory", max_retries=3, base_delay=1.0)
```

### 2. 幂等性保障
- 支付前检查 `txn_id` 是否已存在，避免重复扣款
- 库存释放使用 `hold_id`，确保只释放本次预占的库存

### 3. SharedStateStore（`OrderContext`）
- 跨 Agent 共享订单状态，无需 Agent 之间直接通信
- 记录完整的异常日志和回滚记录，用于事后审计

### 4. 故障注入模拟
```python
def _simulate_flaky_call(service, success_rate=0.5):
    # 调整 success_rate 模拟不同稳定性场景
    # 0.3 = 高故障率（测试降级路径）
    # 0.9 = 稳定（测试正常路径）
```

---

## 扩展建议

- **接入真实内部接口**：将 `@tool` 函数替换为真实的 HTTP/gRPC 调用
- **持久化 StateStore**：将 `OrderContext` 改为写入 Redis，支持进程重启后恢复
- **接入 OpenTelemetry**：在 `log_exception` 中添加链路追踪 span
- **告警渠道**：在 `alert_ops_team` 中调用企业微信/钉钉 Webhook API
- **Hierarchical Process**：对于更复杂的并发场景，改用 `Process.hierarchical`
