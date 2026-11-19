"""Second-generation confirmed order tools.

The inventory and return-create APIs are intentionally fail-closed: no such
PointNXT endpoints are present in this repository.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from config import PNXT_RETURN_WINDOW_DAYS
from services.pointnxt_api import PointNXTAPI
from services.write_tools import WriteContext, WriteError, WriteToolSpec
from tools.orders import get_api, parse_permitted_transitions

_ORDER_LOOKUP = "/commerce/orders"
_MISSING_STOCK_ENDPOINT = "TODO_CAPTURE_POINTNXT_STOCK_ADJUSTMENT_ENDPOINT"
_MISSING_RETURN_ENDPOINT = "TODO_CAPTURE_POINTNXT_RETURN_CREATE_ENDPOINT"

def _body(response: Any) -> dict[str, Any]:
    value = response.get("data", response) if isinstance(response, dict) else {}
    return value if isinstance(value, dict) else {}

async def _resolve(args: dict[str, Any]) -> dict[str, Any]:
    order_id, reference = args.get("order_id"), args.get("order_reference")
    api = get_api()
    if order_id:
        order = _body(await api.async_get(f"/commerce/orders/{order_id.strip()}"))
        if reference:
            ref_value = str(reference).strip().lstrip("#")
            result = await api.async_get(_ORDER_LOOKUP, params={"orderNo": ref_value})
            data = result.get("data", result) if isinstance(result, dict) else {}
            items = data.get("items", []) if isinstance(data, dict) else []
            if items and str(items[0].get("id")) != str(order.get("id")):
                raise WriteError("INVALID_INPUT", "order_id and order_reference resolve to different orders.")
    else:
        reference = str(reference).strip().lstrip("#")
        result = await api.async_get(_ORDER_LOOKUP, params={"orderNo": reference})
        data = result.get("data", result) if isinstance(result, dict) else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            raise WriteError("NOT_FOUND", "The order was not found.")
        order = _body(await api.async_get(f"/commerce/orders/{items[0].get('id')}"))
    if not order.get("id"):
        raise WriteError("NOT_FOUND", "The order was not found.")
    return order

def _target(order: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(order["id"]), "reference": order.get("orderNo") or str(order["id"]),
            "version": order.get("version") or order.get("updatedAt") or order.get("updated_at"), "order": order}

def _identity_schema() -> dict[str, Any]:
    return {
        "order_id": {"type": "string", "description": "Internal PointNXT order UUID; use this or order_reference."},
        "order_reference": {"type": "string", "description": "PointNXT order number, optionally prefixed with #; use this or order_id."},
    }

def _require_identity(args: dict[str, Any]) -> None:
    if not args.get("order_id") and not args.get("order_reference"):
        raise WriteError("INVALID_INPUT", "Give order_id or order_reference.")

async def _load_cancel(args, context):
    _require_identity(args); order = await _resolve(args); status = str(order.get("orderStatus", "")).upper()
    if status in {"SHIPPED", "IN_TRANSIT", "DELIVERED", "CANCELLED", "RTO", "RETURNED"}:
        raise WriteError("VALIDATION_ERROR", f"Order cannot be cancelled while it is {status}.")
    return _target(order)
def _validate_cancel(args, target, context):
    if not isinstance(args.get("reason"), str) or len(args["reason"].strip()) < 3: raise WriteError("VALIDATION_ERROR", "reason must be at least 3 characters.")
def _preview_cancel(args, target, context):
    order = target["order"]; warnings = []
    if str(order.get("paymentMethod", order.get("paymentType", ""))).upper() != "COD": warnings.append("Prepaid order: check refund handling after cancellation.")
    return {"summary": f"Cancel order {target['reference']}", "proposed_changes": {"reason": args["reason"].strip()}, "warnings": warnings}
async def _execute_cancel(args, target, context):
    return await get_api().async_request("PUT", f"/commerce/orders/{target['id']}", json={"orderStatus":"CANCELLED", "cancelReason":args["reason"].strip()}, retry=False)

CANCEL_ORDER_SPEC = WriteToolSpec("cancel_order", "Preview and confirm cancellation of an eligible PointNXT order.", {"type":"object","properties":{**_identity_schema(),"reason":{"type":"string","description":"Reason for cancellation, at least 3 characters."}},"required":["reason"]}, True, False, _load_cancel, _validate_cancel, _preview_cancel, _execute_cancel)

async def _load_tags(args, context):
    _require_identity(args); order = await _resolve(args); return _target(order)
def _validate_tags(args, target, context):
    for key in ("add", "remove"):
        values = args.get(key) or []
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values): raise WriteError("VALIDATION_ERROR", f"{key} must be an array of non-empty strings.")
def _preview_tags(args, target, context):
    current = list(target["order"].get("tags") or []); removed = {v.casefold() for v in (args.get("remove") or [])}; result = [v for v in current if str(v).casefold() not in removed]
    seen = {str(v).casefold() for v in result}
    for value in (args.get("add") or []):
        if value.casefold() not in seen: result.append(value); seen.add(value.casefold())
    if result == current: return {"summary":"No tag changes required.","proposed_changes":{"before":current,"after":result},"warnings":["NO_CHANGE"]}
    return {"summary":f"Edit tags on order {target['reference']}","proposed_changes":{"before":current,"after":result}}
async def _execute_tags(args, target, context):
    preview = _preview_tags(args,target,context); return await get_api().async_request("PUT",f"/commerce/orders/{target['id']}/tags",json={"tags":preview["proposed_changes"]["after"]},retry=False)
EDIT_ORDER_TAGS_SPEC = WriteToolSpec("edit_order_tags","Preview and confirm case-insensitive tag additions and removals.",{"type":"object","properties":{**_identity_schema(),"add":{"type":"array","items":{"type":"string"},"description":"Tags to add without duplicates."},"remove":{"type":"array","items":{"type":"string"},"description":"Tags to remove, case-insensitively."}},"required":[]},True,False,_load_tags,_validate_tags,_preview_tags,_execute_tags)

async def _load_bulk(args, context):
    refs=list(dict.fromkeys(args.get("order_references") or []))
    if not 1 <= len(refs) <= 50: raise WriteError("VALIDATION_ERROR","order_references must contain 1 to 50 references.")
    transitions=parse_permitted_transitions(await get_api().async_get("/commerce/orders/status-transitions")); ready=[]; skipped=[]
    for ref in refs:
        try:
            order=await _resolve({"order_reference":ref}); current=str(order.get("orderStatus","")).upper(); target=args["order_status"].upper()
            if target not in transitions.get(current,set()): skipped.append({"reference":ref,"reason":f"Transition from {current} to {target} is not permitted."})
            else: ready.append({"id":str(order["id"]),"reference":order.get("orderNo") or ref,"version":order.get("version") or order.get("updatedAt"),"current_status":current})
        except WriteError as error: skipped.append({"reference":ref,"reason":error.message})
    return {"id":"bulk:"+hashlib.sha256("|".join(x["id"] for x in ready).encode()).hexdigest()[:16],"reference":"bulk","version":hashlib.sha256(repr(ready).encode()).hexdigest(),"ready":ready,"skipped":skipped}
def _validate_bulk(args,target,context):
    if args.get("order_status","").upper()=="": raise WriteError("VALIDATION_ERROR","order_status is required.")
def _preview_bulk(args,target,context): return {"summary":f"Change {len(target['ready'])} orders to {args['order_status'].upper()}","proposed_changes":{"ready":target["ready"],"skipped":target["skipped"],"order_status":args["order_status"].upper(),"notes":args.get("notes")}}
async def _execute_bulk(args,target,context):
    async def one(item):
        try:
            result=await get_api().async_request("PUT",f"/commerce/orders/{item['id']}",json={k:v for k,v in {"orderStatus":args["order_status"].upper(),"notes":args.get("notes"),"version":item.get("version")}.items() if v is not None},retry=False)
            return {"reference":item["reference"],"status":"updated","result":result}
        except Exception as error: return {"reference":item["reference"],"status":"failed","reason":str(error)}
    results=[]
    for start in range(0,len(target["ready"]),3): results.extend(await asyncio.gather(*(one(x) for x in target["ready"][start:start+3])))
    return {"updated_count":sum(x["status"]=="updated" for x in results),"failed_count":sum(x["status"]=="failed" for x in results),"results":results,"skipped":target["skipped"]}
BULK_CHANGE_ORDER_STATUS_SPEC=WriteToolSpec("bulk_change_order_status","Preview and confirm status changes for up to 50 orders with per-order results.",{"type":"object","properties":{"order_references":{"type":"array","description":"One to 50 order numbers; duplicates are ignored."},"order_status":{"type":"string","description":"Target PointNXT order status."},"notes":{"type":"string","description":"Optional note applied to each status change."}},"required":["order_references","order_status"]},True,False,_load_bulk,_validate_bulk,_preview_bulk,_execute_bulk)

_PHONE = re.compile(r"^[6-9]\d{9}$"); _PIN = re.compile(r"^[1-9]\d{5}$")
async def _load_address(args, context): _require_identity(args); return _target(await _resolve(args))
def _validate_address(args,target,context):
    fields=("name","phone","address_line1","address_line2","landmark","city","state","pincode")
    if not any(args.get(k) is not None for k in fields): raise WriteError("VALIDATION_ERROR","At least one shipping address field is required.")
    if args.get("phone") is not None and not _PHONE.fullmatch(re.sub(r"[ +]","",args["phone"].replace("+91","",1))): raise WriteError("VALIDATION_ERROR","phone must be a 10-digit Indian mobile number.")
    if args.get("pincode") is not None and not _PIN.fullmatch(args["pincode"]): raise WriteError("VALIDATION_ERROR","pincode must be 6 digits and must not start with 0.")
    order=target["order"]; status=str(order.get("orderStatus","")).upper(); shipments=order.get("shipments") or []
    if any(s.get("awbNo") or s.get("awb") or s.get("trackingNumber") for s in shipments) or order.get("awbNo") or order.get("trackingNumber"): raise WriteError("VALIDATION_ERROR","Shipping address cannot be changed after an AWB or tracking number exists.")
    if status in {"SHIPPED","IN_TRANSIT","DELIVERED"}: raise WriteError("VALIDATION_ERROR","Shipping address cannot be changed after shipping.")
def _preview_address(args,target,context):
    old=target["order"].get("shippingAddress") or {}; changes={k:("******"+args[k][-4:] if k=="phone" else args[k]) for k in ("name","phone","address_line1","address_line2","landmark","city","state","pincode") if args.get(k) is not None and args[k]!=old.get(k)}
    return {"summary":f"Update shipping address for {target['reference']}","proposed_changes":changes}
async def _execute_address(args,target,context):
    address=dict(target["order"].get("shippingAddress") or {}); address.update({k:v for k,v in args.items() if k in {"name","phone","address_line1","address_line2","landmark","city","state","pincode"} and v is not None}); return await get_api().async_request("PUT",f"/commerce/orders/{target['id']}",json={"shippingAddress":address,"version":target["version"]},retry=False)
UPDATE_SHIPPING_ADDRESS_SPEC=WriteToolSpec("update_shipping_address","Preview and confirm a shipping-address update before shipping.",{"type":"object","properties":{**_identity_schema(),**{k:{"type":"string","description":f"Optional shipping address {k}."} for k in ("name","phone","address_line1","address_line2","landmark","city","state","pincode")}},"required":[]},True,False,_load_address,_validate_address,_preview_address,_execute_address)

def _missing(name, message):
    async def load(args, context): raise WriteError("CAPABILITY_MISSING", message, details={"todo":name,"capture":["URL","HTTP method","payload field names"]})
    return load
def _noop(*args): return None
ADJUST_STOCK_SPEC=WriteToolSpec("adjust_stock","Adjust inventory only when the PointNXT stock-adjustment endpoint is confirmed.",{"type":"object","properties":{"sku":{"type":"string","description":"Product SKU."},"warehouse_id":{"type":"string","description":"Warehouse UUID."},"quantity_change":{"type":"integer","description":"Non-zero stock delta from -10000 to 10000."},"reason":{"type":"string","description":"Adjustment reason."},"note":{"type":"string","description":"Optional adjustment note."}},"required":["sku","warehouse_id","quantity_change","reason"]},True,True,_missing(_MISSING_STOCK_ENDPOINT,"PointNXT stock read/adjustment endpoints are not confirmed."),_noop,_noop,_noop)
CREATE_RETURN_REQUEST_SPEC=WriteToolSpec("create_return_request","Create a return only when the PointNXT return-create endpoint is confirmed.",{"type":"object","properties":{"items":{"type":"array","description":"SKU and positive quantity items."},"reason":{"type":"string","description":"Return reason."},"return_type":{"type":"string","description":"REFUND or EXCHANGE."},**_identity_schema()},"required":["items","reason"]},True,True,_missing(_MISSING_RETURN_ENDPOINT,"PointNXT return creation endpoint is not confirmed."),_noop,_noop,_noop)

ALL_WRITE_TOOLS_V2=(CANCEL_ORDER_SPEC,BULK_CHANGE_ORDER_STATUS_SPEC,EDIT_ORDER_TAGS_SPEC,UPDATE_SHIPPING_ADDRESS_SPEC,ADJUST_STOCK_SPEC,CREATE_RETURN_REQUEST_SPEC)
