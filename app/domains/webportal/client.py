"""WebPortal-pilot async HTTP client.

Connects to the webportal-pilot API (FastAPI, port 8000) for:
- POST /api/tickets          — create ticket
- GET  /api/tickets/{id}      — get ticket
- GET  /api/tickets/{id}/sql-proposal — generate SQL
- POST /api/tickets/{id}/cmdb-order   — create CMDB order
- GET  /api/tickets           — list/search tickets
- POST /api/tickets/parse     — parse raw text into structured ticket
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("webportal")


# ── Config ──────────────────────────────────────────────────────


@dataclass
class WebPortalConfig:
    """WebPortal-pilot client configuration."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8000"
    timeout_sec: int = 15
    max_retries: int = 2


# ── Client ──────────────────────────────────────────────────────


class WebPortalClient:
    """Async client for webportal-pilot API."""

    def __init__(self, config: WebPortalConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_sec,
            headers={"Content-Type": "application/json"},
        )

    async def close(self):
        await self._client.aclose()

    async def health(self) -> bool:
        """Check if webportal-pilot is reachable."""
        try:
            resp = await self._client.get("/api/health")
            resp.raise_for_status()
            return resp.json().get("status") == "ok"
        except httpx.HTTPError:
            return False

    async def pipeline_run(
        self,
        raw_text: str,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Run the full ticket processing pipeline end-to-end.

        Calls POST /api/pipeline/run on webportal-pilot, which executes:
        ticket_intake → intent_analysis → knowledge_retrieval →
        query_plan → param_extraction → sql_generation → sql_review

        Args:
            raw_text: The raw ticket text to process.
            attachments: Optional list of attachment dicts, each with
                filename / file_path / doc_type / description / size.

        Returns:
            Full pipeline result with ticket, analysis, sql_proposal, steps.
        """
        payload: dict[str, Any] = {"raw_text": raw_text}
        if attachments:
            payload["attachments"] = attachments

        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await self._client.post("/api/pipeline/run", json=payload)
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"Pipeline run completed: ticket_id={data.get('ticket', {}).get('id', '?')}")
                return data
            except httpx.HTTPError as e:
                logger.warning(f"Pipeline run failed (attempt {attempt + 1}): {e}")
                if attempt >= self.config.max_retries:
                    raise
        return {}

    # ── Ticket CRUD ──────────────────────────────────────────────

    async def create_ticket(
        self,
        flow_no: str,
        title: str,
        ticket_type: str,
        detail: str,
        data_content: str = "",
        due_date: str | None = None,
        raw_text: str = "",
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Create a ticket.

        Args:
            flow_no: 流程编号, e.g. "DATA2026081301"
            title: 工单主题
            ticket_type: 类型, e.g. "数据修改"
            detail: 详情描述
            data_content: 涉及的数据内容
            due_date: 期望完成日期 "YYYY-MM-DD"
            raw_text: 原始文本
            attachments: 附件列表

        Returns:
            Created ticket dict with id, status, etc.
        """
        payload = {
            "flow_no": flow_no or "",
            "title": title,
            "type": ticket_type,
            "detail": detail,
            "data_content": data_content or "",
            "raw_text": raw_text or "",
            "attachments": attachments or [],
        }
        if due_date:
            payload["due_date"] = due_date

        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await self._client.post("/api/tickets", json=payload)
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"Ticket created: id={data.get('id', '?')}, flow_no={flow_no}")
                return data
            except httpx.HTTPError as e:
                logger.warning(f"Ticket create failed (attempt {attempt + 1}): {e}")
                if attempt >= self.config.max_retries:
                    raise
        return {}

    async def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """Get a ticket by ID."""
        try:
            resp = await self._client.get(f"/api/tickets/{ticket_id}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"Get ticket {ticket_id} failed: {e}")
            return None

    async def list_tickets(
        self,
        keyword: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List/search tickets."""
        params: dict[str, Any] = {"limit": limit}
        if keyword:
            params["keyword"] = keyword
        if status:
            params["status"] = status
        if ticket_type:
            params["type"] = ticket_type

        try:
            resp = await self._client.get("/api/tickets", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("items", [])
        except httpx.HTTPError as e:
            logger.warning(f"List tickets failed: {e}")
            return []

    # ── SQL Proposal ─────────────────────────────────────────────

    async def get_sql_proposal(self, ticket_id: str) -> dict[str, Any] | None:
        """Generate a SQL proposal for a ticket.

        Returns:
            dict with reasoning, sql_text, risk, review (confidence, verdict, items).
        """
        try:
            resp = await self._client.get(f"/api/tickets/{ticket_id}/sql-proposal")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"SQL proposal failed for {ticket_id}: {e}")
            return None

    # ── CMDB Order ───────────────────────────────────────────────

    async def create_cmdb_order(self, ticket_id: str) -> dict[str, Any] | None:
        """Create a CMDB order for a ticket (currently mock)."""
        try:
            resp = await self._client.post(f"/api/tickets/{ticket_id}/cmdb-order")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"CMDB order creation failed for {ticket_id}: {e}")
            return None

    # ── Parse ────────────────────────────────────────────────────

    async def parse_ticket(self, raw_text: str) -> dict[str, Any] | None:
        """Parse raw WebPortal text into structured ticket fields."""
        try:
            resp = await self._client.post("/api/tickets/parse", json={"raw_text": raw_text})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"Parse ticket failed: {e}")
            return None

    # ── Full pipeline ────────────────────────────────────────────

    async def process_ticket(
        self,
        flow_no: str,
        title: str,
        ticket_type: str,
        detail: str,
        data_content: str = "",
        due_date: str | None = None,
        raw_text: str = "",
    ) -> dict[str, Any]:
        """Full pipeline: create ticket → generate SQL proposal.

        Returns:
            {"ticket": {...}, "sql": {...}}
        """
        ticket = await self.create_ticket(
            flow_no=flow_no,
            title=title,
            ticket_type=ticket_type,
            detail=detail,
            data_content=data_content,
            due_date=due_date,
            raw_text=raw_text,
        )
        ticket_id = ticket.get("id", "")
        sql = None
        if ticket_id:
            sql = await self.get_sql_proposal(ticket_id)
        return {"ticket": ticket, "sql": sql}


# ── Singleton ───────────────────────────────────────────────────

_client: WebPortalClient | None = None


def get_client(config: WebPortalConfig | None = None) -> WebPortalClient:
    global _client
    if _client is None:
        _client = WebPortalClient(config or WebPortalConfig())
    return _client


def set_client(client: WebPortalClient):
    global _client
    _client = client


def reset_client():
    global _client
    _client = None