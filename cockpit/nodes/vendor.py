"""Node 4 - Vendor resolution: match the invoice to the vendor master (tax ID, alias, fuzzy name)."""
from __future__ import annotations

from ..core.domain import Context, Invoice
from ..core.resolve import resolve_vendor

STAGE, TITLE = "vendor", "Vendor resolution"


def run(inv: Invoice, ctx: Context):
    vm = resolve_vendor(inv, ctx.vendors)
    data = {"status": vm.status, "method": vm.method, "score": vm.score, "candidates": vm.candidates,
            "vendor_id": vm.vendor.id if vm.vendor else None}
    if vm.status == "matched":
        return "ok", f"Matched {vm.vendor.name} by {vm.method}.", data, vm
    msg = {"ambiguous": "Vendor name is ambiguous between master records.",
           "unknown": f"'{inv.vendor_name}' is not in the vendor master.",
           "tax_conflict": "Tax ID belongs to a different vendor than the name on the invoice."}.get(vm.status, vm.status)
    return "warn", msg, data, vm
