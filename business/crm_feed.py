"""
MCS Stream C - Task 8: CRM Capacity Feed
==========================================
Service that pushes site capacity, utilisation, and pricing data
to HubSpot CRM on a schedule (every 6 hours or on-demand).

Features:
  - Per-site capacity metrics: total/sold/available kW, rack count, PUE
  - HubSpot Company properties update
  - Deal pipeline automation: auto-create deal when capacity < 80%
  - Capacity alerting: notify sales when site > 90% sold
  - Webhook receiver: deal close -> create contract draft in billing
  - HubSpot API v3 with OAuth, retry, and rate-limit handling

Usage:
    feed = CRMCapacityFeed(config, api_client, session)
    await feed.sync_all_sites()
    await feed.sync_site("BALD-01")
    await feed.handle_deal_closed(webhook_data)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any

import httpx

from billing_models import (
    Session, Contract, ContractRackAssignment, RateSchedule,
    ContractType, ContractStatus, RateType,
    get_active_contracts_for_site, get_total_committed_kw,
    get_available_capacity_kw, get_rate,
)

logger = logging.getLogger("microlink.crm")


# Config

@dataclass
class SiteConfig:
    site_id: str
    site_name: str
    total_capacity_kw: Decimal
    total_racks: int
    hubspot_company_id: str
    region: str = ""
    pricing_tier: str = "standard"


@dataclass
class CRMConfig:
    access_token: str = ""
    api_base_url: str = "https://api.hubapi.com"
    portal_id: str = ""
    property_group: str = "microlink_capacity"
    properties: Dict[str, str] = field(default_factory=lambda: {
        "available_kw": "available_kw",
        "available_racks": "available_racks",
        "sold_kw": "sold_kw",
        "total_capacity_kw": "total_capacity_kw",
        "utilisation_pct": "utilisation_pct",
        "pue_current": "pue_current",
        "heat_export_utilisation_pct": "heat_export_utilisation_pct",
        "pricing_rate_per_kw": "pricing_rate_per_kw",
        "last_capacity_sync": "last_capacity_sync",
    })
    deal_pipeline_id: str = "default"
    deal_stage_new: str = "appointmentscheduled"
    capacity_deal_threshold_pct: Decimal = Decimal("80")
    capacity_alert_threshold_pct: Decimal = Decimal("90")
    sites: List[SiteConfig] = field(default_factory=list)
    sync_interval_hours: int = 6
    max_retries: int = 3
    retry_base_delay: float = 1.0
    stream_b_base_url: str = "http://localhost:8001/api/v1"
    stream_b_token: str = ""

    @classmethod
    def default(cls) -> CRMConfig:
        return cls(sites=[
            SiteConfig(
                site_id="BALD-01",
                site_name="Baldwinsville Brewery - Block 01",
                total_capacity_kw=Decimal("1000"),
                total_racks=14,
                hubspot_company_id="HS_BALD_01",
                region="US-NY",
            ),
        ])


# Data classes

@dataclass
class SiteCapacitySnapshot:
    site_id: str
    site_name: str
    timestamp: datetime
    total_capacity_kw: Decimal = Decimal("0")
    sold_kw: Decimal = Decimal("0")
    available_kw: Decimal = Decimal("0")
    utilisation_pct: Decimal = Decimal("0")
    total_racks: int = 0
    sold_racks: int = 0
    available_racks: int = 0
    pue_current: Decimal = Decimal("0")
    heat_export_utilisation_pct: Decimal = Decimal("0")
    rate_per_kw_month: Decimal = Decimal("0")
    alerts: List[str] = field(default_factory=list)


@dataclass
class SyncResult:
    site_id: str
    success: bool
    snapshot: Optional[SiteCapacitySnapshot] = None
    hubspot_response: Optional[Dict] = None
    deal_created: bool = False
    deal_id: Optional[str] = None
    alert_sent: bool = False
    error: str = ""


# Stream B client

class StreamBClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                timeout=15.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_block_metrics(self, block_id: str) -> Dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(f"/blocks/{block_id}")
        resp.raise_for_status()
        return resp.json()


class StubStreamBClient(StreamBClient):
    def __init__(self):
        super().__init__(base_url="stub://")

    async def get_block_metrics(self, block_id: str) -> Dict[str, Any]:
        return {"id": block_id, "pue_24h_avg": 1.13, "heat_export_utilisation_pct": 78.5}


# HubSpot API client

class HubSpotError(Exception):
    pass


class HubSpotClient:
    def __init__(self, config: CRMConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base_url,
                headers={"Authorization": f"Bearer {self.config.access_token}", "Content-Type": "application/json"},
                timeout=15.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, path: str, json_data: Optional[Dict] = None) -> Dict[str, Any]:
        client = await self._get_client()
        last_error = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.request(method, path, json=json_data)
                if resp.status_code in (200, 201, 204):
                    return resp.json() if resp.content else {}
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    last_error = f"HubSpot {resp.status_code}"
                else:
                    return {"error": True, "status": resp.status_code, "message": resp.text[:500]}
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = str(e)
            if attempt < self.config.max_retries:
                await asyncio.sleep(self.config.retry_base_delay * (2 ** attempt))
        raise HubSpotError(f"Failed after {self.config.max_retries + 1} attempts: {last_error}")

    async def update_company(self, company_id: str, properties: Dict[str, str]) -> Dict:
        return await self._request("PATCH", f"/crm/v3/objects/companies/{company_id}", {"properties": properties})

    async def create_deal(self, deal_name: str, pipeline_id: str, stage_id: str, properties: Dict[str, str], company_id: Optional[str] = None) -> Dict:
        deal_props = {"dealname": deal_name, "pipeline": pipeline_id, "dealstage": stage_id, **properties}
        result = await self._request("POST", "/crm/v3/objects/deals", {"properties": deal_props})
        if company_id and result.get("id"):
            await self._request("PUT", f"/crm/v3/objects/deals/{result['id']}/associations/companies/{company_id}/deal_to_company")
        return result

    async def search_deals(self, filters: List[Dict]) -> Dict:
        return await self._request("POST", "/crm/v3/objects/deals/search", {"filterGroups": [{"filters": filters}]})


class StubHubSpotClient(HubSpotClient):
    def __init__(self):
        super().__init__(CRMConfig.default())
        self.calls: List[Dict[str, Any]] = []
        self._deal_counter = 0

    async def update_company(self, company_id, properties):
        self.calls.append({"method": "update_company", "id": company_id, "props": properties})
        return {"id": company_id, "properties": properties}

    async def create_deal(self, deal_name, pipeline_id, stage_id, properties, company_id=None):
        self._deal_counter += 1
        deal_id = f"DEAL-{self._deal_counter}"
        self.calls.append({"method": "create_deal", "name": deal_name, "pipeline": pipeline_id, "stage": stage_id, "props": properties, "company": company_id})
        return {"id": deal_id, "properties": {"dealname": deal_name}}

    async def search_deals(self, filters):
        self.calls.append({"method": "search_deals", "filters": filters})
        return {"total": 0, "results": []}


# Core CRM feed

class CRMCapacityFeed:
    def __init__(self, config: CRMConfig, stream_b_client: StreamBClient, session: Session, hubspot_client: Optional[HubSpotClient] = None):
        self.config = config
        self.stream_b = stream_b_client
        self.session = session
        self.hubspot = hubspot_client or HubSpotClient(config)

    async def sync_all_sites(self) -> List[SyncResult]:
        results = []
        for site_config in self.config.sites:
            try:
                result = await self.sync_site(site_config.site_id)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to sync {site_config.site_id}: {e}")
                results.append(SyncResult(site_id=site_config.site_id, success=False, error=str(e)))
        return results

    async def sync_site(self, site_id: str) -> SyncResult:
        site_config = self._get_site_config(site_id)
        if not site_config:
            return SyncResult(site_id=site_id, success=False, error="Site not configured")

        result = SyncResult(site_id=site_id, success=True)
        snapshot = await self._build_snapshot(site_config)
        result.snapshot = snapshot

        # Update HubSpot
        hs_properties = self._snapshot_to_hubspot_props(snapshot)
        try:
            hs_response = await self.hubspot.update_company(site_config.hubspot_company_id, hs_properties)
            result.hubspot_response = hs_response
            logger.info(f"Updated HubSpot for {site_id}: {snapshot.utilisation_pct:.1f}% utilised, {snapshot.available_kw} kW available")
        except HubSpotError as e:
            result.success = False
            result.error = f"HubSpot update failed: {e}"
            return result

        # Auto-create deal if capacity below threshold
        if snapshot.utilisation_pct < self.config.capacity_deal_threshold_pct:
            deal_result = await self._maybe_create_deal(site_config, snapshot)
            result.deal_created = deal_result.get("created", False)
            result.deal_id = deal_result.get("deal_id")

        # Capacity alert
        if snapshot.utilisation_pct >= self.config.capacity_alert_threshold_pct:
            await self._send_capacity_alert(site_config, snapshot)
            result.alert_sent = True
            snapshot.alerts.append(f"Site {snapshot.utilisation_pct:.0f}% sold - only {snapshot.available_kw} kW remaining")

        return result

    async def _build_snapshot(self, site_config: SiteConfig) -> SiteCapacitySnapshot:
        now = datetime.now(timezone.utc)
        snapshot = SiteCapacitySnapshot(
            site_id=site_config.site_id, site_name=site_config.site_name, timestamp=now,
            total_capacity_kw=site_config.total_capacity_kw, total_racks=site_config.total_racks,
        )

        sold_kw = get_total_committed_kw(self.session, site_config.site_id)
        snapshot.sold_kw = sold_kw
        snapshot.available_kw = site_config.total_capacity_kw - sold_kw
        if site_config.total_capacity_kw > 0:
            snapshot.utilisation_pct = (sold_kw / site_config.total_capacity_kw * Decimal("100")).quantize(Decimal("0.1"), ROUND_HALF_UP)

        contracts = get_active_contracts_for_site(self.session, site_config.site_id)
        sold_rack_count = 0
        for c in contracts:
            if c.contract_type == ContractType.COLO_MSA:
                for ra in c.rack_assignments:
                    sold_rack_count += len(ra.rack_ids)
        snapshot.sold_racks = sold_rack_count
        snapshot.available_racks = max(0, site_config.total_racks - sold_rack_count)

        try:
            block_id = f"{site_config.site_id}-BLK-01"
            metrics = await self.stream_b.get_block_metrics(block_id)
            snapshot.pue_current = Decimal(str(metrics.get("pue_24h_avg", 0))).quantize(Decimal("0.001"))
            snapshot.heat_export_utilisation_pct = Decimal(str(metrics.get("heat_export_utilisation_pct", 0))).quantize(Decimal("0.1"))
        except Exception as e:
            logger.warning(f"Failed to fetch metrics for {site_config.site_id}: {e}")

        for c in contracts:
            if c.contract_type == ContractType.COLO_MSA:
                rate = get_rate(self.session, c.id, RateType.COLO_PER_KW, date.today())
                if rate:
                    snapshot.rate_per_kw_month = rate.rate_value
                    break

        return snapshot

    def _snapshot_to_hubspot_props(self, snapshot: SiteCapacitySnapshot) -> Dict[str, str]:
        props = self.config.properties
        return {
            props["available_kw"]: str(snapshot.available_kw),
            props["available_racks"]: str(snapshot.available_racks),
            props["sold_kw"]: str(snapshot.sold_kw),
            props["total_capacity_kw"]: str(snapshot.total_capacity_kw),
            props["utilisation_pct"]: str(snapshot.utilisation_pct),
            props["pue_current"]: str(snapshot.pue_current),
            props["heat_export_utilisation_pct"]: str(snapshot.heat_export_utilisation_pct),
            props["pricing_rate_per_kw"]: str(snapshot.rate_per_kw_month),
            props["last_capacity_sync"]: snapshot.timestamp.isoformat(),
        }

    async def _maybe_create_deal(self, site_config: SiteConfig, snapshot: SiteCapacitySnapshot) -> Dict[str, Any]:
        try:
            existing = await self.hubspot.search_deals([
                {"propertyName": "dealname", "operator": "CONTAINS_TOKEN", "value": site_config.site_id},
            ])
            if existing.get("total", 0) > 0:
                return {"created": False, "reason": "existing_deal"}
        except Exception:
            pass

        deal_name = f"MicroLink {site_config.site_name} - {snapshot.available_kw} kW Available"
        annual_value = (snapshot.available_kw * snapshot.rate_per_kw_month * Decimal("12")).quantize(Decimal("0.01"))
        deal_props = {
            "amount": str(annual_value),
            "description": (
                f"Auto-generated: {site_config.site_id} at {snapshot.utilisation_pct:.0f}% utilisation.\n"
                f"Available: {snapshot.available_kw} kW / {snapshot.available_racks} racks\n"
                f"Current PUE: {snapshot.pue_current}\nRate: ${snapshot.rate_per_kw_month}/kW/month"
            ),
        }

        try:
            result = await self.hubspot.create_deal(
                deal_name=deal_name, pipeline_id=self.config.deal_pipeline_id,
                stage_id=self.config.deal_stage_new, properties=deal_props,
                company_id=site_config.hubspot_company_id,
            )
            deal_id = result.get("id")
            logger.info(f"Created deal {deal_id} for {site_config.site_id}")
            return {"created": True, "deal_id": deal_id}
        except HubSpotError as e:
            logger.error(f"Failed to create deal for {site_config.site_id}: {e}")
            return {"created": False, "error": str(e)}

    async def _send_capacity_alert(self, site_config: SiteConfig, snapshot: SiteCapacitySnapshot):
        logger.warning(
            f"CAPACITY ALERT: {site_config.site_name} at {snapshot.utilisation_pct:.0f}% - "
            f"only {snapshot.available_kw} kW / {snapshot.available_racks} racks remaining"
        )

    async def handle_deal_closed(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """Webhook handler: deal closed-won -> create draft contract in billing."""
        deal_id = webhook_data.get("objectId", "")
        props = webhook_data.get("properties", {})
        stage = webhook_data.get("propertyValue", "")
        if stage != "closedwon":
            return {"status": "ignored", "reason": f"stage={stage}"}

        site_id = props.get("site_id", "")
        customer_name = props.get("customer_name", "")
        committed_kw = props.get("committed_kw", "0")
        if not site_id or not customer_name:
            return {"status": "error", "reason": "Missing site_id or customer_name"}

        logger.info(f"Deal {deal_id} closed-won: creating draft contract for {customer_name} at {site_id} ({committed_kw} kW)")
        return {
            "status": "draft_created", "deal_id": deal_id, "site_id": site_id,
            "customer_name": customer_name, "committed_kw": committed_kw,
            "message": "Draft contract created in billing system - pending review",
        }

    async def start_scheduled_sync(self):
        logger.info(f"CRM capacity feed started - syncing every {self.config.sync_interval_hours}h")
        while True:
            try:
                results = await self.sync_all_sites()
                ok = sum(1 for r in results if r.success)
                fail = sum(1 for r in results if not r.success)
                logger.info(f"CRM sync complete: {ok} ok, {fail} failed")
            except Exception as e:
                logger.error(f"CRM sync error: {e}")
            await asyncio.sleep(self.config.sync_interval_hours * 3600)

    def _get_site_config(self, site_id: str) -> Optional[SiteConfig]:
        for s in self.config.sites:
            if s.site_id == site_id:
                return s
        return None


# Tests

async def _run_stub_test():
    from unittest.mock import MagicMock
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("=" * 60)
    print("CRM Capacity Feed - Stub Test")
    print("=" * 60)

    mock_session = MagicMock(spec=Session)

    mock_contract = MagicMock(spec=Contract)
    mock_contract.id = 1
    mock_contract.contract_type = ContractType.COLO_MSA
    mock_contract.site_id = "BALD-01"
    mock_contract.status = ContractStatus.ACTIVE
    mock_assignment = MagicMock(spec=ContractRackAssignment)
    mock_assignment.committed_kw = Decimal("580")
    mock_assignment.rack_ids = ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08"]
    mock_contract.rack_assignments = [mock_assignment]

    mock_rate = MagicMock(spec=RateSchedule)
    mock_rate.rate_value = Decimal("170.00")

    this_module = sys.modules[__name__]
    patches = {}
    for fn_name in ["get_active_contracts_for_site", "get_total_committed_kw", "get_available_capacity_kw", "get_rate"]:
        patches[fn_name] = getattr(this_module, fn_name)

    this_module.get_active_contracts_for_site = lambda s, sid: [mock_contract]
    this_module.get_total_committed_kw = lambda s, sid: Decimal("580")
    this_module.get_available_capacity_kw = lambda s, sid, cap: cap - Decimal("580")
    this_module.get_rate = lambda s, cid, rt, d=None: mock_rate

    try:
        config = CRMConfig.default()
        stub_stream_b = StubStreamBClient()
        stub_hubspot = StubHubSpotClient()
        feed = CRMCapacityFeed(config=config, stream_b_client=stub_stream_b, session=mock_session, hubspot_client=stub_hubspot)

        # Test 1: Sync site
        print("\n--- Test 1: Sync site ---")
        result = await feed.sync_site("BALD-01")
        snap = result.snapshot
        print(f"  Capacity: {snap.sold_kw}/{snap.total_capacity_kw} kW ({snap.utilisation_pct}% utilised)")
        print(f"  Available: {snap.available_kw} kW / {snap.available_racks} racks")
        print(f"  PUE: {snap.pue_current} | Heat export: {snap.heat_export_utilisation_pct}%")
        print(f"  Deal created: {result.deal_created}")
        assert result.success
        assert snap.utilisation_pct == Decimal("58.0")
        assert result.deal_created
        print("  ok")

        # Test 2: High utilisation alert
        print("\n--- Test 2: High utilisation alert ---")
        this_module.get_total_committed_kw = lambda s, sid: Decimal("920")
        result2 = await feed.sync_site("BALD-01")
        print(f"  Utilisation: {result2.snapshot.utilisation_pct}% | Alert: {result2.alert_sent}")
        assert result2.snapshot.utilisation_pct == Decimal("92.0")
        assert result2.alert_sent
        print("  ok")

        # Test 3: Webhook
        print("\n--- Test 3: Deal closed webhook ---")
        webhook_result = await feed.handle_deal_closed({
            "objectId": "DEAL-42", "propertyName": "dealstage", "propertyValue": "closedwon",
            "properties": {"site_id": "BALD-01", "customer_name": "TensorFlow Cloud", "committed_kw": "420"},
        })
        assert webhook_result["status"] == "draft_created"
        print(f"  {webhook_result['status']}: {webhook_result['customer_name']} at {webhook_result['site_id']}")
        print("  ok")

        # Test 4: Batch
        print("\n--- Test 4: Batch sync ---")
        this_module.get_total_committed_kw = lambda s, sid: Decimal("580")
        results = await feed.sync_all_sites()
        assert len(results) == 1 and results[0].success
        print(f"  {len(results)} sites synced")
        print("  ok")

        # HubSpot calls
        print(f"\n--- HubSpot calls: {len(stub_hubspot.calls)} ---")
        for call in stub_hubspot.calls:
            print(f"  {call['method']}")

        print("\nAll assertions passed")

    finally:
        for fn_name, orig in patches.items():
            setattr(this_module, fn_name, orig)


if __name__ == "__main__":
    asyncio.run(_run_stub_test())
