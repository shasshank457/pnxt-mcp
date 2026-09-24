"""Reusable PointNXT business workflow prompts."""

import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from tools.orders import get_orders
from tools.products import get_product_summary

logger = logging.getLogger(__name__)


def _items(result: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        ((result.get("data") or {}).get("items") or [])
        if isinstance(result, dict)
        else []
    )


def _amount(order: dict[str, Any]) -> float:
    return order.get("grandTotal", order.get("orderTotal", 0)) or 0


def _customer(order: dict[str, Any]) -> str:
    customer = order.get("customer") or {}
    return (
        customer
        if isinstance(customer, str)
        else customer.get("name") or customer.get("email") or "Unknown"
    )


async def _run(name: str, fn, *args, **kwargs) -> Any:
    started = perf_counter()
    logger.info("Prompt invoked: %s arguments=%s", name, kwargs)
    try:
        return await fn(*args, **kwargs)
    finally:
        logger.info(
            "Prompt tool completed: %s execution_ms=%.2f",
            name,
            (perf_counter() - started) * 1000,
        )


def register_prompts(mcp) -> None:
    @mcp.prompt(
        name="daily_orders_summary",
        title="Daily Orders Summary",
        description="Summarize PointNXT orders for a date.",
    )
    async def daily_orders_summary(date: str | None = None) -> str:
        day = date or __import__("datetime").date.today().isoformat()
        result = await _run(
            "daily_orders_summary", get_orders, limit=100, start_date=day, end_date=day
        )
        orders = _items(result)
        if not orders:
            return f"# Daily Orders Summary\n\nNo orders found for {day}."
        pending = sum(o.get("orderStatus") == "PENDING" for o in orders)
        cancelled = sum(o.get("orderStatus") == "CANCELLED" for o in orders)
        fulfilled = sum(
            o.get("orderStatus") in {"FULFILLED", "DELIVERED"} for o in orders
        )
        revenue = sum(_amount(o) for o in orders)
        customers = {}
        for order in orders:
            customers[_customer(order)] = customers.get(_customer(order), 0) + 1
        top = sorted(customers.items(), key=lambda x: x[1], reverse=True)[:5]
        return "\n".join(
            [
                f"# Daily Orders Summary\n\nDate: {day}",
                "## Order Metrics",
                f"- Total Orders: {len(orders)}",
                f"- Pending: {pending}",
                f"- Cancelled: {cancelled}",
                f"- Fulfilled: {fulfilled}",
                f"- Revenue: {revenue:,.2f}",
                "\n## Top Customers",
                *[f"- {name}: {count} order(s)" for name, count in top],
            ]
        )

    @mcp.prompt(
        name="pending_orders_report",
        title="Pending Orders Report",
        description="Review pending PointNXT orders.",
    )
    async def pending_orders_report() -> str:
        orders = _items(
            await _run(
                "pending_orders_report", get_orders, limit=100, order_status="PENDING"
            )
        )
        if not orders:
            return "# Pending Orders Report\n\nNo pending orders found."
        lines = [
            "# Pending Orders Report",
            "",
            f"Pending orders: {len(orders)}",
            "",
            "| Order | Customer | Total | Warehouse |",
            "|---|---|---:|---|",
        ]
        lines += [
            f"| {o.get('orderNo', o.get('id', '-'))} | {_customer(o)} | {_amount(o):,.2f} | {o.get('warehouse', '-')} |"
            for o in orders
        ]
        return "\n".join(lines)

    @mcp.prompt(
        name="cancelled_orders_analysis",
        title="Cancelled Orders Analysis",
        description="Analyze cancelled PointNXT orders.",
    )
    async def cancelled_orders_analysis() -> str:
        orders = _items(
            await _run(
                "cancelled_orders_analysis",
                get_orders,
                limit=100,
                order_status="CANCELLED",
            )
        )
        if not orders:
            return "# Cancelled Orders Analysis\n\nNo cancelled orders found."
        return (
            f"# Cancelled Orders Analysis\n\nCancelled orders: {len(orders)}\n\n"
            + "\n".join(
                f"- {_customer(o)} — {_amount(o):,.2f} — {o.get('paymentMethod', 'Unknown')}"
                for o in orders
            )
        )

    @mcp.prompt(
        name="product_catalog_summary",
        title="Product Catalog Summary",
        description="Summarize the PointNXT product catalog.",
    )
    async def product_catalog_summary() -> str:
        result = await _run("product_catalog_summary", get_product_summary)
        data = result.get("data", result) if isinstance(result, dict) else {}
        if not data:
            return "# Product Catalog Summary\n\nNo product summary is available."
        return "# Product Catalog Summary\n\n" + "\n".join(
            f"- **{key.replace('_', ' ').title()}**: {value}"
            for key, value in data.items()
        )

    @mcp.prompt(
        name="customer_order_lookup",
        title="Customer Order Lookup",
        description="Find orders for a customer by name, email, or phone.",
    )
    async def customer_order_lookup(
        name: str | None = None, email: str | None = None, phone: str | None = None
    ) -> str:
        if not any((name, email, phone)):
            return "Please provide a customer name, email, or phone number."
        orders = _items(
            await _run(
                "customer_order_lookup",
                get_orders,
                limit=100,
                customer_email=email,
                customer_phone=phone,
            )
        )
        if name:
            orders = [o for o in orders if name.lower() in _customer(o).lower()]
        if not orders:
            return "# Customer Order Lookup\n\nNo matching orders found."
        return "# Customer Order Lookup\n\n" + "\n".join(
            f"- {o.get('orderNo', o.get('id', '-'))}: {o.get('orderStatus', 'Unknown')} — {_amount(o):,.2f}"
            for o in orders
        )

    @mcp.prompt(
        name="weekly_sales_report",
        title="Weekly Sales Report",
        description="Summarize PointNXT sales for a date range.",
    )
    async def weekly_sales_report(
        start_date: str | None = None, end_date: str | None = None
    ) -> str:
        end = (
            date.fromisoformat(end_date)
            if end_date
            else datetime.now(timezone.utc).date()
        )
        start = (
            date.fromisoformat(start_date) if start_date else end - timedelta(days=6)
        )
        if start > end:
            return "Invalid date range: start_date cannot be after end_date."
        orders = _items(
            await _run(
                "weekly_sales_report",
                get_orders,
                limit=100,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        )
        return f"# Weekly Sales Report\n\nPeriod: {start} to {end}\n\n- Total Orders: {len(orders)}\n- Revenue: {sum(_amount(o) for o in orders):,.2f}\n- Cancelled: {sum(o.get('orderStatus') == 'CANCELLED' for o in orders)}\n- Pending: {sum(o.get('orderStatus') == 'PENDING' for o in orders)}"
