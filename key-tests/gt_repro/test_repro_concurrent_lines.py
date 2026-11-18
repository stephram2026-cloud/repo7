"""Concurrent checkoutLinesAdd / checkoutLinesUpdate for the same variant.

Threaded tests use two real DB connections and the real GraphQL endpoint.
Tests named *_bug assert the buggy behaviour ("passed" == reproduced);
tests named *_control assert correct behaviour.
"""

import threading

import graphene
import pytest
from django.contrib.sites.models import Site
from django.db import connections

from saleor.checkout.models import Checkout, CheckoutLine
from saleor.graphql.checkout.mutations import checkout_lines_add as lines_add_module
from saleor.graphql.tests.fixtures import ApiClient
from saleor.tests.utils import prepare_test_db_connections

LINES_ADD = """
mutation checkoutLinesAdd($id: ID, $lines: [CheckoutLineInput!]!) {
  checkoutLinesAdd(id: $id, lines: $lines) {
    checkout { lines { id quantity variant { id } } }
    errors { field code message }
  }
}
"""
LINES_UPDATE = """
mutation checkoutLinesUpdate($id: ID, $lines: [CheckoutLineUpdateInput!]!) {
  checkoutLinesUpdate(id: $id, lines: $lines) {
    checkout { lines { id quantity variant { id } } }
    errors { field code message }
  }
}
"""


def _request(query, checkout_pk, variant_pk, quantity, start=None):
    """One storefront request on its own thread/connection."""
    out = {}

    def run():
        prepare_test_db_connections()
        try:
            if start is not None:
                start.wait()
            response = ApiClient(user=None).post_graphql(
                query,
                {
                    "id": graphene.Node.to_global_id("Checkout", checkout_pk),
                    "lines": [
                        {
                            "variantId": graphene.Node.to_global_id(
                                "ProductVariant", variant_pk
                            ),
                            "quantity": quantity,
                        }
                    ],
                },
            )
            content = response.json()
            out["errors"] = content.get("errors")
            data = (content.get("data") or {}).get(
                "checkoutLinesAdd" if "LinesAdd" in query else "checkoutLinesUpdate"
            )
            out["mutation_errors"] = data["errors"] if data else None
        except Exception as e:  # noqa: BLE001
            out["exception"] = repr(e)[:200]
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    return thread, out


def _run_pair(query, checkout_pk, variant_pk, quantity, *, force_overlap):
    """Send two identical requests at once.

    force_overlap=True pins the interleaving "both requests have read the existing
    lines, neither has taken the checkout lock yet" with a barrier placed at the
    entry of add_variants_to_checkout. force_overlap=False adds no hooks at all.
    """
    original = lines_add_module.add_variants_to_checkout
    if force_overlap:
        barrier = threading.Barrier(2, timeout=15)

        def synced(*args, **kwargs):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return original(*args, **kwargs)

        lines_add_module.add_variants_to_checkout = synced
    start = threading.Barrier(2, timeout=15)
    try:
        pairs = [
            _request(query, checkout_pk, variant_pk, quantity, start) for _ in range(2)
        ]
        for thread, _ in pairs:
            thread.start()
        for thread, _ in pairs:
            thread.join(timeout=60)
    finally:
        lines_add_module.add_variants_to_checkout = original
    return [out for _, out in pairs]


def _lines(checkout_pk):
    return list(
        CheckoutLine.objects.filter(checkout_id=checkout_pk).values_list(
            "variant_id", "quantity"
        )
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("query", [LINES_ADD, LINES_UPDATE], ids=["add", "update"])
def test_e1_two_requests_for_new_variant_create_two_lines_bug(query, checkout, stock):
    variant_pk = stock.product_variant_id
    assert _lines(checkout.pk) == []

    results = _run_pair(query, checkout.pk, variant_pk, 1, force_overlap=True)

    print("E1 results:", results, "lines:", _lines(checkout.pk))
    assert all(r.get("mutation_errors") == [] for r in results), results
    # BUG: one variant, two checkout lines
    assert _lines(checkout.pk) == [(variant_pk, 1), (variant_pk, 1)]


@pytest.mark.django_db(transaction=True)
def test_e2_no_hooks_natural_rate(checkout, stock, channel_USD):
    """No patching at all: just two requests sent at the same moment, 40 times."""
    variant_pk = stock.product_variant_id
    rounds, split, single, other = 40, 0, 0, []
    for _ in range(rounds):
        c = Checkout.objects.create(
            channel=channel_USD, currency=channel_USD.currency_code, email="a@b.c"
        )
        results = _run_pair(LINES_ADD, c.pk, variant_pk, 1, force_overlap=False)
        lines = _lines(c.pk)
        if lines == [(variant_pk, 1), (variant_pk, 1)]:
            split += 1
        elif lines == [(variant_pk, 2)]:
            single += 1
        else:
            other.append((lines, results))
    print(
        f"E2 natural rate: {split}/{rounds} split into two lines, "
        f"{single}/{rounds} merged into one line, other={other}"
    )
    assert split + single == rounds, other
    assert split > 0


@pytest.mark.django_db(transaction=True)
def test_e3_variant_already_in_checkout_control(checkout_with_item):
    line = checkout_with_item.lines.get()
    results = _run_pair(
        LINES_ADD, checkout_with_item.pk, line.variant_id, 1, force_overlap=True
    )
    print("E3 results:", results, "lines:", _lines(checkout_with_item.pk))
    assert all(r.get("mutation_errors") == [] for r in results), results
    # correct: one line, both increments applied
    assert _lines(checkout_with_item.pk) == [(line.variant_id, line.quantity + 2)]


@pytest.mark.django_db(transaction=True)
def test_e4_quantity_limit_bypassed_by_concurrent_adds_bug(
    checkout_with_item, site_settings
):
    line = checkout_with_item.lines.get()
    site_settings.limit_quantity_per_checkout = line.quantity + 3
    site_settings.save(update_fields=["limit_quantity_per_checkout"])
    Site.objects.clear_cache()

    results = _run_pair(
        LINES_ADD, checkout_with_item.pk, line.variant_id, 2, force_overlap=True
    )
    lines = _lines(checkout_with_item.pk)
    print("E4 results:", results, "lines:", lines, "limit:", line.quantity + 3)
    assert all(r.get("mutation_errors") == [] for r in results), results
    # BUG: each request validated quantity+2 <= limit, together they exceed it
    assert lines == [(line.variant_id, line.quantity + 4)]

    # control: the same third request made sequentially is rejected
    thread, out = _request(LINES_ADD, checkout_with_item.pk, line.variant_id, 1)
    thread.start()
    thread.join(timeout=60)
    assert [e["code"] for e in out["mutation_errors"]] == ["QUANTITY_GREATER_THAN_LIMIT"]
