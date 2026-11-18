"""R11: sync shipping webhook subscription asking for checkout.shippingMethod recurses."""

import datetime
import sys
from unittest import mock

import pytest
from django.utils import timezone

from saleor.checkout.delivery_context import (
    assign_delivery_method_to_checkout,
    fetch_shipping_methods_for_checkout,
    get_or_fetch_checkout_deliveries,
)
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout
from saleor.webhook.models import Webhook

SUBSCRIPTION = """
subscription {
  event {
    ... on ShippingListMethodsForCheckout {
      checkout { id %s { ... on ShippingMethod { id } } }
    }
  }
}
"""


def _info(checkout, manager):
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


@pytest.mark.parametrize("field", ["shippingMethod", "deliveryMethod"])
@mock.patch("saleor.webhook.transport.synchronous.transport.send_webhook_request_sync")
def test_r11_subscription_with_assigned_delivery_field_recurses(
    mocked_webhook,
    field,
    checkout_with_item,
    plugins_manager,
    address,
    shipping_app_with_subscription,
):
    mocked_webhook.return_value = []
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])

    # customer has a (built-in) delivery assigned
    Webhook.objects.update(is_active=False)
    checkout_info, lines = _info(checkout, plugins_manager)
    delivery = fetch_shipping_methods_for_checkout(checkout_info, requestor=None).get()[0]
    assign_delivery_method_to_checkout(checkout_info, lines, plugins_manager, delivery)

    # shipping app subscribes with a query that includes the assigned delivery
    Webhook.objects.update(is_active=True, subscription_query=SUBSCRIPTION % field)

    # deliveries become stale (any lines / address / promo code mutation does this)
    Checkout.objects.filter(pk=checkout.pk).update(
        delivery_methods_stale_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    checkout = Checkout.objects.get(pk=checkout.pk)
    checkout_info, lines = _info(checkout, plugins_manager)

    sys.setrecursionlimit(3000)
    depth = {"n": 0}
    from saleor.checkout import delivery_context as dc

    original = dc.fetch_shipping_methods_for_checkout

    def counting(*args, **kwargs):
        depth["n"] += 1
        if depth["n"] > 25:
            raise AssertionError("nested refresh depth exceeded 25")
        return original(*args, **kwargs)

    with mock.patch.object(dc, "fetch_shipping_methods_for_checkout", counting):
        try:
            result = get_or_fetch_checkout_deliveries(
                checkout_info, requestor=None
            ).get()
            outcome = f"returned {len(result)} deliveries"
        except BaseException as e:  # noqa: BLE001
            outcome = f"raised {type(e).__name__}: {str(e)[:120]}"
    print(
        f"R11[{field}] nested fetch calls={depth['n']} "
        f"webhook requests={mocked_webhook.call_count} outcome={outcome}"
    )
    assert depth["n"] > 1
