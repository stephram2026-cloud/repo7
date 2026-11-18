"""Reproductions for checkout review candidates (batch 2).

Each test asserts the *buggy* behaviour, so "passed" == "bug reproduced".
"""

import datetime
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from promise import Promise

from saleor.checkout.delivery_context import (
    assign_delivery_method_to_checkout,
    fetch_shipping_methods_for_checkout,
)
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.migrations.tasks.saleor3_23 import (
    propagate_checkout_deliveries_task,
)
from saleor.checkout.models import Checkout, CheckoutDelivery
from saleor.checkout.search.indexing import update_checkouts_search_vector
from saleor.shipping.models import ShippingMethod


def _info(checkout, manager):
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


# --- R5: in-flight delivery refresh wipes out a concurrent "stale" marker -----


def test_r5_inflight_refresh_overwrites_stale_marker(
    checkout_with_item, plugins_manager, address, settings
):
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])
    checkout_info, _ = _info(checkout, plugins_manager)
    quantity_seen_by_refresh = checkout_info.lines[0].line.quantity

    marker = {}

    def concurrent_lines_mutation(*args, **kwargs):
        # Simulates checkoutLinesUpdate committing while the refresh waits on the
        # FILTER_SHIPPING_METHODS sync webhook: the line quantity changes and the
        # deliveries are marked stale (mark_checkout_deliveries_as_stale_if_needed).
        line = checkout.lines.get()
        line.quantity = quantity_seen_by_refresh + 5
        line.save(update_fields=["quantity"])
        marker["stale_at"] = timezone.now()
        Checkout.objects.filter(pk=checkout.pk).update(
            delivery_methods_stale_at=marker["stale_at"]
        )
        return Promise.resolve([])

    with mock.patch(
        "saleor.checkout.webhooks.exclude_shipping.excluded_shipping_methods_for_checkout",
        side_effect=concurrent_lines_mutation,
    ):
        deliveries = fetch_shipping_methods_for_checkout(
            checkout_info, requestor=None
        ).get()

    assert len(deliveries) == 1
    db_checkout = Checkout.objects.get(pk=checkout.pk)
    assert db_checkout.lines.get().quantity == quantity_seen_by_refresh + 5
    # BUG: deliveries were computed for the old cart, yet the stale marker written
    # by the concurrent mutation is replaced with "fresh for the whole TTL".
    assert db_checkout.delivery_methods_stale_at > marker["stale_at"]
    assert (
        db_checkout.delivery_methods_stale_at
        >= marker["stale_at"]
        + settings.CHECKOUT_DELIVERY_OPTIONS_TTL
        - datetime.timedelta(minutes=1)
    )


# --- R6: search vector built from rows read before the dirty flag is cleared --


def test_r6_search_vector_stale_and_flag_cleared(checkout_with_item):
    old_email = "old-address@example.com"
    new_email = "new-address@example.com"
    Checkout.objects.filter(pk=checkout_with_item.pk).update(
        email=old_email, search_index_dirty=True
    )

    # the batch task reads the dirty rows...
    checkouts = list(
        Checkout.objects.filter(pk=checkout_with_item.pk, search_index_dirty=True)
    )
    # ...then checkoutEmailUpdate commits (email + search_index_dirty=True)...
    Checkout.objects.filter(pk=checkout_with_item.pk).update(
        email=new_email, search_index_dirty=True
    )
    # ...then the task clears the flag and indexes what it read earlier.
    update_checkouts_search_vector(checkouts)

    db_checkout = Checkout.objects.get(pk=checkout_with_item.pk)
    # BUG: flag is clean, but the index holds the old email and not the new one.
    assert db_checkout.search_index_dirty is False
    assert "old-address" in db_checkout.search_vector
    assert "new-address" not in db_checkout.search_vector


# --- R7: migration task resurrects a delivery the customer removed ------------


@mock.patch(
    "saleor.checkout.migrations.tasks.saleor3_23.propagate_checkout_deliveries_task.delay"
)
def test_r7_migration_task_reassigns_removed_delivery(
    mocked_delay, checkout_with_item, plugins_manager, address
):
    # given: a pre-3.23 checkout that still carries the legacy shipping_method FK
    shipping_method = ShippingMethod.objects.get()
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.shipping_method_name = shipping_method.name
    checkout.save(
        update_fields=["shipping_address", "shipping_method", "shipping_method_name"]
    )

    # batch N of the data migration converts it
    propagate_checkout_deliveries_task()
    checkout = Checkout.objects.get(pk=checkout.pk)
    assert checkout.assigned_delivery_id is not None

    # customer removes the delivery method on 3.23 code while the migration chain
    # is still running
    checkout_info, lines = _info(checkout, plugins_manager)
    assign_delivery_method_to_checkout(checkout_info, lines, plugins_manager, None)
    checkout = Checkout.objects.get(pk=checkout.pk)
    assert checkout.assigned_delivery_id is None
    # legacy column is never cleared by the new code
    assert checkout.shipping_method_id == shipping_method.id

    # batch N+1 of the data migration
    propagate_checkout_deliveries_task()

    checkout = Checkout.objects.get(pk=checkout.pk)
    # BUG: the removed shipping method is assigned again.
    assert checkout.assigned_delivery_id is not None
    assert (
        CheckoutDelivery.objects.get(
            pk=checkout.assigned_delivery_id
        ).built_in_shipping_method_id
        == shipping_method.id
    )


# --- R9: is_checkout_locked ignores whole days --------------------------------


def test_r9_checkout_locked_again_days_later(checkout, settings):
    checkout.completing_started_at = timezone.now() - datetime.timedelta(
        days=2, seconds=10
    )
    assert settings.CHECKOUT_COMPLETION_LOCK_TIME > 10
    # BUG: completion started 2 days ago, still reported as locked.
    assert checkout.is_checkout_locked() is True
