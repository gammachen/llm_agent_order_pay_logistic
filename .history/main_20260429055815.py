"""
企业订单处理中台 — CrewAI 异常处理与恢复完整示例
=======================================================
场景：电商/零售企业内部订单处理，涉及4个内部系统接口：
  - 库存系统 (ERP)
  - 支付网关 (Payment Gateway)
  - 物流路由 (WMS/TMS)
  - 通知服务 (消息中台)

异常处理策略：
  1. RetryHandler  — 指数退避重试（最多3次）
  2. FallbackHandler — 降级到备用接口/离线队列
  3. RollbackHandler — 补偿事务（Saga模式）
  4. EscalationHandler — 钉钉/企微告警 + 工单升级
"""

import time
import random
import logging
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from crewai import Agent, Task, Crew, Process
from crewai.tools import tool

# ─────────────────────────────────────────────
# 配置日志
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("OrderPlatform")


# ─────────────────────────────────────────────
# 枚举 & 数据结构
# ─────────────────────────────────────────────
class OrderStatus(Enum):
    PENDING        = "pending"
    INVENTORY_HOLD = "inventory_hold"
    PAYMENT_DONE   = "payment_done"
    LOGISTICS_ASSIGNED = "logistics_assigned"
    COMPLETED      = "completed"
    FAILED         = "failed"
    PARTIAL        = "partial_success"  # 降级完成


class ExceptionType(Enum):
    TOOL_TIMEOUT      = "tool_timeout"
    API_500           = "api_500_error"
    INSUFFICIENT_STOCK = "insufficient_stock"
    PAYMENT_DECLINED  = "payment_declined"
    LOGISTICS_UNAVAIL = "logistics_unavailable"
    NOTIFY_FAILED     = "notification_failed"


@dataclass
class OrderContext:
    """订单上下文 — 在 Agent 间共享的状态"""
    order_id:     str = field(default_factory=lambda: f"ORD-{uuid.uuid4().hex[:8].upper()}")
    sku:          str = "SKU-LAPTOP-X1"
    quantity:     int = 2
    amount_yuan:  float = 12999.00
    customer_id:  str = "CUST-88421"
    status:       OrderStatus = OrderStatus.PENDING

    # 记录每一步结果
    inventory_hold_id: Optional[str] = None
    payment_txn_id:    Optional[str] = None
    logistics_waybill: Optional[str] = None
    notify_sent:       bool = False

    # 异常与重试日志
    exception_log:  list = field(default_factory=list)
    retry_count:    dict = field(default_factory=dict)
    rollback_steps: list = field(default_factory=list)

    def log_exception(self, step: str, exc_type: ExceptionType, detail: str):
        entry = {
            "ts": datetime.now().isoformat(),
            "step": step,
            "type": exc_type.value,
            "detail": detail,
        }
        self.exception_log.append(entry)
        logger.warning(f"[{self.order_id}] ⚠️  {step} | {exc_type.value} | {detail}")

    def log_rollback(self, action: str):
        self.rollback_steps.append({"ts": datetime.now().isoformat(), "action": action})
        logger.info(f"[{self.order_id}] ↩️  Rollback: {action}")

    def summary(self) -> str:
        return (
            f"订单={self.order_id} 状态={self.status.value} "
            f"库存占用={self.inventory_hold_id} 支付={self.payment_txn_id} "
            f"运单={self.logistics_waybill} 通知={self.notify_sent} "
            f"异常次数={len(self.exception_log)} 回滚={len(self.rollback_steps)}"
        )


# 全局订单上下文（在 CrewAI tool 闭包中引用）
ORDER_CTX = OrderContext()


# ─────────────────────────────────────────────
# 工具层 — 模拟真实内部接口（带随机故障注入）
# ─────────────────────────────────────────────
def _simulate_flaky_call(service: str, success_rate: float = 0.5) -> tuple[bool, str]:
    """模拟不稳定的内部服务调用"""
    ok = random.random() < success_rate
    if not ok:
        fault = random.choice([
            ExceptionType.TOOL_TIMEOUT,
            ExceptionType.API_500,
        ])
        return False, fault.value
    return True, "ok"


# ── 库存工具 ──────────────────────────────────
@tool("inventory_hold")
def inventory_hold(sku: str, qty: int) -> str:
    """
    调用 ERP 库存系统，预占指定 SKU 的库存数量。
    入参: sku=SKU编码, qty=数量
    返回: hold_id 或错误描述
    """
    logger.info(f"[Inventory] 尝试预占 {sku} x{qty}")
    ok, reason = _simulate_flaky_call("inventory", success_rate=0.5)
    if not ok:
        raise RuntimeError(f"库存系统调用失败: {reason} | sku={sku}")
    hold_id = f"HOLD-{uuid.uuid4().hex[:6].upper()}"
    ORDER_CTX.inventory_hold_id = hold_id
    ORDER_CTX.status = OrderStatus.INVENTORY_HOLD
    logger.info(f"[Inventory] ✅ 预占成功 hold_id={hold_id}")
    return f"SUCCESS|hold_id={hold_id}"


@tool("inventory_release")
def inventory_release(hold_id: str) -> str:
    """释放已预占的库存（用于回滚）"""
    logger.info(f"[Inventory] 释放预占 hold_id={hold_id}")
    ORDER_CTX.inventory_hold_id = None
    ORDER_CTX.log_rollback(f"释放库存 hold_id={hold_id}")
    return f"RELEASED|hold_id={hold_id}"


@tool("inventory_query_cache")
def inventory_query_cache(sku: str) -> str:
    """
    降级方案：查询本地库存缓存（只读，不预占）
    返回预估库存水位，用于后续离线排队
    """
    logger.info(f"[Inventory-Fallback] 查询本地缓存 sku={sku}")
    # 缓存命中率高，模拟90%成功
    ok, _ = _simulate_flaky_call("cache", success_rate=0.9)
    if ok:
        return f"CACHE_HIT|sku={sku}|estimated_qty=5|note=进入待确认队列"
    return f"CACHE_MISS|sku={sku}"


# ── 支付工具 ──────────────────────────────────
@tool("payment_charge")
def payment_charge(order_id: str, amount: float, customer_id: str) -> str:
    """
    调用支付网关扣款。
    入参: order_id, amount(元), customer_id
    返回: txn_id 或错误描述
    """
    logger.info(f"[Payment] 扣款 order={order_id} amount=¥{amount}")
    ok, reason = _simulate_flaky_call("payment", success_rate=0.55)
    if not ok:
        raise RuntimeError(f"支付网关异常: {reason} | order={order_id}")
    txn_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"
    ORDER_CTX.payment_txn_id = txn_id
    ORDER_CTX.status = OrderStatus.PAYMENT_DONE
    logger.info(f"[Payment] ✅ 扣款成功 txn_id={txn_id}")
    return f"SUCCESS|txn_id={txn_id}"


@tool("payment_refund")
def payment_refund(txn_id: str) -> str:
    """原路退款（支付回滚）"""
    logger.info(f"[Payment] 退款 txn_id={txn_id}")
    ORDER_CTX.payment_txn_id = None
    ORDER_CTX.log_rollback(f"退款 txn_id={txn_id}")
    return f"REFUNDED|txn_id={txn_id}"


@tool("payment_offline_queue")
def payment_offline_queue(order_id: str, amount: float) -> str:
    """降级：将支付请求写入离线消息队列，异步处理"""
    logger.info(f"[Payment-Fallback] 写入离线队列 order={order_id}")
    queue_id = f"Q-{uuid.uuid4().hex[:6].upper()}"
    return f"QUEUED|queue_id={queue_id}|note=将在30分钟内完成扣款"


# ── 物流工具 ──────────────────────────────────
@tool("logistics_route")
def logistics_route(order_id: str, customer_id: str) -> str:
    """
    调用 TMS 物流路由接口，分配承运商与运单。
    入参: order_id, customer_id
    返回: waybill_no 或错误描述
    """
    logger.info(f"[Logistics] 分配运单 order={order_id}")
    ok, reason = _simulate_flaky_call("logistics", success_rate=0.6)
    if not ok:
        raise RuntimeError(f"物流接口不可用: {reason} | order={order_id}")
    waybill = f"SF-{random.randint(10000000, 99999999)}"
    ORDER_CTX.logistics_waybill = waybill
    ORDER_CTX.status = OrderStatus.LOGISTICS_ASSIGNED
    logger.info(f"[Logistics] ✅ 运单分配成功 waybill={waybill}")
    return f"SUCCESS|waybill={waybill}"


@tool("logistics_manual_queue")
def logistics_manual_queue(order_id: str) -> str:
    """降级：加入人工调度队列，运营人员手动分配承运商"""
    logger.info(f"[Logistics-Fallback] 进入人工调度队列 order={order_id}")
    ticket = f"MANUAL-{uuid.uuid4().hex[:6].upper()}"
    return f"MANUAL_QUEUE|ticket={ticket}|note=1小时内运营处理"


# ── 通知工具 ──────────────────────────────────
@tool("notify_customer")
def notify_customer(customer_id: str, order_id: str, message: str) -> str:
    """
    调用消息中台发送短信/APP推送通知。
    入参: customer_id, order_id, message
    返回: 发送结果
    """
    logger.info(f"[Notify] 发送通知 customer={customer_id}")
    ok, reason = _simulate_flaky_call("notify", success_rate=0.65)
    if not ok:
        raise RuntimeError(f"通知服务异常: {reason}")
    ORDER_CTX.notify_sent = True
    logger.info(f"[Notify] ✅ 通知发送成功")
    return f"SUCCESS|channel=sms+push"


@tool("notify_via_email_fallback")
def notify_via_email_fallback(customer_id: str, order_id: str) -> str:
    """降级：通过邮件发送订单确认（备用渠道）"""
    logger.info(f"[Notify-Fallback] 邮件兜底通知 customer={customer_id}")
    ORDER_CTX.notify_sent = True
    return f"EMAIL_SENT|note=短信通道故障，已发送邮件确认"


@tool("alert_ops_team")
def alert_ops_team(order_id: str, error_summary: str) -> str:
    """
    告警升级：向企业微信/钉钉运维群发送告警（EscalationHandler）
    """
    logger.error(
        f"[ESCALATION] 🚨 订单 {order_id} 需要人工介入 | {error_summary}"
    )
    ticket_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
    return f"ALERTED|incident={ticket_id}|channel=企业微信运维群"


# ─────────────────────────────────────────────
# 重试装饰器（指数退避）
# ─────────────────────────────────────────────
def with_retry(step_name: str, max_retries: int = 3, base_delay: float = 1.0):
    """
    指数退避重试装饰器
    第1次失败 → 等待 1s → 重试
    第2次失败 → 等待 2s → 重试
    第3次失败 → 等待 4s → 抛出异常
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            ctx = ORDER_CTX
            ctx.retry_count[step_name] = 0
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    if attempt > 0:
                        logger.info(f"[Retry] ✅ {step_name} 第{attempt+1}次尝试成功")
                    return result
                except Exception as e:
                    last_exc = e
                    ctx.retry_count[step_name] = attempt + 1
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"[Retry] ⏳ {step_name} 第{attempt+1}次失败，"
                            f"{delay:.0f}s 后重试 | {e}"
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"[Retry] ❌ {step_name} 已重试{max_retries}次，全部失败"
                        )
            raise last_exc
        return wrapper
    return decorator


# ─────────────────────────────────────────────
# 定义 CrewAI Agents
# ─────────────────────────────────────────────
inventory_agent = Agent(
    role="库存管理专员",
    goal=(
        "对订单进行库存预占，确保货品可以发货。"
        "如果主接口失败，自动重试3次（指数退避），"
        "仍失败则切换至本地缓存查询，并将订单写入待确认队列。"
    ),
    backstory=(
        "你是企业ERP库存系统的专属接入Agent，熟悉库存预占、"
        "释放和缓存降级策略。你的首要任务是确保库存操作有结果——"
        "哪怕是降级结果，也要记录明确的状态供后续环节决策。"
    ),
    tools=[inventory_hold, inventory_release, inventory_query_cache],
    verbose=True,
)

payment_agent = Agent(
    role="支付处理专员",
    goal=(
        "完成订单扣款操作。主通道失败时重试3次，"
        "超出重试上限则切换离线队列，确保资金链路不断。"
        "若检测到库存已被回滚，则跳过支付。"
    ),
    backstory=(
        "你对接企业支付网关与核心银行系统，深知支付幂等性的重要性。"
        "每次调用前你会先检查是否已存在交易ID，避免重复扣款。"
        "遇到网关超时，你优先走离线队列，保证资金安全。"
    ),
    tools=[payment_charge, payment_refund, payment_offline_queue],
    verbose=True,
)

logistics_agent = Agent(
    role="物流调度专员",
    goal=(
        "为订单分配承运商并生成运单号。"
        "主路由失败时重试3次，仍失败则推入人工调度队列，"
        "并通过工单系统通知运营同事处理。"
    ),
    backstory=(
        "你熟悉顺丰、京东物流、菜鸟等多个承运商API。"
        "当自动路由失败时，你会根据订单目的地和历史成功率"
        "推荐最合适的人工调度方案，不让包裹卡在中间环节。"
    ),
    tools=[logistics_route, logistics_manual_queue, alert_ops_team],
    verbose=True,
)

notification_agent = Agent(
    role="通知服务专员",
    goal=(
        "向客户发送订单状态通知（短信+APP推送）。"
        "主渠道失败时自动切换邮件通道，确保客户知情。"
        "若所有通知渠道均失败，上报运维群告警。"
    ),
    backstory=(
        "你管理企业消息中台，支持短信、APP推送、邮件、企业微信多渠道。"
        "你的原则是：客户一定要收到通知，即使渠道降级了也要发出去。"
        "你会在通知内容中说明当前订单状态（含降级情况），不让客户蒙在鼓里。"
    ),
    tools=[notify_customer, notify_via_email_fallback, alert_ops_team],
    verbose=True,
)

exception_recovery_agent = Agent(
    role="异常恢复编排专员",
    goal=(
        "监控整个订单处理链路，当出现不可恢复的异常时，"
        "执行补偿事务（Saga回滚）：先退款，再释放库存，最后告警升级。"
        "输出完整的订单处理结论报告。"
    ),
    backstory=(
        "你是订单中台的最后一道防线，负责异常恢复决策。"
        "你熟悉Saga补偿模式：当支付成功但物流失败时，必须按逆序回滚——"
        "先退款后释放库存，保持数据一致性。"
        "你会生成一份结构化的事后报告，供SRE团队分析根因。"
    ),
    tools=[payment_refund, inventory_release, alert_ops_team],
    verbose=True,
)


# ─────────────────────────────────────────────
# 定义 Tasks（带完整异常处理逻辑描述）
# ─────────────────────────────────────────────
def make_tasks(ctx: OrderContext):
    t_inventory = Task(
        description=f"""
        订单信息:
          - 订单号: {ctx.order_id}
          - SKU: {ctx.sku}
          - 数量: {ctx.quantity}
          - 客户: {ctx.customer_id}

        执行步骤:
        1. 调用 inventory_hold 预占库存（{ctx.sku} x{ctx.quantity}）
        2. 如果工具调用失败（RuntimeError），最多重试3次，每次间隔翻倍（1s→2s→4s）
        3. 三次全部失败后，调用 inventory_query_cache 查询缓存库存
        4. 记录最终状态（成功的hold_id 或 缓存降级标记）供下一步使用

        重要: 即使降级，也必须返回明确的状态字符串。
        """,
        expected_output="库存预占结果: 成功时返回hold_id，降级时返回CACHE_HIT并说明进入队列",
        agent=inventory_agent,
    )

    t_payment = Task(
        description=f"""
        在库存预占成功（或降级排队）后，处理支付。

        订单信息:
          - 订单号: {ctx.order_id}
          - 金额: ¥{ctx.amount_yuan}
          - 客户: {ctx.customer_id}

        执行步骤:
        1. 调用 payment_charge 发起扣款
        2. 如果失败，最多重试3次（指数退避）
        3. 三次失败后调用 payment_offline_queue 写入离线队列（降级）
        4. 若库存阶段已完全失败（没有hold_id也没有缓存命中），
           则跳过支付，直接返回 SKIP_PAYMENT 状态

        幂等要求: 确认是否已存在 txn_id，避免重复扣款。
        """,
        expected_output="支付结果: 成功txn_id / 离线队列queue_id / 跳过原因",
        agent=payment_agent,
    )

    t_logistics = Task(
        description=f"""
        支付完成（或进入队列）后，为订单分配物流运单。

        订单信息:
          - 订单号: {ctx.order_id}
          - 客户: {ctx.customer_id}

        执行步骤:
        1. 调用 logistics_route 自动路由，获取运单号
        2. 失败后重试3次
        3. 三次失败后调用 logistics_manual_queue 进入人工调度
        4. 同时调用 alert_ops_team 通知运营组跟进
           告警内容: "订单{ctx.order_id}物流自动路由失败，需人工分配承运商"

        注意: 物流失败不触发支付回滚，只标记为部分完成。
        """,
        expected_output="物流结果: 运单号waybill / 人工调度ticket_id",
        agent=logistics_agent,
    )

    t_notify = Task(
        description=f"""
        订单处理完毕后，向客户发送通知。

        通知信息:
          - 客户ID: {ctx.customer_id}
          - 订单号: {ctx.order_id}

        执行步骤:
        1. 调用 notify_customer 发送短信+APP推送
           消息内容: "您的订单 {ctx.order_id} 已确认，正在处理中"
        2. 失败时重试2次
        3. 仍然失败则调用 notify_via_email_fallback 发送邮件
        4. 邮件也失败时调用 alert_ops_team 告警
           告警内容: "订单{ctx.order_id}所有通知渠道均失败，需人工联系客户"

        无论通知结果如何，不影响订单状态。
        """,
        expected_output="通知结果: 发送成功渠道 / 告警incident_id",
        agent=notification_agent,
    )

    t_recovery = Task(
        description=f"""
        汇总整个订单 {ctx.order_id} 的处理结果，执行必要的回滚或告警。

        判断逻辑:
        1. 如果支付成功 (有txn_id) 但物流和库存均失败：
           a. 调用 payment_refund 退款 (txn_id来自上一步)
           b. 调用 inventory_release 释放库存 (hold_id来自库存步骤)
           c. 调用 alert_ops_team 发送人工介入告警

        2. 如果仅通知失败：只告警，不回滚

        3. 输出结构化的处理报告，包含:
           - 最终订单状态（COMPLETED / PARTIAL / FAILED）
           - 各步骤结果摘要
           - 触发的异常次数
           - 执行的回滚操作
           - 建议的人工跟进项

        订单号: {ctx.order_id}
        """,
        expected_output="完整的订单处理结论报告（结构化文本）",
        agent=exception_recovery_agent,
    )

    return [t_inventory, t_payment, t_logistics, t_notify, t_recovery]


# ─────────────────────────────────────────────
# 主流程入口
# ─────────────────────────────────────────────
def run_order_processing():
    ctx = ORDER_CTX
    print("\n" + "═" * 60)
    print(f"  企业订单处理中台 — 异常处理演示")
    print(f"  订单号: {ctx.order_id}")
    print(f"  商品: {ctx.sku} x{ctx.quantity}")
    print(f"  金额: ¥{ctx.amount_yuan:,.2f}")
    print(f"  客户: {ctx.customer_id}")
    print("═" * 60 + "\n")

    tasks = make_tasks(ctx)

    crew = Crew(
        agents=[
            inventory_agent,
            payment_agent,
            logistics_agent,
            notification_agent,
            exception_recovery_agent,
        ],
        tasks=tasks,
        process=Process.sequential,  # 顺序执行，保证事务依赖
        verbose=True,
    )

    result = crew.kickoff()

    print("\n" + "═" * 60)
    print("  📊 订单上下文最终状态")
    print("═" * 60)
    print(ctx.summary())
    print(f"\n  异常记录（{len(ctx.exception_log)}条）:")
    for log in ctx.exception_log:
        print(f"    [{log['ts']}] {log['step']} → {log['type']}: {log['detail']}")
    print(f"\n  回滚操作（{len(ctx.rollback_steps)}条）:")
    for rb in ctx.rollback_steps:
        print(f"    [{rb['ts']}] {rb['action']}")
    print(f"\n  重试次数: {ctx.retry_count}")
    print("═" * 60)

    return result


if __name__ == "__main__":
    run_order_processing()
